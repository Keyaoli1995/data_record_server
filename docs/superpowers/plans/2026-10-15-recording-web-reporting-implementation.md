# 录制服务 Web 上报实现计划

> **For agentic workers:** 使用 superpowers:executing-plans 逐项执行本计划；未经新授权不部署、不推送。

**Goal:** 保持原始录制行为，按一文件一任务向 Web 可靠发送创建、更新、结束信息。

**Architecture:** storage 产生本地生命周期与统计，SQLite outbox 保存稳定请求，后台 HTTP sender 顺序重试；作业地时区通过独立解析器提供，不参与原始录制有效性判断。

**Tech Stack:** Python 3.12、标准库 sqlite3/urllib/zoneinfo、现有 unittest；离线时区库需在设计评审后锁定版本。

依据：[设计文档](../specs/2026-10-15-recording-web-reporting-design.md)。状态：本地开发及自动化验收已执行，详见文末实测记录。时区策略和外部接口前提须按设计第 9 节收敛后再实现；不能把模拟服务通过当作真实联调通过。

## 1. 文件职责

| 文件 | 改动 |
| --- | --- |
| data_record_server/reporting_models.py | 新建：任务身份、统计、严格三阶段请求构建 |
| data_record_server/reporting_store.py | 新建：SQLite 登记、outbox、重试状态、恢复查询 |
| data_record_server/reporting_client.py | 新建：有界 HTTP 请求、响应校验、错误分类 |
| data_record_server/reporting_worker.py | 新建：调度、阶段依赖、退避和后台生命周期 |
| data_record_server/worksite_timezone.py | 新建：坐标质量、离线查询、稳定确认和锁定 |
| data_record_server/storage.py | 生命周期挂接、关闭后核验、未完成通知文件保护 |
| data_record_server/config.py、server.py | 配置、启动恢复、有界停止 |
| tests/test_reporting_models.py、test_reporting_store.py、test_reporting_client.py、test_reporting_worker.py、test_worksite_timezone.py | 新建相应单测 |
| tests/test_storage.py、test_server.py、test_config.py、test_deployment.py | 扩展回归和接入测试 |
| README.md、CAPABILITIES.md、DEPLOYMENT_GUIDE.md、compose.yaml、Dockerfile | 用法、恢复、容器挂载与依赖 |

不为本次工作重构无关 TCP 信号协调、拒绝连接记录和解析脚本。

## 2. 开发与提交规范

每项工作先写指定场景的 unittest，运行对应测试，确认失败原因是缺少预期能力；再最小实现并确认变绿，最后必要时重构。提交只包含该阶段相关文件。

- Red：`test: init cases for recording web reporting <component>`。
- Green：`feat: implement recording web reporting <component> and pass tests`。
- Refactor：`refactor: optimize recording web reporting <component> logic`。

基线命令：`python3 -m unittest discover -s tests -v`。测试使用临时目录、固定 UTC 时钟、mock 传输和固定随机源，不连接生产服务、不依赖睡眠等待。

## 3. 分步任务

### Task 1：冻结配置和接口契约

- [x] 检查 Git 状态、当前 Python 版本并运行基线测试，记录实际结果。
- [x] 在 test_config.py 添加关闭上报保持旧默认值；开启缺地址/本地环境报错，鉴权可省略，非空鉴权检查非法字符；周期和超时必须有限正数；日志不输出凭据的失败用例。
- [x] 执行 `python3 -m unittest tests.test_config -v`，确认新用例失败。
- [x] config.py 增加独立 reporting 配置，默认禁用；在 compose 中显式传递配置，禁止编入凭据。
- [x] 重跑配置和部署测试，记录通过后提交。

### Task 2：身份、字段和统计

- [x] test_reporting_models.py 覆盖 fileKey/taskId 稳定独立、创建序号与更新序号独立、SN 和相对路径、schemaVersion 整数 1。
- [x] 覆盖 GPS 乱序取 min/max、缺失/布尔/非有限/越界时间；无 GPS 异常结束；原始字节换行参与 SHA 和大小；所有实际记录参与计数。
- [x] 运行 `python3 -m unittest tests.test_reporting_models -v`，确认 Red。
- [x] 实现 reporting_models.py，阶段请求使用不可变快照；对 ICD 未定义字段不擅自添加，对已确定字段严格校验。
- [x] 重跑该模块，确认 Green；核对关闭原因与 Web 已确认枚举。

### Task 3：作业地时区

- [x] 确认自动识别策略，首版不实现人工覆盖，再锁定离线库及地理数据版本，核对 Docker Python 3.12 兼容性和许可。
- [x] test_worksite_timezone.py 使用可注入坐标查询函数，覆盖未知→候选→锁定、连续点及时间跨度、不同结果重置、锁定后冲突不改、无定位不猜测、查询异常不影响录制。
- [x] 另外覆盖有效范围、bool/NaN/Inf、缺字段、GNSS 状态不合格、海洋/边界无结果；人工覆盖若纳入首版，则测试过期与新任务快照。
- [x] 执行 `python3 -m unittest tests.test_worksite_timezone -v`，确认 Red。
- [x] 实现 worksite_timezone.py；地理查询在后台工作路径，TCP 线程只传递有限候选数据；未知不发送 timezone。
- [x] 增加实际库固定坐标回归和 zoneinfo 冬夏日期转换测试，确认 Green。固定坐标回归与 mock 业务测试分开，避免升级地理库后静默改变预期。

### Task 4：持久化队列

