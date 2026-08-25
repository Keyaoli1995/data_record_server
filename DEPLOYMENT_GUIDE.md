# 部署指南

以下示例假设服务器地址为 `8.134.210.73`、项目目录为 `/opt/data_record_server`，并使用默认 TCP 端口 `30050`。

## 上传并检查环境

在本地执行：

```bash
rsync -av \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='data/' \
  data_record_server/ root@8.134.210.73:/opt/data_record_server/
ssh root@8.134.210.73
cd /opt/data_record_server
docker --version
docker compose version
```

`data/` 被排除，避免发布代码覆盖已经录制的设备 JSONL 和拒绝连接原始字节归档。

## 准备数据目录并启动

默认 UID/GID 为 `10001:10001`：

```bash
cd /opt/data_record_server
chmod +x scripts/prepare-data-dir.sh
./scripts/prepare-data-dir.sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 collector
```

准备脚本只处理项目内的 `data/`，拒绝 `data/`、`data/devices/` 以及后者下直接 `device-*` 会话目录的符号链接，并使容器非 root 身份可创建 `data/devices/device-<URL-escaped-deviceId>/` 下的归档。请勿把这些路径替换为指向项目外的链接；遇到权限问题也不要把容器改为 root。

如需自定义端口、会话空闲超时、行长上限、保留天数或 UID/GID，在项目根目录创建不提交的 `.env`：

```dotenv
COLLECTOR_UID=1000
COLLECTOR_GID=1000
TCP_PORT=30100
IDLE_TIMEOUT_SECONDS=45
MAX_JSON_LINE_BYTES=65536
RETENTION_DAYS=30
```

随后以相同 UID/GID 准备目录并启动：

```bash
sudo env COLLECTOR_UID=1000 COLLECTOR_GID=1000 scripts/prepare-data-dir.sh
docker compose config --quiet
docker compose up -d --build
```

`MAX_JSON_LINE_BYTES` 默认 `65536`，是单条 JSONL 内容（不含换行符）的上限；它不同于 `READ_BUFFER_BYTES` 的 socket 读取块大小。`RETENTION_DAYS` 默认 `30`，用于清理非活跃归档。所有数值应为正整数，端口必须在 `1` 至 `65535`。Compose 会将宿主机 `/etc/localtime` 只读挂载到容器，使清理时间与服务器本地时区一致。

## 网络放行

在阿里云安全组和宿主机防火墙中，为实际端口创建 TCP 入方向规则，并限制到设备/运维网络的来源 IP。不要长期使用 `0.0.0.0/0`。安全组只允许流量进入，监听由容器中的服务完成。

检查默认配置（`TCP_PORT=30050`）的发布端口：

```bash
docker compose port collector 30050
sudo ss -ltnp | grep ':30050'
```

`docker compose port` 是 Docker 映射的准确信息；`ss` 仅作宿主机辅助观察。

若使用上文示例的 `.env`（`TCP_PORT=30100`），请使用同一个自定义端口，而不是默认端口：

```bash
docker compose port collector 30100
sudo ss -ltnp | grep ':30100'
```

## 验证设备记录

从可访问服务器的客户端向默认端口发送一条符合契约的 JSONL 记录：

```bash
python3 - <<'PY'
import socket

with socket.create_connection(("8.134.210.73", 30050), timeout=5) as sock:
    sock.sendall(b'{"deviceId":"deploy-check","sequence":1}\n')

print("已发送 JSONL 测试记录")
PY
```

若使用上文 `TCP_PORT=30100` 的 `.env` 示例，客户端同样必须使用 `30100`：

```bash
python3 - <<'PY'
import socket

with socket.create_connection(("8.134.210.73", 30100), timeout=5) as sock:
    sock.sendall(b'{"deviceId":"deploy-check","sequence":1}\n')

print("已发送 JSONL 测试记录")
PY
```

回到服务器检查：

```bash
cd /opt/data_record_server
tail -n 50 data/events.jsonl
find data/devices -type f -name '*.jsonl' -print
find data/connections -type f -name '*.bin' -print
first_jsonl=$(find data/devices -type f -name '*.jsonl' -print -quit)
[ -z "$first_jsonl" ] || sed -n '1,20p' "$first_jsonl"
```

预期可看到 `data/devices/device-deploy-check/` 下的 `.jsonl`，内容包含刚发送的完整行；事件通常先出现 `session_started`、`session_attached`。从最后一条有效记录起经过 30 秒（或配置值）后，会依次出现 `session_idle_timeout` 和 `session_closed`。

记录必须是完整 UTF-8 JSONL 行，且顶层 `deviceId` 为非空字符串。同一设备从不同 IP/端口或 TCP 重连提交记录时，在上述空闲窗口内会写入同一会话文件。无效 JSON、缺少/变更 `deviceId`、超长行和断开时未完成行不会进入设备 JSONL；每个出现拒绝记录的 TCP 连接会得到一个 `data/connections/*.bin`，该连接的所有拒绝原始字节都追加到同一个文件。`events.jsonl` 中的 `rejected_connection_started`、`record_rejected` 和 `rejected_connection_closed` 均通过 `file` 字段关联该 `.bin`。

## 日常维护与故障处理

```bash
docker compose ps
docker compose logs --tail=100 collector
docker compose restart collector
docker compose down
```

`data/` 是 bind mount，重启、停止或重建容器不会删除 `data/devices/`、`data/connections/` 或 `events.jsonl`。服务启动后立即清理一次，随后每天服务器本地时间跨越 `00:00` 时清理一次：删除超过 `RETENTION_DAYS` 的非活跃 `devices/**/*.jsonl` 与 `connections/*.bin`，并记录 `retention_cleanup` 事件。`events.jsonl` 永不由该清理任务删除。拒绝连接 `.bin` 没有单连接大小上限，仍须监控磁盘使用量并按需要备份。

若日志出现 `Permission denied`，执行 `docker compose down`，再以 Compose 使用的同一 UID/GID 运行 `scripts/prepare-data-dir.sh` 后重新启动。若客户端得到 `connection refused`，依次检查 `docker compose ps`、服务日志、`docker compose port` 及安全组/防火墙的来源 IP 和端口。
