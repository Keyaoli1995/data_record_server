# 拒绝连接归档与保留期 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 有效 JSONL 按设备归档；每条 TCP 连接的拒绝记录集中写入一个诊断 `.bin`；启动时和服务器本地时间每日零点清理 30 天前归档。

**Architecture:** 解析器把原始字节放入拒绝对象。请求处理器为首次拒绝的连接惰性创建记录器，后续拒绝追加同一文件。存储维护活动文件集合并安全清理；服务轮询按本地日期调度。

**Tech Stack:** Python 3.9+、`unittest`、Docker Compose、POSIX shell。

---

### Task 1: 让协议拒绝结果携带原始字节

**Files:**
- Modify: `data_record_server/protocol.py:12-106`
- Test: `tests/test_protocol.py:14-206`

- [ ] **Step 1: 写失败测试**

```python
def test_preserves_raw_bytes_for_rejections(self):
    result = JsonlStreamParser(8).feed(b"bad\n" + b"x" * 9)
    self.assertEqual((b"bad\n", b"x" * 9),
                     tuple(item.raw_bytes for item in result.rejections))
```

另测试 `finish()` 使 `b'{"deviceId":"D1"'` 作为 `incomplete_line` 的 `raw_bytes` 返回。

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_protocol.JsonlStreamParserTest.test_preserves_raw_bytes_for_rejections -v`

Expected: FAIL，`RecordRejected` 不存在 `raw_bytes`。

- [ ] **Step 3: 实现最小协议字段**

```python
class RecordRejected(ValueError):
    def __init__(self, reason: str, raw_bytes: bytes) -> None:
        super().__init__(reason)
        self.reason = reason
        self.raw_bytes = raw_bytes
