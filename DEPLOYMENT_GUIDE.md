可以。按当前项目的默认配置，部署后数据路径是：

```text
App
  → 8.134.210.73:30050
  → 阿里云安全组
  → Docker 端口映射
  → TCP 接收程序
  → /opt/data_record_server/data/
```

下面按“在哪执行、执行什么、有什么作用”说明。

## 第一步：把项目上传到服务器

在你本地电脑执行，不是在服务器上执行：

```bash
cd /home/keyaoli/Code/Wayrobo/manual_raking_trajectory

scp -r data_record_server root@8.134.210.73:/opt/
```

输入服务器密码后，项目会被复制到：

```text
/opt/data_record_server
```

含义：服务器必须拿到程序源码、`Dockerfile` 和 `compose.yaml`，才能构建并运行接收服务。

如果以后只想同步修改后的代码，可以使用 `rsync`：

```bash
rsync -av \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='data/' \
  data_record_server/ \
  root@8.134.210.73:/opt/data_record_server/
```

排除 `data/` 是为了防止覆盖服务器已经采集的数据。

## 第二步：登录服务器并进入项目目录

在本地执行：

```bash
ssh root@8.134.210.73
```

登录后执行：

```bash
cd /opt/data_record_server
pwd
ls
```

正常应该能看到：

```text
Dockerfile
README.md
compose.yaml
data_record_server
scripts
tests
```

后续命令都默认在这个目录执行。

## 第三步：检查 Docker 环境

```bash
docker --version
docker compose version
```

两个命令都能显示版本号，才具备直接部署条件。

它们的区别是：

- `docker`：负责构建和运行容器。
- `docker compose`：读取 `compose.yaml`，统一配置端口、目录、重启策略等。

如果第二条提示 `docker: 'compose' is not a docker command`，暂时不要继续，把输出发给我，需要根据服务器系统安装 Compose 插件。

## 第四步：确认端口配置

当前项目默认使用：

```text
TCP 30050
```

所以现在不需要创建 `.env` 文件。`compose.yaml` 中已经把默认端口设为 `30050`。

阿里云安全组需要有入方向规则：

```text
协议：TCP
端口：30050
来源：测试时可临时使用 0.0.0.0/0
```

安全组只是允许网络流量进入服务器，并不负责监听端口。真正的监听由后面启动的 TCP 程序完成。

## 第五步：准备数据保存目录

你使用的是 `root`，在服务器项目目录执行：

```bash
chmod +x scripts/prepare-data-dir.sh
./scripts/prepare-data-dir.sh
```

检查结果：

```bash
ls -ldn data data/connections
```

这里主要做三件事：

- 创建 `data/` 和 `data/connections/`。
- 设置合理的文件权限。
- 将目录所有者设置为容器使用的 `10001:10001`。

为什么要设置这个权限：容器里的 TCP 程序不是以 `root` 运行，而是以 UID/GID `10001:10001` 运行。如果目录不可写，App 虽然可能连接成功，但数据保存会失败。

## 第六步：检查 Compose 配置

```bash
docker compose config --quiet
```

含义：让 Docker 检查 `compose.yaml` 的语法和变量是否有效。

正常情况下没有输出，返回到命令提示符就是成功。如果有错误，先不要启动，把错误信息发给我。

你也可以查看展开后的完整配置：

```bash
docker compose config
```

其中应该能看到类似端口映射：

```text
30050:30050
```

左侧是服务器端口，右侧是容器内部端口。

## 第七步：构建并启动接收程序

```bash
docker compose up -d --build
```

各参数含义：

- `up`：创建并启动服务。
- `--build`：根据 `Dockerfile` 重新构建程序镜像。
- `-d`：在后台运行，退出 SSH 后程序也不会停止。

第一次运行可能会下载 Python 基础镜像，需要稍等一会。

这个命令还会自动完成：

