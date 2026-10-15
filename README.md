# 设备会话 JSONL 采集服务

本服务接收 TCP 上的设备 JSON Lines（JSONL）记录，并按设备身份归档。它不是任意未知 TCP 协议的原始字节采集器。

## 接收与归档契约

- 只接受以换行符结束的**完整 JSONL 行**；每行必须是 UTF-8 JSON 对象，且顶层 `deviceId` 为非空字符串。
- 同一条 TCP 连接中的后续有效记录必须保持相同 `deviceId`。格式错误、非对象、缺少/变更 `deviceId`、超长行或连接结束时未完成的行都不会写入设备 JSONL 归档。
- 有效行以接收时的原始完整行（包括原有的 `\n` 或 `\r\n`）追加到权威归档：`data/devices/device-<URL-escaped-deviceId>/*.jsonl`。
- 每条出现拒绝记录的 TCP 连接会创建一个 `data/connections/*.bin`；该连接中所有被拒绝的原始字节按接收顺序追加到同一个 `.bin`。`data/events.jsonl` 中的 `rejected_connection_started`、`record_rejected` 与 `rejected_connection_closed` 事件通过 `file` 字段关联该文件。
- 归档按 `deviceId` 而不是 IP、端口或 TCP 连接划分。同一设备在不同 IP/端口或 TCP 重连后，只要仍处于活跃会话，就会继续写入同一个 `.jsonl` 文件。
- 设备会话从最后一条有效记录起默认 30 秒空闲后关闭。该会话关闭后下一条有效记录会创建新的 `.jsonl` 文件；TCP 连接断开本身不会立即关闭设备会话。

示例有效记录：

```json
{"deviceId":"raker-01","timestamp":"2026-08-25T12:00:00Z","latitude":30.123}
```

## 配置

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `TCP_PORT` | `30050` | 容器监听并发布的 TCP 端口。 |
| `READ_BUFFER_BYTES` | `4096` | 单次 socket 读取块大小，不是 JSONL 行长度限制。 |
| `IDLE_TIMEOUT_SECONDS` | `30` | 从最后一条有效记录开始计算的设备会话空闲秒数。 |
| `MAX_JSON_LINE_BYTES` | `65536` | 单条 JSONL 内容（不含换行符）的最大字节数；超出时记录 `record_rejected` 并关闭该 TCP 连接。 |
| `DEVICE_RETENTION_DAYS` | `365` | 非活跃设备 JSONL 归档（`data/devices/**/*.jsonl`）的保留天数。 |
| `RETENTION_DAYS` | `30` | 非活跃拒绝连接 `.bin`（`data/connections/*.bin`）的保留天数；不影响设备归档。 |
| `COLLECTOR_UID` / `COLLECTOR_GID` | `10001` / `10001` | 容器非 root 运行身份与 bind mount 所有者。 |

可选的 `.env`（不要提交）示例：

```dotenv
COLLECTOR_UID=1000
COLLECTOR_GID=1000
TCP_PORT=30100
IDLE_TIMEOUT_SECONDS=45
MAX_JSON_LINE_BYTES=65536
DEVICE_RETENTION_DAYS=365
RETENTION_DAYS=30
```

所有数值必须是正整数；`TCP_PORT` 还必须在 `1` 至 `65535` 之间。使用自定义 UID/GID 时，下面的目录准备命令必须传入同一组值。Compose 会将宿主机的 `/etc/localtime` 只读挂载到容器，因此每日清理按服务器本地时间判定。

## 本地测试

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

## 部署

首次部署或修改容器 UID/GID 前，在项目根目录执行：

