# 录制服务 Web 上报接入与运维（ICD(2)）

当前契约以 2026-10-19 ICD V1.1（ICD(2)）为准。录制服务负责原始文件保存、统计、作业地时区和文件生命周期上报，不负责 Web 查询、地图或解析 Worker。Web 真实联调通过与本地测试通过是两回事。

## 正式接口

| 阶段 | 方法及路径 | 成功文件状态 |
| --- | --- | --- |
| 创建 | POST `/api/v2.4/manual-raking/recording-files/` | OPEN |
| 更新 | PATCH `/api/v2.4/manual-raking/recording-files/{fileKey}/` | OPEN |
| 结束 | PATCH `/api/v2.4/manual-raking/recording-files/{fileKey}/status/` | 正常 READY（兼容历史 CLOSED）；放弃 ABORTED |

创建字段为 `recordingTaskId`、`locatorSn`、`fileKey`、`sequence`、`storageUri`、`openedAt`。不再使用旧 `rakingRecordingTaskId`、`rakingLocatorSn` 和 `schemaVersion`。更新携带统计、最新有效位置、设备数据时间和可用时区；不发送可选 points，不承诺完整实时轨迹。结束携带最终统计、摘要、原路径、关闭时间及原因，不发送 `status=FINISHED`。

响应必须同时满足 HTTP 成功、整数 `code=0`、`data.recordingTaskId`/`fileKey` 对应当前文件、文件状态符合阶段；更新还要求整数 `lastSequence` 不小于请求序号。文件状态 OPEN 与任务状态 RECORDING 是不同概念。普通业务错误可为 HTTP 200、`code=-1`、`data=null`，保留队列转人工处理，不打印可能含敏感内容的 msg。已知旧结构的稳定错误码仍能识别，不依赖 Web 一定提供它们。

创建序号为 1，后续快照严格递增；重试复用任务 ID、fileKey、序号和原请求体。继续发送稳定 Idempotency-Key 便于跟踪，但 ICD(2) 的 Web 不校验该头，不能依赖它去重。模拟集成测试按请求资源及 sequence 去重。

## 配置

在部署目录 `.env` 中填写；不要提交凭据：

```dotenv
WEB_REPORTING_ENABLED=true
WEB_BASE_URL=https://v.bynav.com:8105
WEB_ENVIRONMENT=test
WEB_AUTHORIZATION=
WEB_STORAGE_ROOT=/opt/manual_raking_trajectory/data_record_server/data
WEB_ALLOW_HTTP=false
WEB_UPDATE_INTERVAL_SECONDS=10
WEB_REQUEST_TIMEOUT_SECONDS=5
WEB_SHUTDOWN_REASON=SERVER_SHUTDOWN
```

以上 URL 仅为已知测试环境示例，不能复制到生产。URL 只包含协议、主机、端口。默认验证 HTTPS 证书且不跟随重定向；确认的内网 HTTP 地址必须显式启用 WEB_ALLOW_HTTP。Web 上报免登录鉴权，Authorization 可省略，网络 ACL 由 Web 部署维护。不发送 X-Environment；WEB_ENVIRONMENT 仅与 origin 一起绑定本地队列，禁止跨环境重放。

WEB_STORAGE_ROOT 是 **Web 与 Worker 看到的绝对路径根目录**，与录制容器内 DATA_DIR 分开。Compose 默认宿主机录制目录，非 Compose 启动未设置时使用实际 DATA_DIR 的绝对路径。若路径含 `..`、`.`、控制字符或反斜杠则拒绝启动。

例如录制进程读写 `/data/devices/device-SN/file.jsonl`，上报 `/opt/manual_raking_trajectory/data_record_server/data/devices/device-SN/file.jsonl`。Web 和 Worker 必须把原始数据只读挂载到上报路径；它们不会自动添加根目录或替换路径。录制服务只拼接路径，不上传原始内容，也无法保证远端挂载已配置。每份文件创建时持久化 storageUri，之后配置变化只影响新文件，旧文件结束仍使用创建时路径。

