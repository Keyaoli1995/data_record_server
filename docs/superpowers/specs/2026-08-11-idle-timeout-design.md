# TCP 连接空闲超时设计

## 目标

当设备或网络突然失联而未正常关闭 TCP 连接时，采集器在连续 30 秒未收到该连接的数据后主动关闭它。这样会结束对应的原始 `.bin` 文件，并将超时与正常断开明确区分。

## 范围

- 默认空闲超时为 30 秒。
- 超时仅影响无数据的那一条 TCP 连接，不影响监听服务或其他客户端。
- 每次成功读取数据后，下一次 30 秒的等待重新开始。
- 设备重启后建立的新 TCP 连接仍对应新的 `.bin` 文件。

不在本次实现 TCP keepalive、应用层心跳、设备身份识别或文件按时间/大小轮转。

## 配置

新增环境变量 `IDLE_TIMEOUT_SECONDS`：

- 缺省值：`30`。
- 类型：正十进制整数，单位为秒。
- 非整数、零或负数均在启动前以 `ValueError` 拒绝。

`compose.yaml` 将此变量传入容器。没有 `.env` 时仍使用默认值 30；需要调整时可在 `.env` 中定义该变量，并与现有 Compose 配置一同校验和部署。

## 运行行为

每个连接处理线程在创建原始文件后，为其 socket 设置读取超时。处理流程为：

1. 连接建立时创建一个 `.bin` 文件，并写入 `connected`。
2. 每次 `recv()` 收到数据时原样写入该文件，并写入 `received`。
3. 若一次 `recv()` 等待满 `IDLE_TIMEOUT_SECONDS` 仍未收到数据，写入 `idle_timeout`。
4. 随后关闭 socket 与 `.bin` 文件，并写入已有的 `disconnected`。

`idle_timeout` 事件字段为：

- `time`：UTC ISO 8601 时间。
- `file`：关联的相对原始文件路径。
- `idle_timeout_seconds`：实际配置的超时秒数。
- `total_bytes`：该连接到超时时已保存的累计字节数。

超时连接的事件顺序固定为：

```text
connected → received（零次或多次）→ idle_timeout → disconnected
```

正常客户端主动关闭、连接重置或服务停止不写入 `idle_timeout`，继续沿用当前的关闭/错误处理行为。

## 测试与验证

测试将覆盖：

- `IDLE_TIMEOUT_SECONDS` 的默认值、显式覆盖和非法值拒绝。
- 使用短测试超时建立真实 TCP 连接；客户端保持静默后，验证对应 `.bin` 关闭。
- 验证事件顺序与 `idle_timeout_seconds`、`total_bytes` 字段。
- 验证在超时之前持续回传数据的连接不会被提前关闭。
- 运行完整单元测试套件及 Python 编译检查。

## 运维说明

生产默认值为 30 秒，适合当前约 1 秒一次的设备回传频率。若设备存在超过 30 秒的正常静默期，应在服务器 `.env` 中提高 `IDLE_TIMEOUT_SECONDS`，并重启服务后生效。
