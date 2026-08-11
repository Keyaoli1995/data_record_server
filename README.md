# TCP 原始字节流采集器

本服务用于接收未知协议的 TCP 数据，并按每个连接**原样保存**收到的字节流。它不解析协议、不添加分隔符或编码转换，也不会发送应用层 ACK。

- `data/connections/*.bin` 是原始数据的权威副本，后续协议分析应以它为准。
- `data/events.jsonl` 是诊断日志，只记录连接、超时、断开和错误等生命周期事件；不保存每次 `recv()` 的数据摘要。
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

## 自定义 UID/GID、端口或空闲超时

默认部署不需要 `.env`：使用上一节的 `sudo scripts/prepare-data-dir.sh`，再运行 `docker compose up -d --build` 即可，默认值为 `10001:10001`、TCP `30050` 和 30 秒空闲超时。

若要自定义 UID/GID、端口或空闲超时，请在项目根目录创建仅供本机/服务器使用的 `.env`（不要提交该文件）。即使只修改其中一个值，`.env` 也必须完整定义 `COLLECTOR_UID`、`COLLECTOR_GID`、`TCP_PORT` 和 `IDLE_TIMEOUT_SECONDS` 四项。以下是使用本机 `1000:1000`、TCP `30100` 和 45 秒空闲超时的完整示例：

```dotenv
COLLECTOR_UID=1000
COLLECTOR_GID=1000
TCP_PORT=30100
IDLE_TIMEOUT_SECONDS=45
```

Compose 会自动读取项目根目录的 `.env`，让构建参数、容器 `user`、端口映射和容器内 `TCP_PORT` 使用同一组值。但 `sudo` 不会自动读取它。

### 仅加载并校验 `.env`（无副作用）

每次进行自定义准备、启动、状态检查或排障时，都在**同一个终端会话**中先运行下面的片段。它只读取、解析和校验变量；不会 `source` 或执行 `.env` 内容，也不会调用 `sudo`、Docker、网络或修改目录。

```bash
# 安全读取唯一且非空的 KEY=value；不 source/eval .env 内容。
read_dotenv_value() {
  awk -F= -v key="$1" '
    $1 == key { count++; value = substr($0, length(key) + 2) }
    END {
      if (count != 1 || value == "") exit 1
      print value
    }
  ' .env
}

COLLECTOR_UID=$(read_dotenv_value COLLECTOR_UID) || { echo '缺少或重复 COLLECTOR_UID'; exit 1; }
COLLECTOR_GID=$(read_dotenv_value COLLECTOR_GID) || { echo '缺少或重复 COLLECTOR_GID'; exit 1; }
TCP_PORT=$(read_dotenv_value TCP_PORT) || { echo '缺少或重复 TCP_PORT'; exit 1; }
IDLE_TIMEOUT_SECONDS=$(read_dotenv_value IDLE_TIMEOUT_SECONDS) || { echo '缺少或重复 IDLE_TIMEOUT_SECONDS'; exit 1; }

case "$COLLECTOR_UID" in '' | *[!0-9]*) echo 'COLLECTOR_UID 必须是正十进制整数'; exit 1;; esac
case "$COLLECTOR_GID" in '' | *[!0-9]*) echo 'COLLECTOR_GID 必须是正十进制整数'; exit 1;; esac
case "$TCP_PORT" in '' | *[!0-9]*) echo 'TCP_PORT 必须是十进制整数'; exit 1;; esac
case "$IDLE_TIMEOUT_SECONDS" in '' | *[!0-9]*) echo 'IDLE_TIMEOUT_SECONDS 必须是正十进制整数'; exit 1;; esac
if ! [ "$COLLECTOR_UID" -gt 0 ] 2>/dev/null; then echo 'COLLECTOR_UID 不能为 root（必须大于 0）'; exit 1; fi
if ! [ "$COLLECTOR_GID" -gt 0 ] 2>/dev/null; then echo 'COLLECTOR_GID 不能为 root（必须大于 0）'; exit 1; fi
if ! [ "$TCP_PORT" -ge 1 ] 2>/dev/null || ! [ "$TCP_PORT" -le 65535 ] 2>/dev/null; then
  echo 'TCP_PORT 必须在 1 到 65535 之间'
  exit 1
fi
if ! [ "$IDLE_TIMEOUT_SECONDS" -gt 0 ] 2>/dev/null; then
  echo 'IDLE_TIMEOUT_SECONDS 必须大于 0'
  exit 1
fi
```

### 使用校验值准备并启动

在刚运行完上面的无副作用片段的同一终端中，执行：

```bash
sudo env COLLECTOR_UID="$COLLECTOR_UID" COLLECTOR_GID="$COLLECTOR_GID" scripts/prepare-data-dir.sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

在该会话中，准备脚本收到的 UID/GID 与 `.env` 中 Compose 使用的值一致；`$TCP_PORT` 和 `$IDLE_TIMEOUT_SECONDS` 也已通过校验。即使打开新终端，Compose 也会自动从 `.env` 读取配置；但任何需要 shell 展开这些变量的命令仍要先重新运行**仅加载并校验 `.env`（无副作用）**片段。不要跳过目录准备步骤：容器以非 root 身份写入 `/data`，错误的目录所有权会导致无法保存采集结果。

## 启动与连接配置

使用默认端口启动：

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 collector
```

服务在容器中监听 `0.0.0.0:30050`，默认将主机 TCP `30050` 发布到容器。App 的服务器地址设置为 `8.134.210.73`，TCP 端口设置为 `30050`。

