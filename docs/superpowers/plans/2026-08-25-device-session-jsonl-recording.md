# 按设备会话保存 JSONL 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**目标：** 将同一 deviceId 在 30 秒连续窗口内、跨 IP/端口/TCP 重连的有效 JSONL 记录原样汇总到一份设备会话 .jsonl 文件。

**架构：** 新增纯 JSONL 流解析器，从任意 TCP 数据块恢复并验证完整记录。Storage 维护按设备 ID 索引的活动会话，并由 CollectorServer 的 service_actions() 周期性回收过期会话。

**技术栈：** Python 3.12、标准库（json、threading、urllib.parse、socketserver、unittest）和 Docker Compose。

---

## 文件职责

- data_record_server/protocol.py：流式 JSONL 分帧、UTF-8/JSON/deviceId 校验。
- data_record_server/storage.py：设备会话文件、线程安全映射、生命周期事件和会话回收。
- data_record_server/config.py：最大 JSON 行长度的环境配置。
- data_record_server/server.py：连接处理、拒绝记录、周期回收和关闭收尾。
- tests/test_protocol.py、tests/test_storage.py、tests/test_server.py：协议、存储和真实 TCP 测试。
- Dockerfile、compose.yaml、README.md、CAPABILITIES.md、DEPLOYMENT_GUIDE.md：部署和运维契约。

### Task 1：配置和 JSONL 流协议（Red）

**文件：**

- Create: tests/test_protocol.py
- Modify: tests/test_config.py
- Modify: data_record_server/config.py:7-48

- [ ] **Step 1：添加配置失败测试**

在 tests/test_config.py 增加：

~~~
def test_uses_a_64_kib_json_line_limit_by_default(self):
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_environ()
    self.assertEqual(65536, config.max_json_line_bytes)

def test_rejects_non_positive_json_line_limit(self):
    for value in ("0", "-1", "not-a-number"):
        with self.subTest(value=value):
            with self.assertRaisesRegex(ValueError, "MAX_JSON_LINE_BYTES"):
                Config.from_environ({"MAX_JSON_LINE_BYTES": value})
~~~

- [ ] **Step 2：添加协议失败测试**

创建 tests/test_protocol.py，沿用 unittest。覆盖跨 recv() 的 CRLF/LF 保留、缺少 ID、无效 UTF-8、无效 JSON、JSON 顶层不是对象、后续 ID 变更、超长未终止行与 EOF 残缺行：

~~~
from data_record_server.protocol import JsonlStreamParser, RecordRejected

class JsonlStreamParserTest(unittest.TestCase):
    def test_keeps_original_crlf_and_device_id_across_chunks(self):
        parser = JsonlStreamParser(64)
        self.assertEqual([], parser.feed(b'{"deviceId":"D1"}\r'))
        records = parser.feed(b'\n{"deviceId":"D1","n":2}\n')
        self.assertEqual(
            [b'{"deviceId":"D1"}\r\n', b'{"deviceId":"D1","n":2}\n'],
            [record.raw_line for record in records],
        )
        self.assertEqual(["D1", "D1"], [record.device_id for record in records])
~~~

- [ ] **Step 3：确认 Red 并提交**

运行：

~~~
cd data_record_server
python3 -m unittest tests.test_config tests.test_protocol -v
git add tests/test_config.py tests/test_protocol.py
git commit -m 'test: init cases for device JSONL protocol'
~~~

预期：Config 缺少 max_json_line_bytes 且 protocol 模块不存在，因功能缺失失败后才提交测试。

### Task 2：配置和协议解析器（Green）

**文件：**

- Create: data_record_server/data_record_server/protocol.py
- Modify: data_record_server/data_record_server/config.py:7-48
- Test: tests/test_config.py
- Test: tests/test_protocol.py

- [ ] **Step 1：实现最小配置**

为 Config 增加 max_json_line_bytes: int = 65536。在 from_environ() 中解析 MAX_JSON_LINE_BYTES；小于等于零时抛出 ValueError("MAX_JSON_LINE_BYTES must be positive")。

- [ ] **Step 2：实现解析器**

在 protocol.py 定义以下 API：

~~~
@dataclass(frozen=True)
class AcceptedRecord:
    device_id: str
    raw_line: bytes