```bash
sudo scripts/prepare-data-dir.sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

准备脚本只操作项目根目录的 `data/`，拒绝 `data/`、`data/devices/` 和 `data/devices/` 下直接的 `device-*` 会话目录作为符号链接，并以容器运行身份准备可写目录。脚本以递归但不跟随符号链接的方式修复已有归档的所有者；因此在不可信路径中不要手动替换 `data/` 下的目录为符号链接。容器以非 root 身份运行，根文件系统只读，并启用 `no-new-privileges` 和 `cap_drop: ALL`；不要改用 root 绕过权限错误。

使用自定义身份时：

```bash
sudo env COLLECTOR_UID=1000 COLLECTOR_GID=1000 scripts/prepare-data-dir.sh
docker compose config --quiet
docker compose up -d --build

# 上述 .env 示例的 TCP_PORT=30100
docker compose port collector 30100
```

同时在云安全组和宿主机防火墙中，仅向需要的来源 IP 放行实际 TCP 端口。

## 查看状态与数据

```bash
docker compose ps
docker compose logs --tail=100 collector

# 默认配置的 TCP_PORT=30050
docker compose port collector 30050

# 查看设备会话事件
tail -n 50 data/events.jsonl

# 列出所有权威 JSONL 归档
find data/devices -type f -name '*.jsonl' -print

# 列出拒绝连接的原始字节归档
find data/connections -type f -name '*.bin' -print

# 查看第一份归档的 JSONL 记录
first_jsonl=$(find data/devices -type f -name '*.jsonl' -print -quit 2>/dev/null)
if [ -n "$first_jsonl" ]; then
  sed -n '1,20p' "$first_jsonl"
else
  echo '尚未生成设备 JSONL 归档'
fi
```

`data/events.jsonl` 每行是独立的 JSON 对象：

| 事件 | 含义 |
| --- | --- |
| `session_started` | 为一个设备创建新的会话归档文件。 |
| `session_attached` | 某个 IP/端口首次向该活跃设备会话提交有效记录。 |
| `rejected_connection_started` | 已为发生拒绝的 TCP 连接创建 `.bin`；`file` 指向该文件。 |
| `record_rejected` | 一条无效、未完成或超长记录被拒绝，未写入设备 JSONL；`file` 指向所属连接 `.bin`。 |
| `rejected_connection_closed` | 拒绝连接已关闭；`file` 指向该连接 `.bin`。 |
| `session_idle_timeout` | 从最后一条有效记录起达到空闲超时。 |
| `session_closed` | 会话文件已关闭；原因可能是空闲超时或服务停止。 |
| `device_retention_cleanup` / `connection_retention_cleanup` | 已按归档类型执行清理；分别记录各自的删除文件数、回收字节数和失败数。 |

服务启动后会立即执行一次清理，之后每天服务器本地时间跨越 `00:00` 时再执行一次。清理按归档类型分别判定：超过 `DEVICE_RETENTION_DAYS`（默认 365 天）的非活跃 `data/devices/**/*.jsonl`，以及超过 `RETENTION_DAYS`（默认 30 天）的非活跃 `data/connections/*.bin`；删除设备归档后会移除清空的设备目录。活跃文件及 `data/events.jsonl` 不会被删除。两类归档各自写出一条 `*_retention_cleanup` 事件。

## 备份与排障

`data/` 是 bind mount，`docker compose restart`、`down` 或重建容器不会删除其中的数据。请按需要备份 `data/devices/`、`data/connections/` 与 `data/events.jsonl`。保留清理不会删除事件日志，且拒绝连接的单个 `.bin` 没有大小上限；请持续监控磁盘容量。

出现 `Permission denied` 时，先停止容器，以与 Compose 完全相同的 UID/GID 重新执行 `scripts/prepare-data-dir.sh`，再启动容器。出现 `connection refused` 时，检查 `docker compose ps`、`docker compose logs --tail=100 collector`、端口映射以及安全组/防火墙规则。


## Web 录制文件上报（2026-10-15）

已实现默认关闭的创建、更新、结束三阶段通知，以及持久化补发、重启恢复和全球作业地时区识别。配置与维护见 [Web 上报接入与运维](docs/web-reporting-operations.md)。真实 Web 接口和镜像部署尚需环境联调；仅启用本地录制时无需配置 Web。