每条 TCP 连接连续 30 秒未收到任何数据时，服务会主动关闭该连接；每次收到数据都会重新开始计时。对于当前约 1 秒一次的设备回传频率，30 秒允许短暂网络波动，同时会结束设备突然断电留下的旧连接。若设备存在更长的正常静默期，请在 `.env` 中将 `IDLE_TIMEOUT_SECONDS` 设置为大于该静默期的正整数，然后重启服务。

使用非默认端口时，请按上一节创建 `.env`，而不要只在单条命令前临时设置 `TCP_PORT`。例如 `.env` 中的 `TCP_PORT=30100` 会使服务监听并发布 `30100/TCP`。阿里云安全组和 App 端口也必须一同改为 `30100`，否则无法从公网连接。

## 查看状态与数据

```bash
# 容器状态与最近日志
docker compose ps
docker compose logs --tail=100 collector

# 默认端口：Compose 看到的已发布端口
docker compose port collector 30050

# 默认端口下，ss 只作为宿主机的补充观察，不能单独证明 Docker 端口映射正确
sudo ss -ltnp | grep ':30050'

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

自定义端口命令会使用 shell 展开的 `TCP_PORT`。Compose 会自行读取 `.env`，但 shell 不会；在服务器当前终端、并且**紧接在执行自定义端口命令前**只运行“仅加载并校验 `.env`（无副作用）”片段，然后执行：

```bash
docker compose port collector "$TCP_PORT"
sudo ss -ltnp | grep ":$TCP_PORT"
```

从另一台可访问公网的客户端实际探测时，客户端不能读取服务器的 `.env`，必须填写已经配置的实际端口（需安装 netcat/nc）：

```bash
# 默认端口
nc -vz 8.134.210.73 30050

# 自定义示例：服务器 .env 中为 TCP_PORT=30100
nc -vz 8.134.210.73 30100
```

请将示例端口替换为服务器当前配置的实际端口。

`events.jsonl` 每行都是一个独立 JSON 对象，常见字段如下：

| `event` 值 | 字段 | 含义 |
| --- | --- | --- |
| `connected` | `time`、`file`、`client_ip`、`client_port` | 已建立连接及对应的原始文件。 |
| `idle_timeout` | `time`、`file`、`idle_timeout_seconds`、`total_bytes` | 该连接在设定秒数内未收到数据，服务主动结束前的超时记录。 |
| `disconnected` | `time`、`file`、`total_bytes` | 连接结束及该文件累计字节数。 |
| `error` | `time`、`file`、`error_type`、`error` | 该连接处理期间的异常摘要。 |

一次 TCP 连接对应一个 `.bin` 文件，文件内容按接收顺序连续追加。TCP 没有协议帧边界：同一协议帧可能被拆成多次读取，多个协议帧也可能合并到一次读取；请根据 `.bin` 的完整字节序列进行协议分析。正常客户端/App 主动关闭、服务端关闭或连接错误结束时会写入 `disconnected`；因空闲超时关闭时，事件顺序为 `idle_timeout` 后紧跟 `disconnected`。长时间保持连接且持续收到数据时，`.bin` 会继续增长，直到连接结束或超时。

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

自定义 UID/GID 时，先停止服务。`docker compose down` 可自行读取 `.env`，因此无需先加载变量：

```bash
docker compose down
```

然后在**同一终端**只运行“仅加载并校验 `.env`（无副作用）”片段；它完成后，再执行目录修复与启动：

```bash
sudo env COLLECTOR_UID="$COLLECTOR_UID" COLLECTOR_GID="$COLLECTOR_GID" scripts/prepare-data-dir.sh
docker compose up -d --build
```

### App 显示 `connection refused`

通常表示目标主机可达，但端口没有监听或没有正确发布。检查：

```bash
docker compose ps
docker compose logs --tail=100 collector

# 默认端口；自定义 .env 时，改用紧随本代码块之后的加载/校验命令。
docker compose port collector 30050
sudo ss -ltnp | grep ':30050'
```

自定义端口时，在服务器当前终端、紧接在端口检查前只运行“仅加载并校验 `.env`（无副作用）”片段，然后执行：

```bash
docker compose port collector "$TCP_PORT"
sudo ss -ltnp | grep ":$TCP_PORT"
```

### App 连接超时

通常表示网络路径被拦截或地址/端口错误。确认 App 指向 `8.134.210.73:30050`（或实际自定义端口），并检查阿里云安全组是否放行对应 TCP 端口及所需来源 IP；随后再检查上述 `docker compose` 日志和 `ss` 输出。

## 部署验证说明

当前 WSL 环境无法执行 Docker 命令（Docker CLI 不可用），因此没有在此环境进行容器启动、`docker compose config` 或镜像构建验证。请在目标阿里云服务器完成目录准备后执行。若使用自定义设置，请保留上一节的 `.env`，并在同一终端先只运行“仅加载并校验 `.env`（无副作用）”片段，再执行以下命令；不要临时省略 UID/GID、端口或空闲超时：

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

默认端口的服务器端发布检查：

```bash
docker compose port collector 30050
```

自定义端口的服务器端发布检查，必须先在当前 shell 只运行“仅加载并校验 `.env`（无副作用）”片段，再展开 `$TCP_PORT`：

```bash
docker compose port collector "$TCP_PORT"
```

再从外部网络以实际端口探测并从 App 发起真实 TCP 连接：默认端口使用 `nc -vz 8.134.210.73 30050`；如果 `.env` 中为 `TCP_PORT=30100`，则使用 `nc -vz 8.134.210.73 30100`。外部客户端不能读取服务器 `.env`，请将示例端口替换为当前实际值。确认 `data/connections/` 出现对应 `.bin` 文件；在 App 关闭该连接后，`data/events.jsonl` 应出现同名文件的 `connected` 和 `disconnected` 事件。
