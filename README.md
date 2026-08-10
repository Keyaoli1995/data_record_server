# TCP 原始字节流采集器

本服务用于接收未知协议的 TCP 数据，并按每个连接**原样保存**收到的字节流。它不解析协议、不添加分隔符或编码转换，也不会发送应用层 ACK。

- `data/connections/*.bin` 是原始数据的权威副本，后续协议分析应以它为准。
- `data/events.jsonl` 是诊断日志，记录连接生命周期和每次 `recv()` 读取的摘要；其中的十六进制数据便于排查，但不替代 `.bin`。
- TCP 内核本身仍会处理正常的传输层确认；这里“不发送 ACK”是指服务不会猜测未知协议并回复应用层报文。

## 前置条件

- 本地测试：Python 3 和 `pip`；开发依赖为 PyYAML。
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

如果需要使用自定义 UID/GID，**准备脚本和 Compose 构建/启动必须使用完全相同的值**。例如使用本机 `1000:1000`：

```bash
sudo env COLLECTOR_UID=1000 COLLECTOR_GID=1000 scripts/prepare-data-dir.sh
COLLECTOR_UID=1000 COLLECTOR_GID=1000 docker compose up -d --build
```

不要跳过此步骤：容器以非 root 身份写入 `/data`，错误的目录所有权会导致无法保存采集结果。

## 启动与连接配置

使用默认端口启动：

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 collector
```

服务在容器中监听 `0.0.0.0:30050`，默认将主机 TCP `30050` 发布到容器。App 的服务器地址设置为 `8.134.210.73`，TCP 端口设置为 `30050`。

使用其他端口时，在启动命令前同时设置 `TCP_PORT`；例如：

```bash
TCP_PORT=30100 docker compose up -d --build
```

此时服务监听并发布 `30100/TCP`。阿里云安全组和 App 端口也必须一同改为 `30100`，否则无法从公网连接。

## 查看状态与数据

```bash
# 容器状态与最近日志
docker compose ps
docker compose logs --tail=100 collector

# 主机是否在默认端口监听（改端口后把 30050 替换为实际端口）
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

# 安全查看第一份原始文件的前 256 字节；没有文件时不调用 xxd
first_bin=$(find data/connections -maxdepth 1 -type f -name '*.bin' -print -quit 2>/dev/null)
if [ -n "$first_bin" ]; then
  xxd -g 1 -l 256 "$first_bin"
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

一次 TCP 连接对应一个 `.bin` 文件，文件内容按接收顺序连续追加。`received` 事件对应的是 TCP `recv()` 的读取块，**不是**协议帧：同一协议帧可能被拆成多块，多个协议帧也可能合并到一块。请根据 `.bin` 的完整字节序列进行协议分析。

## 重启、停止与备份

```bash
docker compose restart collector
docker compose down
```

`data/` 是 bind mount，重启、停止或重新创建容器都不会删除其中的数据。删除、移动或清理 `data/` 前，请先在宿主机备份原始文件和 `events.jsonl`。

## 安全注意事项

- 原始数据可能含设备标识、位置或其他敏感信息。限制 `data/` 的访问权限，按保留策略备份和清理，不要将其提交到 Git。
- 阿里云安全组应尽可能只放行必要的来源 IP 和 TCP 端口，不要对所有来源长期开放。
- 容器以非 root 用户运行，并启用了只读根文件系统、最小 capability 和 `no-new-privileges`；不要通过改为 root 来绕过目录权限问题。
- 因为协议未知，服务不发送应用层 ACK。若设备必须收到特定业务响应，需先基于采集到的 `.bin` 完成协议确认后再单独实现。

## 故障排查

### 写入时出现 `Permission denied`

先停止容器，然后以与 Compose 相同的 UID/GID 重新准备目录。默认值：

```bash
sudo scripts/prepare-data-dir.sh
docker compose up -d --build
```

自定义 UID/GID 时，两个命令都带相同变量，例如：

```bash
sudo env COLLECTOR_UID=1000 COLLECTOR_GID=1000 scripts/prepare-data-dir.sh
COLLECTOR_UID=1000 COLLECTOR_GID=1000 docker compose up -d --build
```

### App 显示 `connection refused`

通常表示目标主机可达，但端口没有监听或没有正确发布。检查：

```bash
docker compose ps
docker compose logs --tail=100 collector
sudo ss -ltnp | grep ':30050'
```

### App 连接超时

通常表示网络路径被拦截或地址/端口错误。确认 App 指向 `8.134.210.73:30050`（或实际自定义端口），并检查阿里云安全组是否放行对应 TCP 端口及所需来源 IP；随后再检查上述 `docker compose` 日志和 `ss` 输出。

## 部署验证说明

当前 WSL 环境无法执行 Docker 命令（Docker CLI 不可用），因此没有在此环境进行容器启动、`docker compose config` 或镜像构建验证。请在目标阿里云服务器完成目录准备后执行：

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

再从 App 发起真实 TCP 连接，并确认 `data/connections/` 出现对应 `.bin` 文件、`data/events.jsonl` 出现同名文件的 `connected`、`received` 和 `disconnected` 事件。