```

每个完整行拒绝传入原始行（含换行符）；无换行超长分支在清空 buffer 前复制字节；`finish()` 复制并清空未完成 buffer。更新断言辅助函数，断言全部拒绝原始字节。

- [ ] **Step 4: 验证并提交**

Run: `python3 -m unittest tests.test_protocol -v`

Expected: PASS。

```bash
git add data_record_server/protocol.py tests/test_protocol.py
git commit -m "feat: preserve rejected record bytes"
```

### Task 2: 实现一个连接一个诊断 BIN

**Files:**
- Modify: `data_record_server/storage.py:20-260`
- Test: `tests/test_storage.py:20-520`

- [ ] **Step 1: 写失败测试**

```python
recorder = storage.open_rejected_connection(("203.0.113.7", 5007))
recorder.record_rejected("invalid_json", b"bad\n")
recorder.record_rejected("invalid_utf8", b"\xff\n")
recorder.close("eof")
self.assertEqual(b"bad\n\xff\n", connection_file.read_bytes())
```

断言只创建一个 `connections/*.bin`。事件顺序为 `rejected_connection_started`、两条 `record_rejected`、`rejected_connection_closed`；拒绝事件包含同一个 `file`、本次 `rejected_bytes` 和递增 `total_bytes`。

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_storage.StorageTest.test_rejected_connection_appends_to_one_bin -v`

Expected: FAIL，缺少 `open_rejected_connection`。

- [ ] **Step 3: 实现记录器**

新增 `RejectedConnectionRecorder` 和 `Storage.open_rejected_connection(client_address)`。文件名沿用旧规则：UTC 时间、将 IPv6 冒号换成下划线的 IP、端口与 UUID。记录器先写入和 flush 原始字节，再写拒绝事件；`close(reason)` 幂等关闭并写关闭事件。Storage 以锁维护活动诊断相对路径，关闭回调移除它。

- [ ] **Step 4: 验证边界并提交**

新增 IPv6 安全命名、从未拒绝不生成文件、幂等关闭、事件写入失败不遗留活动路径的测试。

Run: `python3 -m unittest tests.test_storage -v`

Expected: PASS。

```bash
git add data_record_server/storage.py tests/test_storage.py
git commit -m "feat: archive rejected connection records"
```

### Task 3: 在 TCP 处理器复用诊断记录器

**Files:**
- Modify: `data_record_server/server.py:20-57`
- Test: `tests/test_server.py:160-340`

- [ ] **Step 1: 写失败集成测试**

一个客户端发送：

```python
valid = b'{"deviceId":"D1","n":1}\n'
invalid = b'{"deviceId":\nnot-json\n'
# sendall(valid + invalid + valid); shutdown(SHUT_WR)
```

断言设备会话只包含两条有效行；仅一个诊断 BIN，内容等于两条无效行；两条拒绝事件指向它，关闭原因为 `eof`。

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_server.CollectorServerTest.test_keeps_valid_records_and_one_rejected_connection_bin -v`

Expected: FAIL，当前处理器只记录拒绝事件。

- [ ] **Step 3: 实现处理器生命周期**

在 `handle()` 保存局部 `rejected_recorder`，默认关闭原因为 `handler_error`。首次 rejection 调用 `storage.open_rejected_connection(self.client_address)`，随后：

```python
rejected_recorder.record_rejected(rejection.reason, rejection.raw_bytes)
```

EOF、socket 超时、超长行和异常使用 `eof`、`socket_timeout`、`line_too_long`、`handler_error`；`finally` 关闭已创建的记录器。解析器 `finish()` 产生的拒绝走同一逻辑。

- [ ] **Step 4: 验证并提交**

添加超长行、socket 超时未完成行、服务器关闭时的诊断文件测试。

Run: `python3 -m unittest tests.test_server -v`

Expected: PASS。

```bash
git add data_record_server/server.py tests/test_server.py
git commit -m "feat: capture rejected TCP connection bytes"
```

### Task 4: 配置并实现保留期清理与本地午夜调度

**Files:**
- Modify: `data_record_server/config.py:7-55`
- Modify: `data_record_server/storage.py:150-320`
- Modify: `data_record_server/server.py:61-145,350-365`
- Test: `tests/test_config.py:8-80`
- Test: `tests/test_storage.py:20-520`
- Test: `tests/test_server.py:20-160`

- [ ] **Step 1: 写失败测试**

测试 `Config.from_environ({}).retention_days == 30`、覆盖值为 45、0/负数/非数值报 `RETENTION_DAYS` 错误。用 `os.utime()` 构造过期与未过期的 `.bin` 和 `.jsonl`，断言清理删除过期普通文件、保留活动文件和符号链接、删除空 `device-*` 目录，并写 `retention_cleanup` 事件。

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_config tests.test_storage.StorageTest.test_cleanup_expired_files -v`

Expected: FAIL，缺少保留期接口。

- [ ] **Step 3: 实现最小清理逻辑**

`Config` 增加默认 30 的正整数 `retention_days`。实现 `Storage.cleanup_expired_files(retention_days, now, trigger)`：只遍历 `connections/*.bin` 和 `devices/device-*/*.jsonl`；以 `lstat()` 和 `stat.S_ISREG` 跳过符号链接；跳过活动路径；删除修改时间早于 `now - retention_days * 86400 秒` 的文件；清理空的非符号链接设备目录；单文件失败后继续并把 `deleted_files`、`reclaimed_bytes`、`failed_files`、`trigger` 写入事件。

在 `CollectorServer` 加可注入 `local_clock`，默认 `datetime.now().astimezone`。首次 `service_actions()` 调用触发 `startup` 清理；本地 `date()` 变化后的首次调用触发 `midnight`；同一天不重复。创建服务器时传递配置保留天数。

- [ ] **Step 4: 验证并提交**

Run: `python3 -m unittest tests.test_config tests.test_storage tests.test_server -v`

Expected: PASS。

```bash
git add data_record_server/config.py data_record_server/storage.py data_record_server/server.py tests/test_config.py tests/test_storage.py tests/test_server.py
git commit -m "feat: retain recent collector archives"
```

### Task 5: 部署配置、中文文档和最终验证

**Files:**
- Modify: `compose.yaml:1-28`
- Modify: `README.md:1-120`
- Modify: `DEPLOYMENT_GUIDE.md:1-140`
- Modify: `CAPABILITIES.md`
- Test: `tests/test_deployment.py:30-150`

- [ ] **Step 1: 写失败部署测试**

断言 Compose 环境中包含默认值 30 的 `RETENTION_DAYS`，卷中包含只读 `/etc/localtime:/etc/localtime:ro`。

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_deployment.DeploymentFilesTest.test_compose_file_declares_the_collector_runtime_contract -v`

Expected: FAIL，Compose 尚未声明保留期和宿主机时间文件。

- [ ] **Step 3: 实现部署和文档**

更新 Compose、README、部署指南和能力文档，说明有效 JSONL 与拒绝 BIN 的目录、关联事件、启动和每日零点清理、默认保留 30 天、`events.jsonl` 不清理，以及不设置单连接上限时需要监控磁盘。

- [ ] **Step 4: 全量验证并提交**

Run:

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q data_record_server tests
sh -n scripts/prepare-data-dir.sh
git diff --check
docker compose config --quiet
```

Expected: 测试、编译、脚本、差异和 Compose 配置通过；若 Docker CLI 不可用，则记录为环境限制。

```bash
git add compose.yaml README.md DEPLOYMENT_GUIDE.md CAPABILITIES.md tests/test_deployment.py
git commit -m "docs: describe rejected archive retention"
```
