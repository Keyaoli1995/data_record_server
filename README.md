# TCP 原始字节流采集器

本服务用于接收未知协议的 TCP 数据，并按每个连接**原样保存**收到的字节流。它不解析协议、不添加分隔符或编码转换，也不会发送应用层 ACK。

- `data/connections/*.bin` 是原始数据的权威副本，后续协议分析应以它为准。
- `data/events.jsonl` 是诊断日志，记录连接生命周期和每次 `recv()` 读取的摘要；其中的十六进制数据便于排查，但不替代 `.bin`。
- TCP 内核本身仍会处理正常的传输层确认；这里“不发送 ACK”是指服务不会猜测未知协议并回复应用层报文。

## 前置条件

- 本地测试：Python 3.9+ 和 `pip`（建议使用与镜像一致的 Python 3.12）；开发依赖为 PyYAML。某些 Linux 发行版还需要安装 `python3-venv` 软件包才能创建虚拟环境。
- 部署：目标服务器安装 Docker Engine 和 Docker Compose（`docker compose`）。

## 本地测试

建议使用虚拟环境，避免将测试依赖安装到系统 Python：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

## 部署前：准备 bind mount 目录

**每次首次部署或修改容器 UID/GID 前，必须先执行：**

```bash
sudo scripts/prepare-data-dir.sh
```

脚本固定操作本项目根目录下的 `data/`，而不是调用命令时所在目录。默认容器身份为 UID/GID `10001:10001`；脚本会创建目录、限制其权限，并在有权限时修正既有文件的所有者。它拒绝将 `data/` 作为符号链接，且不会递归跟随符号链接，因此不会因错误的工作目录或链接而修改项目外路径。

## 自定义 UID/GID 或端口

默认部署不需要 `.env`：使用上一节的 `sudo scripts/prepare-data-dir.sh`，再运行 `docker compose up -d --build` 即可，默认值为 `10001:10001` 和 TCP `30050`。

若要自定义 UID/GID 或端口，请在项目根目录创建仅供本机/服务器使用的 `.env`（不要提交该文件）。例如使用本机 `1000:1000` 和 TCP `30100`：

```dotenv
COLLECTOR_UID=1000
COLLECTOR_GID=1000
TCP_PORT=30100
```

Compose 会自动读取项目根目录的 `.env`，让构建参数、容器 `user`、端口映射和容器内 `TCP_PORT` 使用同一组值。但 `sudo` 不会自动读取它；在**同一个终端会话**中按以下顺序加载并只将已校验的 UID/GID 显式传给脚本：

```bash
# 只 source 由本项目管理员创建和审阅过的 .env，不要 source 不可信文件。
set -a
. ./.env
set +a

case "$COLLECTOR_UID" in '' | *[!0-9]*) echo 'COLLECTOR_UID 必须是非负十进制整数'; exit 1;; esac
case "$COLLECTOR_GID" in '' | *[!0-9]*) echo 'COLLECTOR_GID 必须是非负十进制整数'; exit 1;; esac
case "$TCP_PORT" in '' | *[!0-9]*) echo 'TCP_PORT 必须是十进制整数'; exit 1;; esac

sudo env COLLECTOR_UID="$COLLECTOR_UID" COLLECTOR_GID="$COLLECTOR_GID" scripts/prepare-data-dir.sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

之后在该会话运行的所有 `docker compose config`、`up`、`build` 和 `ps` 命令都会使用相同的导出变量；即使打开新终端，Compose 也会自动从 `.env` 读取它们。不要跳过目录准备步骤：容器以非 root 身份写入 `/data`，错误的目录所有权会导致无法保存采集结果。

## 启动与连接配置

使用默认端口启动：

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 collector
```

服务在容器中监听 `0.0.0.0:30050`，默认将主机 TCP `30050` 发布到容器。App 的服务器地址设置为 `8.134.210.73`，TCP 端口设置为 `30050`。

使用非默认端口时，请按上一节创建 `.env`，而不要只在单条命令前临时设置 `TCP_PORT`。例如 `.env` 中的 `TCP_PORT=30100` 会使服务监听并发布 `30100/TCP`。阿里云安全组和 App 端口也必须一同改为 `30100`，否则无法从公网连接。

## 查看状态与数据

```bash
# 容器状态与最近日志
docker compose ps
docker compose logs --tail=100 collector

# Compose 看到的已发布端口；自定义 .env 时请先按上一节在当前终端加载它
docker compose port collector "${TCP_PORT:-30050}"

# 从另一台可访问公网的客户端实际探测。需要安装 netcat/nc。
nc -vz 8.134.210.73 "${TCP_PORT:-30050}"

# ss 只作为宿主机的补充观察，不能单独证明 Docker 端口映射正确
sudo ss -ltnp | grep ":${TCP_PORT:-30050}"

# 末尾事件；尚未有数据时给出提示
if [ -f data/events.jsonl ]; then
  tail -n 50 data/events.jsonl
else
  echo '尚未生成 data/events.jsonl'
fi

# 列出已保存的原始文件；尚未有连接时给出提示
if [ -d data/connections ]; then
  find data/connections -maxdepth 1 -type f -name '*.bin' -print
else
  echo '尚未生成 data/connections/'
fi

# 查看第一份原始文件的前 256 字节；xxd 是可选工具，缺少时回退到 od
first_bin=$(find data/connections -maxdepth 1 -type f -name '*.bin' -print -quit 2>/dev/null)
if [ -n "$first_bin" ]; then
  if command -v xxd >/dev/null 2>&1; then
    xxd -g 1 -l 256 "$first_bin"
  else
    od -Ax -tx1 -N 256 "$first_bin"
  fi
else
  echo '没有可查看的 .bin 文件'
fi
```

