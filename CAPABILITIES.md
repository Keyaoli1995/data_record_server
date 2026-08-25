# 当前能力与边界

## 服务用途

本项目是长期运行的 TCP JSONL 接收服务。它只保存符合设备记录契约的完整 JSONL 行，并按顶层 `deviceId` 管理设备会话；不是按 TCP 连接保存任意字节流的服务。Docker Compose 默认发布 `30050/TCP`，并使用 `restart: unless-stopped` 恢复异常退出后的容器。

## 已具备的能力

### JSONL 验证与设备会话归档

- 接收 UTF-8 JSONL；一条记录必须以换行符结束、JSON 顶层值为对象，且 `deviceId` 是非空字符串。
- `MAX_JSON_LINE_BYTES` 默认 `65536`，限制一条记录内容（不含换行符）的字节数。`READ_BUFFER_BYTES` 默认 `4096` 仅是读取块大小。
- 每条 TCP 连接在第一个有效记录后绑定一个 `deviceId`；同连接不能切换设备身份。
- 有效完整行不做重编码或 JSON 重排，原样写入权威归档 `data/devices/device-<URL-escaped-deviceId>/*.jsonl`。
- 相同 `deviceId` 的不同 IP、端口和 TCP 重连会附着到同一个活跃设备会话，不会按连接拆分文件。
- 会话从最后一条有效记录起默认 30 秒后关闭；关闭后同设备的下一条有效记录会开始新归档。
- 无效 UTF-8/JSON、非对象、缺少或变更 `deviceId`、超长行和断开时未完成行均不进入归档，只产生 `record_rejected`。

### 生命周期与诊断

`data/events.jsonl` 是带全局锁的 JSON Lines 诊断日志；多线程写入时每行保持完整。事件包括：

| 事件 | 主要字段 | 含义 |
| --- | --- | --- |
| `session_started` | `time`、`file`、`device_id` | 新设备会话文件已创建。 |
| `session_attached` | `time`、`file`、`device_id`、`client_ip`、`client_port` | 一个客户端首次向活跃会话写入有效记录。 |
| `record_rejected` | `time`、`client_ip`、`client_port`、`reason` | 无效记录被拒绝，未创建或写入归档。 |
| `session_idle_timeout` | `time`、`file`、`device_id`、`idle_timeout_seconds`、`total_bytes` | 会话自最后有效记录起空闲到期。 |
| `session_closed` | `time`、`file`、`device_id`、`total_bytes`、`reason` | 会话已关闭。 |

### 安全运行

- 数据通过 `./data:/data` bind mount 保留；升级或重建容器不会删除归档。
- `scripts/prepare-data-dir.sh` 固定在项目目录下准备 `data/`，拒绝 `data/`、`data/devices/` 及其直接 `device-*` 会话目录的符号链接，并在需要时修复归档所有者；应在首次部署和 UID/GID 修改前运行。
- 容器使用非 root 用户、只读根文件系统、`no-new-privileges` 和空 capability 集合。不要通过 root 身份绕开宿主机目录权限。
- 服务不提供 TLS、客户端认证、速率限制、连接总数上限或自动保留清理；安全组和宿主机防火墙必须仅放行必要来源，运维方必须监控并备份数据。

## 并发模型与容量边界

每个活跃 TCP 连接使用一个 Python 线程。设备归档与事件日志分别在必要的锁保护下写入，以避免同一活跃设备会话的 JSONL 行交错。可承载数量受文件描述符、线程/内存、磁盘容量与 IOPS、上报频率和系统限制共同约束；尚未对大规模并发给出压测容量承诺。

## 当前不具备的能力

- 不接受未按此 JSONL 契约编码或没有完整 JSONL 行的数据作为归档。
- 不接受没有非空顶层 `deviceId` 的记录，也不允许一条 TCP 连接切换设备 ID。
- 不提供 Web 页面、数据库、检索接口、实时展示、TLS、客户端认证或应用层 ACK。
- 不自动删除历史 `.jsonl`，不限制单设备总数据量或最大活跃客户端数。

## 运行状态检查

```bash
docker compose ps
docker compose logs --tail=100 collector
find data/devices -type f -name '*.jsonl' -printf '%TY-%Tm-%Td %TH:%TM:%TS %s %p\n'
tail -n 50 data/events.jsonl
docker compose exec collector sh -c 'ulimit -n; cat /proc/self/limits'
```

完整步骤见 [README.md](README.md) 与 [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)。