ICD(2) 的结束接口不读取文件、不重算 SHA256。返回 READY 仅表示 Web 接受结束信息，不能据此证明 Worker 可读或完成完整性验证。录制服务仍自行统计和核验原始字节。

## 时间和时区

录制协议的 gpsTime 当前按 Unix 毫秒解析。firstGpsTime、lastGpsTime 上报原始有效整数的最小/最大值，不凭位数自动除以 1000；lastReportedDataAt 和位置 recordedAt 从该毫秒值转换为 UTC ISO 时间。ICD(2) 仅规定 GPS 字段 integer，未写单位，其示例不能作为改变原始数据单位的依据。真实联调需使用已知采样验证双方含义；若 Web 最终要求秒，应另行明确转换规则和精度，不修改原始文件。

作业时区由固定解 WGS84 坐标在离线全球时区数据中查询；3个时间递增点、跨度至少2秒且时区一致才锁定。未确定时省略，不按服务器地区猜测。冲突仅本地审计；ICD(2) Web 不返回冲突字段。时间始终 UTC。

closeReason=ABORTED 表示异常放弃；IDLE_TIMEOUT、SERVER_SHUTDOWN 等其他原因按正常结束。Web 的任务正常结束为 READY，放弃为 FAILED，FINISHED 只是前端筛选别名。无有效 GPS 或损坏文件仍本地保留并上报 ABORTED。

## 升级和旧队列

升级前停止录制进程，并备份原始数据及 `/data/reporting`（包括 SQLite/WAL/SHM，或使用 SQLite 正规备份）。启动自动添加 storage_uri 列并保留所有身份、请求体和幂等键。

旧创建请求使用 rakingRecordingTaskId 的未完成文件会置为 ERROR，错误为 `LEGACY_ICD_RECONCILIATION_REQUIRED`，不自动重放、不删除文件，也不影响新文件。即使旧请求曾被标记 SENT，也不能证明它与新响应契约一致，因此需要与 Web 对账。已完成 DONE 的历史记录保持原状态。普通 retry 不能解除该隔离，不能通过新建 ID 冒充一次新录制；确认远端接收情况后需制定单独迁移步骤。

## 查看和维护

```bash
docker compose exec collector python -m data_record_server.reporting_admin status
```

状态查询只读。可重试的网络/429/5xx 自动退避，普通业务失败及未知响应隔离。修复后先停止录制服务，再用同配置离线执行：

```bash
docker compose stop collector
docker compose run --rm --no-deps collector python -m data_record_server.reporting_admin retry --file-key FILE_KEY
docker compose up -d --no-build collector
```

远端已终态或本地 ERROR 文件不能普通重试。工具不会自动修复 Web 文件、回滚任务或修改原始文件。

历史补录需要明确原 SN 和有依据的 UTC 开始/结束时间，在服务停止后执行：

```bash
docker compose run --rm --no-deps collector python -m data_record_server.reporting_admin import \
  --path devices/device-SN/file.jsonl --device-id SN \
  --opened-at 2026-10-19T02:00:00Z --closed-at 2026-10-19T03:00:00Z \
  --reason IDLE_TIMEOUT
```

--path 仍是相对于 DATA_DIR 的本地路径，发布时转换为 WEB_STORAGE_ROOT 下路径。缺文件、越界或损坏记录保留 ERROR，不用改原始数据掩盖问题。待补发及 ERROR 文件受保留清理保护，应监控磁盘。队列日志输出数量、年龄和字节数，状态命令显示需对账的 fileKey。

## 本地验证

使用 Python 3.12 的隔离环境安装 requirements-reporting.txt 和 requirements-dev.txt，然后执行：

```bash
env -u PYTHONPATH PYTHONTZPATH='' .venv/bin/python -m unittest discover -s tests -q
```

部署须使用新镜像；仅修改 .env 不能更新旧客户端。正式启用后分别验证创建、更新和结束响应，再验证 Worker 读取原路径。OPTIONS 返回200不代表文件全流程联调通过。