- [x] test_reporting_store.py 覆盖同文件严格依赖顺序、不同文件独立、重试请求内容不变、重开数据库身份保留、事务回滚、磁盘/SQLite 错误可观测。
- [x] 执行 `python3 -m unittest tests.test_reporting_store -v`，确认 Red。
- [x] reporting_store.py 建立 recordings 与 outbox 表，保存 fileKey、taskId、阶段、序号、固定请求 JSON、幂等键、状态、下次发送时间、错误摘要；不保存鉴权凭据。
- [x] 使用短事务和唯一约束防重复，提供可发送请求查询、成功确认、重试和人工阻塞操作；不可在事务内等待 HTTP。
- [x] 重跑测试并验证两个独立文件互不阻塞。

### Task 5：HTTP 客户端和发送器

- [x] test_reporting_client.py 覆盖方法/路径/headers、code=0 加状态身份核对、HTTP 200 业务失败、畸形响应、鉴权失败、重定向不泄漏凭据、429/5xx/超时分类。
- [x] test_reporting_worker.py 覆盖创建确认前不更新、更新确认后关闭、响应丢失重发同键、10 秒可配置调度、无变化不更新、退避上限、停止有界、终态错误人工阻塞。
- [x] 分别运行对应 unittest 模块确认 Red。
- [x] 实现 reporting_client.py 和 reporting_worker.py，使用 mock 时钟和传输保持可测试；正确解析 Retry-After，日志脱敏。
- [x] 重跑模块测试，确认失败设备不阻塞其他设备。

### Task 6：挂接录制生命周期

- [x] tests/test_storage.py 添加同 SN 多连接仍一文件一任务、30 秒超时结束、新数据新任务、正常停机、无有效 GPS、通知存储故障场景。
- [x] tests/test_server.py 添加 Web 无响应时 TCP 仍能录制其他设备、关闭时最终通知已持久化、发送器有限等待退出。
- [x] 运行 `python3 -m unittest tests.test_storage tests.test_server -v` 确认新用例 Red。
- [x] 只在 session 创建/追加/关闭处挂接本地登记和统计，HTTP 完全独立；关闭最终摘要由后台核验；默认禁用时不引入行为变化。
- [x] 重新运行以上测试及协议回归，确认原始字节未变。

### Task 7：异常重启、补录和保留

- [x] 新建 tests/test_reporting_recovery.py，注入“登记后未建文件、已写入未建关闭通知、服务器确认后本地未确认、尾部不完整”四个故障点。
- [x] 覆盖待通知文件不被清理、READY 文件恢复正常保留策略、历史未知文件不自动伪造任务、Web TIMEOUT 保留待人工恢复。
- [x] 执行 `python3 -m unittest tests.test_reporting_recovery -v` 确认 Red。
- [x] reporting_store/worker 与 storage 实现启动核对；旧文件不恢复追加，不改原始字节；补录入口必须显式提供文件范围并复用稳定登记身份。
- [x] 重跑恢复、保留和停机测试，核对持久化数据与文件各自失败时的日志。

### Task 8：端到端模拟验收和交付

- [x] 新建 tests/test_reporting_integration.py，用本地 HTTP 模拟器接收真实请求，真实 TCP 输入合成 JSONL；验证创建、更新、关闭及服务重启重放。
- [x] 核对服务器读取原始文件得到的 SHA/大小/记录数/GPS 范围与请求完全一致；模拟重复响应、断连、鉴权错误、远端任务超时。
- [x] 执行 `python3 -m unittest tests.test_reporting_integration -v`，再执行全量 `python3 -m unittest discover -s tests -v`。
- [x] 更新 README、CAPABILITIES、部署指南，明确时区来源、原始存储映射、配置模板、重试诊断和补录操作，不能把建议参数写成 Web 已确认契约。
- [ ] 在测试环境构建镜像，验证非 root、只读根文件系统、SQLite 可写目录、地理数据可读与健康退出；生产发布另行执行。
- [x] 提供实测结果和外部确认清单。真实接口尚未提供时，交付状态标为“本地模拟验收通过，等待 Web 联调”，不得标为已上线。

## 4. 接口冻结检查表

- [ ] 一文件一任务的 Web FINISHED 转换已确认。
- [ ] 内网地址、网络 ACL、存储映射和完整成功/失败响应已确认（新版免登录鉴权）。
- [ ] GPS min/max、关闭原因、INVALID 修复、TIMEOUT 补录已确认。
- [ ] 时区自动识别与人工覆盖范围已确认。
- [ ] 最新位置足够，还是需要全部增量 points，已确认。
- [ ] Worker 存储挂载、保留和读取期间不可变规则已确认。

这些外部前提不阻止编写 mock 测试，但阻止将猜测作为生产接口部署。

## 5. 实测记录与实施调整

- 基线100项测试通过后开始，每项新增业务先观察失败再实现；提交历史保留 test/feat 阶段。原有 unittest 风格保持。
- 恢复场景集中在 test_reporting_worker.py、test_reporting_lifecycle.py 和 test_reporting_integration.py，不单独创建 test_reporting_recovery.py。运维接口新增 reporting_admin.py。
- 最新位置不填造 accuracyMeters；可选全量 points 和人工时区覆盖不实现。
- 依赖已在隔离Python3.12安装，全球时区真实坐标测试强制执行，不以skip代替验证。
- 本地真实TCP/HTTP验证了响应丢失与重启重放。生产/Web真实联调未执行。
- 当前 WSL 无可用 Docker 引擎，镜像构建与非root容器启动未执行；部署文件由契约测试验证。
- 详细配置、可重试/人工处理错误及补录命令见 [接入与运维](../../web-reporting-operations.md)。

最终本地验证：`env -u PYTHONPATH PYTHONTZPATH='' /tmp/wayrobo-timezone312/bin/python -m unittest discover -s tests -q`，167项全部通过（23.247秒）；`pip check`、`git diff --check`通过。