`events.jsonl` 每行都是一个独立 JSON 对象，常见字段如下：

| `event` 值 | 字段 | 含义 |
| --- | --- | --- |
| `connected` | `time`、`file`、`client_ip`、`client_port` | 已建立连接及对应的原始文件。 |
| `received` | `time`、`file`、`bytes`、`hex` | 一次 TCP `recv()` 的读取长度和十六进制内容。 |
| `disconnected` | `time`、`file`、`total_bytes` | 连接结束及该文件累计字节数。 |
| `error` | `time`、`file`、`error_type`、`error` | 该连接处理期间的异常摘要。 |

一次 TCP 连接对应一个 `.bin` 文件，文件内容按接收顺序连续追加。`received` 事件对应的是 TCP `recv()` 的读取块，**不是**协议帧：同一协议帧可能被拆成多块，多个协议帧也可能合并到一块。请根据 `.bin` 的完整字节序列进行协议分析。`disconnected` 只有在客户端/App 主动关闭长连接、服务端关闭连接，或连接发生错误而结束后才会写入；长时间保持连接时，文件和 `received` 事件会继续增长而暂时没有该事件。

## 重启、停止与备份

```bash
docker compose restart collector
docker compose down
```

`data/` 是 bind mount，重启、停止或重新创建容器都不会删除其中的数据。删除、移动或清理 `data/` 前，请先在宿主机备份原始文件和 `events.jsonl`。

## 安全注意事项

- 原始数据可能含设备标识、位置或其他敏感信息。限制 `data/` 的访问权限，按保留策略备份和清理，不要将其提交到 Git。
- 本服务未提供 TLS、客户端认证、速率限制、连接数/容量限制，也没有自动数据保留清理。阿里云安全组和宿主机防火墙的来源 IP 与端口 allowlist 是当前的访问控制边界；不要对所有来源长期开放。
- 监控并告警磁盘使用量，制定明确的轮转/清理和备份流程；不要依赖服务自动删除旧数据。
- 容器以非 root 用户运行，并启用了只读根文件系统、最小 capability 和 `no-new-privileges`；不要通过改为 root 来绕过目录权限问题。
- 因为协议未知，服务不发送应用层 ACK。若设备必须收到特定业务响应，需先基于采集到的 `.bin` 完成协议确认后再单独实现。

## 故障排查

### 写入时出现 `Permission denied`

先停止容器，然后以与 Compose 相同的 UID/GID 重新准备目录。默认值：

```bash
docker compose down
sudo scripts/prepare-data-dir.sh
docker compose up -d --build
```

自定义 UID/GID 时，保留现有 `.env`，并在同一终端加载它后再恢复目录权限和启动：

```bash
docker compose down
set -a
. ./.env
set +a
case "$COLLECTOR_UID" in '' | *[!0-9]*) echo 'COLLECTOR_UID 必须是非负十进制整数'; exit 1;; esac
case "$COLLECTOR_GID" in '' | *[!0-9]*) echo 'COLLECTOR_GID 必须是非负十进制整数'; exit 1;; esac
sudo env COLLECTOR_UID="$COLLECTOR_UID" COLLECTOR_GID="$COLLECTOR_GID" scripts/prepare-data-dir.sh
docker compose up -d --build
```

### App 显示 `connection refused`

通常表示目标主机可达，但端口没有监听或没有正确发布。检查：

```bash
docker compose ps
docker compose logs --tail=100 collector
docker compose port collector "${TCP_PORT:-30050}"
sudo ss -ltnp | grep ":${TCP_PORT:-30050}"
```

### App 连接超时

通常表示网络路径被拦截或地址/端口错误。确认 App 指向 `8.134.210.73:30050`（或实际自定义端口），并检查阿里云安全组是否放行对应 TCP 端口及所需来源 IP；随后再检查上述 `docker compose` 日志和 `ss` 输出。

## 部署验证说明

当前 WSL 环境无法执行 Docker 命令（Docker CLI 不可用），因此没有在此环境进行容器启动、`docker compose config` 或镜像构建验证。请在目标阿里云服务器完成目录准备后执行。若使用自定义设置，请保留上一节的 `.env`，并在同一终端先加载/校验它，再执行以下命令；不要临时省略 UID/GID 或端口：

```bash
# 自定义 .env 时，先按“自定义 UID/GID 或端口”章节加载变量。
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose port collector "${TCP_PORT:-30050}"
```

再从外部网络执行 `nc -vz 8.134.210.73 "${TCP_PORT:-30050}"`，并从 App 发起真实 TCP 连接。确认 `data/connections/` 出现对应 `.bin` 文件；在 App 关闭该连接后，`data/events.jsonl` 应出现同名文件的 `connected`、`received` 和 `disconnected` 事件。