- 启动 TCP 接收程序。
- 监听容器内的 `0.0.0.0:30050`。
- 将服务器的 `30050` 映射到容器的 `30050`。
- 将服务器的 `./data` 挂载到容器的 `/data`。
- 配置异常退出后自动重启。

## 第八步：检查是否启动成功

先看容器状态：

```bash
docker compose ps
```

正常状态应该是 `Up` 或 `running`，并能看到类似：

```text
0.0.0.0:30050->30050/tcp
```

查看程序日志：

```bash
docker compose logs --tail=100 collector
```

确认 Docker 端口映射：

```bash
docker compose port collector 30050
```

预期类似：

```text
0.0.0.0:30050
```

还可以辅助检查：

```bash
ss -ltnp | grep ':30050'
```

但 Docker 某些网络模式下，`ss` 不一定显示 `docker-proxy`，所以应结合 `docker compose ps` 和公网连接测试判断，不能只看 `ss`。

## 第九步：从另一台电脑测试公网连接

回到你的本地电脑执行：

```bash
nc -vz 8.134.210.73 30050
```

成功时通常显示：

```text
Connection to 8.134.210.73 30050 port [tcp/*] succeeded!
```

这能证明以下链路已经连通：

```text
本地电脑 → 公网 → 阿里云安全组 → 服务器 → Docker → 接收程序
```

如果电脑没有 `nc`，可用 Python 测试，并发送一段测试数据：

```bash
python3 - <<'PY'
import socket

with socket.create_connection(("8.134.210.73", 30050), timeout=5) as sock:
    sock.sendall(b"hello-test")

print("连接成功，测试数据已发送")
PY
```

## 第十步：检查测试数据是否落盘

回到服务器项目目录：

```bash
cd /opt/data_record_server
```

查看事件记录：

```bash
tail -n 50 data/events.jsonl
```

应该看到：

- `connected`：客户端建立了连接。
- `received`：接收到了一批数据。
- `disconnected`：客户端断开了连接。

列出原始数据文件：

```bash
find data/connections -maxdepth 1 -type f -name '*.bin' -ls
```

查看第一份数据的十六进制内容：

```bash
first_bin=$(find data/connections -maxdepth 1 -type f -name '*.bin' -print -quit)

if [ -n "$first_bin" ]; then
  od -Ax -tx1 -N 256 "$first_bin"
fi
```

两种文件的区别：

- `data/connections/*.bin`：设备传来的原始字节，是最重要、最可信的数据。
- `data/events.jsonl`：方便观察连接时间、客户端 IP、每次收到多少字节等信息。

当前程序不会猜测设备协议，也不会主动向设备回复业务 ACK。

## 第十一步：配置 App

在 App 中填写：

```text
网络协议：TCP
IP/域名：8.134.210.73
端口：30050
回传频率：根据需要选择，例如 1 秒 1 次
```

点击“设置”后，在服务器实时观察：

```bash
docker compose logs -f collector
```

另开一个 SSH 窗口观察事件：

```bash
cd /opt/data_record_server
tail -f data/events.jsonl
```

按 `Ctrl+C` 只是退出日志查看，不会停止接收程序。

## 常用维护命令

查看状态：

```bash
docker compose ps
```

查看最近日志：

```bash
docker compose logs --tail=100 collector
```

实时查看日志：

```bash
docker compose logs -f collector
```

重启程序：

```bash
docker compose restart collector
```

停止并删除容器：

```bash
docker compose down
```

重新启动：

```bash
docker compose up -d --build
```

`data/` 是服务器上的持久化目录，所以正常重启、`down` 或重新构建容器都不会删除已经采集的数据。

最后提醒：当前服务没有 TLS 和客户端认证。测试阶段可以临时开放 `0.0.0.0/0:30050`，正式运行时应尽量限制安全组来源，并监控磁盘空间。另外，之前服务器 root 密码已经在聊天中明文出现，建议部署完成后更换密码并改用 SSH 密钥登录。