class RecordRejected(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

class JsonlStreamParser:
    def __init__(self, max_line_bytes: int) -> None: ...
    def feed(self, data: bytes) -> list[AcceptedRecord]: ...
    def finish(self) -> None: ...
~~~

feed() 用 bytearray 暂存，按 b"\n" 切分完整行。每行严格 UTF-8 解码并 json.loads()，只接受 dict 和非空字符串 deviceId。第一条有效 ID 固定该连接的 ID；不同 ID 抛出 RecordRejected("device_id_changed")。拒绝原因固定为 invalid_utf8、invalid_json、json_not_object、missing_device_id、device_id_changed、line_too_long、incomplete_line。输出 raw_line 需保留原 LF/CRLF；未终止缓冲区超过上限时清空并抛出 line_too_long。

- [ ] **Step 3：确认 Green 并提交**

~~~
python3 -m unittest tests.test_config tests.test_protocol -v
git add data_record_server/config.py data_record_server/protocol.py tests/test_config.py tests/test_protocol.py
git commit -m 'feat: implement device JSONL protocol and pass tests'
~~~

预期：所有协议和配置测试通过。

### Task 3：设备会话存储（Red）

**文件：**

- Modify: tests/test_storage.py
- Modify: data_record_server/storage.py:1-143

- [ ] **Step 1：添加归并失败测试**

以可控 UTC clock 建立 Storage，断言不同 client_address 的同一 D1 追加到同一文件：

~~~
first = storage.append_record(("203.0.113.1", 5001), "D1", b'{"deviceId":"D1"}\n')
second = storage.append_record(("203.0.113.2", 5002), "D1", b'{"deviceId":"D1","n":2}\r\n')
self.assertEqual(first.relative_path, second.relative_path)
self.assertEqual(
    b'{"deviceId":"D1"}\n{"deviceId":"D1","n":2}\r\n',
    (Path(directory) / first.relative_path).read_bytes(),
)
self.assertIn("devices/device-D1/", first.relative_path)
~~~

再覆盖：不同 ID 建立不同文件；并发追加 D1 时只有一个文件且总字节数正确；record_rejected() 只写事件；空闲恰好 30 秒时写 session_idle_timeout 和 session_closed；close_all_sessions() 重复调用仅关闭一次。

- [ ] **Step 2：确认 Red 并提交**

~~~
python3 -m unittest tests.test_storage.StorageTest -v
git add tests/test_storage.py
git commit -m 'test: init cases for device session storage'
~~~

预期：Storage 不含 append_record、expire_sessions、close_all_sessions 与 record_rejected，测试失败。

### Task 4：设备会话存储（Green）

**文件：**

- Modify: data_record_server/data_record_server/storage.py:1-143
- Test: tests/test_storage.py

- [ ] **Step 1：实现会话 API**

保留 JsonlEventWriter、utc_now() 与 format_time()。新增带会话锁的 DeviceSession，保存 device_id、relative_path、流、total_bytes、last_valid_record_at 和 closed。创建路径必须为：

~~~
devices/device-<urllib.parse.quote(device_id, safe="")>/<UTC>_<escaped-id>_<uuid8>.jsonl
~~~

实现：

~~~
append_record(client_address, device_id, raw_line) -> DeviceSession
expire_sessions(idle_timeout_seconds) -> None
close_all_sessions() -> None
record_rejected(client_address, reason) -> None
~~~

append_record() 持映射锁查找或创建会话；创建时写 session_started。每条连接首次写入该会话时写 session_attached，带 client_ip、client_port、device_id、file。DeviceSession.append() 在会话锁内 write/flush，更新字节数和最后有效时间。expire_sessions() 使用 timedelta(seconds=idle_timeout_seconds)，关闭并删除过期会话；空闲关闭依次写 session_idle_timeout、session_closed。record_rejected() 仅写 time、client_ip、client_port、reason，绝不创建数据目录。close_all_sessions() 必须幂等且对任意关闭原因写一次 session_closed。

- [ ] **Step 2：确认 Green 并提交**

~~~
python3 -m unittest tests.test_storage.StorageTest -v
git add data_record_server/storage.py tests/test_storage.py
git commit -m 'feat: implement device session storage and pass tests'
~~~

预期：测试通过，生成文件均为 .jsonl。

### Task 5：TCP 接入和会话生命周期（Red）

**文件：**

- Modify: tests/test_server.py
- Modify: data_record_server/server.py:23-66,68-180,320-350

- [ ] **Step 1：添加真实 TCP 失败测试**

用两条真实 socket 先后发送同一 D1 的两行并半关闭，断言 data/devices 下仅有一份 .jsonl，内容是两行串联。增加不同 ID 两文件、非法 JSON/缺少 ID 仅有 record_rejected、EOF 残缺行有 incomplete_line、设备空闲产生 session_idle_timeout/session_closed、shutdown() 关闭活动会话。将旧 connections/*.bin 断言替换为新目录和事件。

- [ ] **Step 2：确认 Red 并提交**

~~~
python3 -m unittest tests.test_server.CollectorServerTest -v
git add tests/test_server.py
git commit -m 'test: init cases for device session server flow'
~~~

预期：当前代码直接写 connections/*.bin，因此新增测试失败。

### Task 6：TCP 接入、周期回收和关闭（Green）

**文件：**

- Modify: data_record_server/data_record_server/server.py:23-66,68-180,320-350
- Test: tests/test_server.py

- [ ] **Step 1：实现连接处理**

每条连接创建 JsonlStreamParser(self.server.max_json_line_bytes)。每次 recv() 后，逐条执行：

~~~
self.server.storage.append_record(
    self.client_address, record.device_id, record.raw_line
)
~~~

捕获 RecordRejected 后执行 self.server.storage.record_rejected(self.client_address, error.reason)。line_too_long 记录后终止连接；其他被拒绝的完整行继续读取。EOF 时调用 parser.finish() 并记录 incomplete_line。socket.timeout 仅结束 TCP socket，不得提前结束设备会话。

- [ ] **Step 2：实现回收和收尾**

CollectorServer.__init__ 接收 max_json_line_bytes，create_server() 从 Config 传入。覆盖：

~~~
def service_actions(self) -> None:
    self.storage.expire_sessions(self.idle_timeout_seconds)
    super().service_actions()
~~~

shutdown() 在关闭活跃 socket、等待请求线程结束后调用 storage.close_all_sessions()。server_close() 的所有退出路径也调用相同幂等收尾，确保 session_closed 只出现一次。

- [ ] **Step 3：确认 Green 并提交**

~~~
python3 -m unittest tests.test_server.CollectorServerTest -v
python3 -m unittest discover -s tests -v
git add data_record_server/server.py tests/test_server.py
git commit -m 'feat: implement device session recording and pass tests'
~~~

预期：新增集成测试和完整服务器测试通过。

### Task 7：部署契约、中文文档和最终验证

**文件：**

- Modify: Dockerfile
- Modify: compose.yaml
- Modify: README.md
- Modify: CAPABILITIES.md
- Modify: DEPLOYMENT_GUIDE.md
- Modify: tests/test_deployment.py

- [ ] **Step 1：添加部署失败测试**

在 tests/test_deployment.py 断言 Dockerfile 含 MAX_JSON_LINE_BYTES=65536，Compose 含 MAX_JSON_LINE_BYTES: ${MAX_JSON_LINE_BYTES:-65536}；样本数据扩展名改为 .jsonl，不再断言 connections/。

~~~
python3 -m unittest tests.test_deployment.DeploymentFilesTest -v
~~~

预期：环境变量与新归档布局尚未出现，测试失败。

- [ ] **Step 2：实现部署和文档改动**

Dockerfile 增加 ENV MAX_JSON_LINE_BYTES=65536。compose.yaml 增加 MAX_JSON_LINE_BYTES: ${MAX_JSON_LINE_BYTES:-65536}。三份中文文档改为：仅接受含非空 deviceId 的 JSONL；归档路径为 data/devices/device-<escaped-deviceId>/*.jsonl；同设备 30 秒窗口跨重连归并；无效行只写 record_rejected；列出 session_started、session_attached、session_idle_timeout、session_closed。

- [ ] **Step 3：最终验证并提交**

~~~
python3 -m unittest discover -s tests -v
python3 -m compileall -q data_record_server tests
docker compose config --quiet
docker build -t data-record-server:device-session-test .
git diff --check
git add Dockerfile compose.yaml README.md CAPABILITIES.md DEPLOYMENT_GUIDE.md tests/test_deployment.py
git commit -m 'docs: describe device session JSONL recording'
~~~

预期：所有验证命令以退出码 0 结束。

## 计划自检

- JSONL 协议、30 秒设备会话、跨连接归并、并发隔离、输入拒绝、最大行限制、服务关闭、事件、部署与文档均有对应任务。
- 每项生产实现前都有失败测试、验证命令和 Red-Green-Refactor 提交。
- Config.max_json_line_bytes、JsonlStreamParser、Storage.append_record()、Storage.expire_sessions()、Storage.close_all_sessions() 和 Storage.record_rejected() 的命名在任务间一致；不引入新依赖。

