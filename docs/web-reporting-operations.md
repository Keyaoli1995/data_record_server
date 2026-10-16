# 录制服务 Web 上报接入与运维

本功能已在本地实现，默认关闭。原始录制保持一文件一任务，空闲超时沿用 IDLE_TIMEOUT_SECONDS（默认30秒）。本地模拟测试不代表 Web 接口已发布；不得直接使用生产环境试验通知。

## 能力范围

- 创建/更新/关闭按 ICD 第4.1节请求上报，稳定 fileKey、taskId、幂等键和文件内通知序号。
- 后台扫描已落盘字节，周期统计记录数、大小、GPS min/max和最新有效固定解位置。HTTP 不在 TCP 接收线程运行。
- SQLite outbox 保存在 `/data/reporting/outbox.sqlite3`，文件身份先于创建落盘；同文件依赖顺序发送，不同文件错误隔离。重试保持请求内容不变。
- 网络/协议错误、429、5xx 指数退避；ICD `data.errorCode` 中明确的永久错误优先于 HTTP 状态。无法确认的响应转人工处置，绝不仅因 HTTP200 标记成功。
- 时区确定前省略 timezone；无 GPS 的原始文件保留并 ABORTED；损坏尾部不截断修复。
- 正常停机先持久化关闭意图，后台核验并生成结束 outbox；超过停止等待预算时保留意图，重启重建。崩溃恢复只处理已登记文件，新数据新任务。
- 首版只提供 currentLocation，不传可选 points 数组；不承诺全量实时预览轨迹。没有人工时区覆盖入口。

## 配置

通过本地 `.env` 或部署系统注入；`.env` 已被 Git 和镜像构建忽略。下列示例没有可用凭据：

```dotenv
WEB_REPORTING_ENABLED=false
WEB_BASE_URL=https://web.example.invalid
WEB_ENVIRONMENT=test
WEB_AUTHORIZATION=
WEB_ALLOW_HTTP=false
WEB_UPDATE_INTERVAL_SECONDS=10
WEB_REQUEST_TIMEOUT_SECONDS=5
WEB_SHUTDOWN_REASON=SERVER_SHUTDOWN
```

新版 ICD 的三个录制上报接口免登录鉴权，通过内网调用，由网络和路由 ACL 限制来源，不应暴露到公网。启用前确认内网 URL、存储根映射、三阶段接口和停机原因枚举。WEB_AUTHORIZATION 可以省略或留空，此时不发送 Authorization；仅在网关另有要求时配置完整请求头值。

WEB_ENVIRONMENT 仍是必填的本地队列隔离标识，不会作为 X-Environment 请求头或请求参数发送。Web 的业务环境仅由其部署配置决定。创建、更新路径不变，结束路径为 POST `/api/v2.4/manual-raking/recording-files/{fileKey}/closure/`，不再调用 `/close/`。

URL 只填 origin（协议、主机和端口），不带 `/api`、路径、查询或用户名密码。默认 HTTPS 校验证书，禁止重定向携带凭据；已确认的内网 HTTP 地址需显式 WEB_ALLOW_HTTP=true（本地模拟同样适用），默认仍只接受 HTTPS。环境和 origin 与 outbox 绑定，改变后拒绝启动，防止把积压发往其他区域；凭据轮换可保持原库重试。

正常关闭原因采用 IDLE_TIMEOUT；服务停机默认 SERVER_SHUTDOWN（待 Web 确认），可以按双方认可的枚举配置。异常使用 ABORTED，不能把服务停机冒充 USER_STOPPED 或 DEVICE_SHUTDOWN。

原始文件路径相对于 DATA_DIR。Web/Worker 必须配置同一环境的只读存储映射；相对 storageUri 不能直接交给要求绝对路径的解析脚本。服务不上传原始文件，不提供下载接口。

## 全球作业地时区

使用 WGS84 `GNSSlat/GNSSlon` 和当前离线地理边界查询 IANA 名称，不根据服务器地区、系统时区或 IP 猜测。仅接受 GNSSPosStat=4、合法坐标和整数 UTC 毫秒；至少3个时间递增的合格点、跨度2秒且结果一致，才锁定本任务时区。位置含义是天线原始定位，不是杆臂修正后的轨迹。

