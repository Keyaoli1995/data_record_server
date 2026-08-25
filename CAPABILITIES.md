# 当前能力与边界

## 服务用途

本项目是长期运行的 TCP JSONL 接收服务。符合设备记录契约的完整 JSONL 行按顶层 `deviceId` 管理设备会话；发生拒绝时，服务仅保留该 TCP 连接中被拒绝的原始字节以供诊断，并不把任意连接字节当作设备数据归档。Docker Compose 默认发布 `30050/TCP`，并使用 `restart: unless-stopped` 恢复异常退出后的容器。

## 已具备的能力

### JSONL 验证与设备会话归档

- 接收 UTF-8 JSONL；一条记录必须以换行符结束、JSON 顶层值为对象，且 `deviceId` 是非空字符串。
- `MAX_JSON_LINE_BYTES` 默认 `65536`，限制一条记录内容（不含换行符）的字节数。`READ_BUFFER_BYTES` 默认 `4096` 仅是读取块大小。
- 每条 TCP 连接在第一个有效记录后绑定一个 `deviceId`；同连接不能切换设备身份。
- 有效完整行不做重编码或 JSON 重排，原样写入权威归档 `data/devices/device-<URL-escaped-deviceId>/*.jsonl`。
- 相同 `deviceId` 的不同 IP、端口和 TCP 重连会附着到同一个活跃设备会话，不会按连接拆分文件。
- 会话从最后一条有效记录起默认 30 秒后关闭；关闭后同设备的下一条有效记录会开始新归档。
- 无效 UTF-8/JSON、非对象、缺少或变更 `deviceId`、超长行和断开时未完成行均不进入设备 JSONL 归档。

### 拒绝连接诊断归档与保留清理

- 每个出现拒绝记录的 TCP 连接都会创建一个 `data/connections/*.bin`，同一连接的全部拒绝原始字节按接收顺序追加到同一个文件。
- `RETENTION_DAYS` 默认 `30`；服务启动后清理一次，之后每当服务器本地时间跨越 `00:00` 清理一次。清理只删除超过保留期的非活跃 `data/devices/**/*.jsonl` 和 `data/connections/*.bin`，并会删除清空的设备目录。
- `data/events.jsonl` 是诊断日志，不受保留清理删除；`retention_cleanup` 事件记录每次清理的触发原因和统计数据。

### 生命周期与诊断

`data/events.jsonl` 是带全局锁的 JSON Lines 诊断日志；多线程写入时每行保持完整。事件包括：

| 事件 | 主要字段 | 含义 |
| --- | --- | --- |
| `session_started` | `time`、`file`、`device_id` | 新设备会话文件已创建。 |
| `session_attached` | `time`、`file`、`device_id`、`client_ip`、`client_port` | 一个客户端首次向活跃会话写入有效记录。 |
| `rejected_connection_started` | `time`、`file`、`client_ip`、`client_port` | 已创建拒绝连接 `.bin`；`file` 关联该连接的字节归档。 |
| `record_rejected` | `time`、`file`、`client_ip`、`client_port`、`reason` | 无效记录被拒绝，未写入设备 JSONL；`file` 关联所属连接 `.bin`。 |
| `rejected_connection_closed` | `time`、`file`、`total_bytes`、`reason` | 拒绝连接诊断归档已关闭。 |
| `session_idle_timeout` | `time`、`file`、`device_id`、`idle_timeout_seconds`、`total_bytes` | 会话自最后有效记录起空闲到期。 |
| `session_closed` | `time`、`file`、`device_id`、`total_bytes`、`reason` | 会话已关闭。 |
| `retention_cleanup` | `time`、`trigger`、`deleted_files`、`reclaimed_bytes`、`failed_files` | 已完成一次保留清理。 |

### 安全运行

- 数据通过 `./data:/data` bind mount 保留；升级或重建容器不会删除归档。`/etc/localtime` 以只读方式挂载，使容器的每日保留清理与服务器本地时区同步。
- `scripts/prepare-data-dir.sh` 固定在项目目录下准备 `data/`，拒绝 `data/`、`data/devices/` 及其直接 `device-*` 会话目录的符号链接，并在需要时修复归档所有者；应在首次部署和 UID/GID 修改前运行。
- 容器使用非 root 用户、只读根文件系统、`no-new-privileges` 和空 capability 集合。不要通过 root 身份绕开宿主机目录权限。
- 服务不提供 TLS、客户端认证、速率限制或连接总数上限；安全组和宿主机防火墙必须仅放行必要来源，运维方必须监控并备份数据。

## 并发模型与容量边界

每个活跃 TCP 连接使用一个 Python 线程。设备归档与事件日志分别在必要的锁保护下写入，以避免同一活跃设备会话的 JSONL 行交错。可承载数量受文件描述符、线程/内存、磁盘容量与 IOPS、上报频率和系统限制共同约束；尚未对大规模并发给出压测容量承诺。

## 当前不具备的能力

- 不接受未按此 JSONL 契约编码或没有完整 JSONL 行的数据作为归档。
- 不接受没有非空顶层 `deviceId` 的记录，也不允许一条 TCP 连接切换设备 ID。
- 不提供 Web 页面、数据库、检索接口、实时展示、TLS、客户端认证或应用层 ACK。
- 不限制单设备总数据量、拒绝连接单个 `.bin` 的大小或最大活跃客户端数；即使启用保留清理，仍须监控磁盘容量。

## 运行状态检查

```bash
docker compose ps
docker compose logs --tail=100 collector
find data/devices -type f -name '*.jsonl' -printf '%TY-%Tm-%Td %TH:%TM:%TS %s %p\n'
tail -n 50 data/events.jsonl
docker compose exec collector sh -c 'ulimit -n; cat /proc/self/limits'
```

完整步骤见 [README.md](README.md) 与 [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)。
