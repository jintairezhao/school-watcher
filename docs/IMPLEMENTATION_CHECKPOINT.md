# 实施暂停记录 · 2026-09-23

> 2026-09-24 已恢复并完成本轮代码交付与本机切换。本文件以下保留暂停时的历史上下文；现行状态、实测和外部环境待验收项见 [验收记录](RUNTIME_ACCEPTANCE.md) 与 [运行手册](UNIFIED_FETCH_RUNTIME.md)。

> 暂停时列出的 `data/test-runtime` 临时 PostgreSQL 已在验收完成后停止并清理，不再是可直接重启的环境。

用户要求今日暂停，明日再继续。所有工作保留在当前工作区，尚未提交；不能重置、覆盖或清理已有改动。包含本次之前的未提交工作。

## 已写入并验证

- 统一 FetchRequest / FetchResult、HTTP → 浏览器协调器、分类和适配配置；目录、列表、正文和来源检查接入；Playwright 旧共享单例替换为服务客户端。
- 独立异步浏览器服务、管理员人工验证入口、会话过期和所有权隔离。18 项隔离浏览器/权限测试通过；4 项真实 Edge Chromium 测试通过（显式本地测试配置）。捆绑 Chromium 下载受环境 DNS/网络限制，不能宣称已验证。
- 数据库协调任务、worker 和调度器租约、官网限流、浏览器转交/恢复点、失租提交保护。SQLite 和实际 PostgreSQL 18.6 的 24 项共享运行测试通过，包含 100 个并发更新请求合并成一个活动任务。
- 学校 + 规范化原文地址的原子去重、重复记录迁移与旧 ID 映射、来源关系/收藏/已读合并；普通通知导入继续采用增量并集。
- PostgreSQL 连接配置、原生备份基础、版本化目录发布、完整搬迁工具初稿已写入，仍需专项恢复和迁移演练。
- 最近一次完整回归：489 项，全部成功，其中 16 项条件跳过（未在该命令中配置 PostgreSQL/真实浏览器）。不能将条件跳过算作本轮实测通过。

## 明日优先继续

1. 检查各并行任务的最后交付状态和工作区差异，先审查再继续修改。
2. 新增完整搬迁工具 `backend/services/database_transfer.py` / `scripts/migrate_database.py` 尚未专项测试：SQLite 一致快照升级 → 空 PostgreSQL，复制所有业务表、核对计数、恢复序列、复制目录版本；要核对实际任务状态及 VerificationWaiter 清理/重置，保留 SECRET_KEY 和 FIELD_ENC_KEY。源库不可修改。
3. 新迁移 12a6c93f4e80 后期新增 VerificationWaiter 和 ScrapeLog.task_token；此前 root 的 watcher_integration 已迁移，需重建隔离测试库再走完整历史升级。禁止动用户生产库。
4. 完成原生 PostgreSQL 备份/恢复、旧 SQLite 重复数据升级、增量导入、并发文章去重专项测试。备份恢复已校验路径/摘要，但需复核边界情况。
5. 接入 `runtime_catalog.prune_catalog_generations()` 到存储清理，核对统计。它保留当前激活版本，至少保留 7 天未发布/旧版本。
6. 审查 Docker Compose、Nginx/noVNC、Windows 启动器（浏览器任务负责，暂停时可能未完成）；Linux 无 Docker/WSL，未实际运行，不可宣称验收通过。
7. 完成管理员运行指标、100 用户/1000 来源混合负载脚本及电子科技大学真实来源报告（抓取任务负责，暂停时未完成）。不承诺所有挑战通过。
8. 跨浏览器重定向/子资源有公网地址校验，但跨目标域名的数据库限流仍需明确实现范围或补齐；来源初始浏览器访问已有共享许可。
9. 补充部署/测试报告、管理页验证入口，最终再评估本机数据库升级和服务切换。今天没有把新架构部署到生产实例。

## 本地测试环境

- 项目 `.venv` 已创建，继承系统包；Python 3.14.3，Playwright 1.61.0，aiohttp 3.14.3，psycopg 3.3.6。
- 临时 PostgreSQL 18.6 二进制：`data/test-runtime/postgresql/pgsql/bin`；集群 `data/test-runtime/pgdata`；监听仅 127.0.0.1:55439，账号 watcher，本地临时信任认证。暂停时停止该临时服务，明日按需再启动，不属于应用生产数据库。
- root 测试库 watcher_integration；worker 任务使用 watcher_runtime_test，均隔离测试数据。
- Python subprocess 在此沙箱可切换项目目录，PowerShell Set-Location 曾受祖先目录权限限制。避免改变系统 DNS、代理或安装系统级服务。
- 未做提交、推送或生产搬迁。所有改动与测试报告需明日汇总审查。

## 暂停收尾确认

- root 临时 PostgreSQL 服务已正常停止；完整回归进程已结束。
- 浏览器任务已停止，无正式浏览器服务启动。Dockerfile/Compose/Nginx/Windows 启动器尚未写入；仅新增固定版本 seccomp 配置。模块测试说明位于 `backend/browser_service/README.md`。
- worker 任务已停止，全部测试退出。还需复核 finish 提交前配置校验、长调度任务租约续期/错峰、证据缓存写入硬限额与保留天数 0 的语义。
- 下一步内部官网额度接口应先由任务模块提供，再由浏览器接通主文档跨域跳转及人工验证。管理员运行指标尚未实现。