锁定后不覆盖，冲突写入本地审计。库不可用、无合格定位或无法匹配时继续 UTC 录制和上报，不造默认时区。时间始终存 UTC，由 Web 根据 IANA 时区和实际日期转换夏令时。

依赖锁在 requirements-reporting.txt：timezonefinder9.0.0、全球边界数据3.2026.4、tzdata2026.5。镜像 PYTHONTZPATH 为空，使用固定 tzdata 规则；开发机测试也建议设置该变量。审计保存库版本、数据版本、坐标和 GPS 时间。

28个地点覆盖六个有人居住的大洲和太平洋，检查非整点偏移、日期变更线、季节变化；这些回归并非全球每条边界的穷举证明。多边形采用严格包含查询并排除 Etc 海洋默认区，但边界数据包含部分领海，重叠区可能返回第一个匹配，定位噪声也会影响边界附近判定。没有额外海岸线/边界距离置信度算法。软件升级时须复核固定坐标与夏令时回归，不能宣称永远自动覆盖未来政策变更。

timezonefinder 代码为 MIT，边界数据为 ODbL-1.0（来源 OpenStreetMap/timezone-boundary-builder）；发布镜像保留依赖自带许可。离线代码和地理数据约34MiB，完整 Python依赖另计。

## 查看、补发和历史补录

启用前需使 `/data/reporting` 可被容器用户10001写入，与归档共用持久化卷。SQLite数据库及 WAL/SHM 必须一起按 SQLite 正规备份流程处理，不能仅复制运行中的主文件。单个 DATA_DIR 只允许一个上报进程，使用 flock 防止第二个进程误恢复仍在录制的文件。

查看状态只读，可在服务运行时执行：

```bash
python -m data_record_server.reporting_admin status
```

需使用相同 DATA_DIR。输出仅包含文件、阶段、状态、重试次数及错误码，不包含鉴权值。

修复鉴权或配置后，停止该 DATA_DIR 的录制进程，再用相同配置执行：

```bash
python -m data_record_server.reporting_admin retry --file-key FILE_KEY
```

随后启动服务继续发送。该命令不自行发送 HTTP，不修改原始文件，不解除 Web TIMEOUT/AlreadyFinished。远端终态需要 Web 对账或双方确认的恢复流程，不能通过换 ID 偷换为新文件。

历史文件显式补录也必须先停止进程，核实文件确已结束、原始 SN 和事件时间。时间参数必须为 UTC，不能以 gpsTime 冒充服务器事件时间：

```bash
python -m data_record_server.reporting_admin import \
  --path devices/device-BY80126B66/record.jsonl \
  --device-id BY80126B66 \
  --opened-at 2026-10-15T02:00:00Z \
  --closed-at 2026-10-15T03:00:00Z \
  --reason IDLE_TIMEOUT
```

该命令只登记和生成通知，重复导入同路径返回原标识。开始/结束事件时间必须有依据；未知历史事件时间不要编造。正常导入必须有有效 GPS 和完整记录；异常归档需显式 ABORTED。录制中的文件、外部路径、任意二进制均不应导入。

每分钟日志包含队列状态/数量、最老等待秒数、请求体字节数和受保护原始文件字节数。未确认通知及需人工处理的文件不会被保留清理自动删除，应监控磁盘；已发送 outbox 当前保留用于审计，不自动压缩清除。

缺文件、损坏或路径异常记录会保留 ERROR 状态和原文件。不要编辑原文件来掩盖错误；恢复缺失文件后如何重新纳入索引需维护人员与 Web 确认。本工具不提供覆盖已归档文件或解除远端终态的强制开关。

## 验证与发布前提

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-reporting.txt PyYAML
env -u PYTHONPATH PYTHONTZPATH='' .venv/bin/python -m unittest discover -s tests -v
```

测试包括真实本地 TCP/HTTP、丢失响应后的重启补发、完整性与队列状态验证。日志中模拟 NETWORK_ERROR/停机超时等为测试预期故障。

本次环境 Docker Desktop 未启用 WSL 集成，不能执行实际镜像构建和容器启动；Dockerfile/Compose契约已由测试校验，Python3.12隔离环境的依赖已实际安装。部署环境仍需执行 `docker compose build`、非 root持久化写入、原始存储挂载和 Web真实接口联调。

本次最终本地回归：167项全部通过（Python3.12，包含原有100项）；依赖检查通过。
