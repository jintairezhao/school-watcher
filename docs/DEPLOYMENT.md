# 轻量公开试运营

> 本文记录 2026-09-21 的 SQLite 试运营版本与历史验收。2026-09-24 统一抓取、PostgreSQL、独立浏览器及 Docker Compose 的现行说明见 [统一抓取与多人部署运行手册](UNIFIED_FETCH_RUNTIME.md)。下文“PostgreSQL 尚未验收”等描述只对应历史版本。

网站与采集独立运行，SQLite 放在单台服务器的本地持久磁盘。正式上线前需要在目标服务器复测；本轮没有购买服务器或发布公网网站。

## 运行结构

- `app.py`：现有 Flask＋Waitress 网站，默认监听 127.0.0.1:5000。请求只入队，不启动采集线程。
- `scripts/run_worker.py`：一个独立进程、两个任务执行线程；HTTP 请求合计最多两个并发。任务使用数据库去重、租约和重试，进程中断后可重新领取。
- 采集请求通过统一入口控制同一官网的请求启动间隔（至少 1 秒），包含旧栏目采集、正文和重定向。通知列表任务遇到短暂网络错误时最多尝试三次；访问校验、拒绝访问、HTTP 429、失效地址或栏目规则未识别时停止本轮自动重试，日志显示具体原因。
- `source_catalog.sqlite3`：按学校、页面数字编号组织的压缩目录；包含必要关系、栏目配置、来源核验结果及核验样例原文。网页不依赖旧调查库。
- `discovery_cache.sqlite3`：独立适配任务的临时工作库。每轮最多 20 页、深度最多 4、每校最多 1,000 个待核对及已读取页面；达到限制不表示覆盖完成。
- SQLite 专属连接配置仅用于 SQLite，业务 upsert 通过方言入口管理。PostgreSQL 是以后扩容的迁移方向，尚未在 PostgreSQL 实例上验收。

正文在首次阅读或收藏时入队补取；未收藏正文默认最多 150MB、30 天，收藏正文不随缓存淘汰。搜索覆盖标题、摘要及保存中的正文。图片和附件保留官网链接。调查原文默认保留 7 天，临时调查库达到 100MB 时清理已发布的可重建记录；清理后仍超限则停止该轮并报错。管理员可在存储管理页面修改两类缓存保留天数，设为 0 时只取消时间淘汰，容量限制仍生效。

部分校园网络把学校域名解析为内网地址。采集仍只连接经过验证的公网目标：备用 UDP DNS 不可达时，使用固定 HTTPS DNS 服务查询公网地址并按 TTL 缓存，检查问题域名、CNAME 归属和公网 IPv4，保留官网 TLS 证书及主机名校验，不写死学校 IP。JSON 应答格式参考[阿里云官方 API 文档](https://static-aliyun-doc.oss-cn-hangzhou.aliyuncs.com/download%2Fpdf%2F171662%2FAPI_Reference_intl_en-US.pdf)与[Cloudflare 官方文档](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/)。

## 本地使用

现有桌面快捷方式及 `scripts/run.bat` 都调用 `scripts/launch_desktop.py`，分别启动网站和采集进程。重复启动通过文件锁避免多开。

手动运行时，在两个终端分别启动：

```sh
python scripts/migrate_safely.py
python app.py
# 第二个终端
python scripts/run_worker.py
```

调度器每 30 秒读取数据库设置；默认每 30 分钟同步订阅栏目，每日检查来源和维护缓存，每月安排学校目录检查。目录更新时间以实际发布记录为依据。首轮缺失来源通过受限适配任务补齐，候选栏目缺少名称或可信度不足时保留待核实。

系统管理 → 抓取记录（`/admin#logs`）按学校完整展示近七天记录，更早记录默认收起，展开后按需加载。管理员可选择保留 7、30、90、180、365 天或永久，默认 30 天；规则保存为数据库配置 `scrape_log_retention_days`（0 表示永久）。每日维护任务读取最新规则，只清理超期且已结束的抓取记录，不影响通知、用户记录和正在进行的抓取。延长保留期无法恢复此前已经清理的记录。

迁移 `f05f437d20b6` 增加记录的学院/栏目名称快照和按时间、学校查询的索引，保留已有编号和记录内容；旧记录未保存的栏目名称显示“历史任务（未记录具体栏目）”，不反推归属。升级时网站和 worker 都应重启，使后续采集开始记录具体范围。

## Linux 部署步骤

1. 将代码放到 `/opt/school-watcher`，建立虚拟环境并安装 `requirements.txt`。创建专用 `school-watcher` 系统用户及归其所有的 `/var/lib/school-watcher` 目录。
2. 将 `deploy/environment.example` 复制为 `/etc/school-watcher.env`，设置真实域名和持久随机 `SECRET_KEY`，权限设为仅管理员可读。不要把真实密钥写进版本库。
3. 停止网站、worker 后迁移业务数据库；将业务数据库及精简目录放到持久目录。首次空部署先运行迁移，尚无精简目录的学校通过订阅后的适配任务建立。
4. 迁移命令须使用与服务相同的环境，示例：

   ```sh
   set -a
   . /etc/school-watcher.env
   set +a
   /opt/school-watcher/.venv/bin/python /opt/school-watcher/scripts/migrate_safely.py
   ```

5. 安装两个 `deploy/school-watcher-*.service` 到 systemd 服务目录；安装 Caddy，将 `deploy/Caddyfile` 作为配置，并将 `deploy/caddy-watcher.conf` 放到 Caddy 的 systemd drop-in 目录。重新载入服务配置，启用网站、worker 和 Caddy。
6. 域名指向服务器，仅公开网站入口所需的 80/443 端口，5000 保持绑定本机。Caddy 环境文件中的域名用于证书申请；网站只信任这一层反向代理。
7. 设置独立备份位置 `WATCHER_BACKUP_COPY_DIR` 时，同时将该位置加入 worker 服务的 `ReadWritePaths`。进程日志由 systemd 管理，可选用提供的 journald 限额配置；worker 自有日志每份 2MB，保留三份轮转。
8. 核验 HTTPS 登录、CSRF、跨账户隔离、任务执行、备份恢复，并在目标服务器运行相同压测，再决定开放规模。

健康入口：`/health/live` 检查网站，`/health/ready` 检查数据库与最近三分钟的 worker 心跳。管理员在“系统管理 → 存储管理”查看真实数据、缓存和备份大小，保存保留规则、手动清理并导出/合并数据。`/api/tasks/<id>` 仅供管理员查看持久任务状态。

## 备份、迁移和回退

- 每日使用 SQLite backup API 保存已提交数据，打包业务库与精简目录；本地及可选独立位置默认各保留最近七份，按数据库 `backup_keep_count` 配置调整（1–100）。网页下载的数据备份不参与自动轮转。
- `python scripts/backup_runtime.py` 手动备份；恢复到新的空目录：`python scripts/backup_runtime.py --restore BACKUP.zip --destination EMPTY_DIRECTORY`。恢复时检查文件大小与 SQLite 完整性，完成后停服切换数据目录。
- 从旧调查库升级：先运行 `scripts/build_runtime_catalog.py`，确认所有核验样例通过；停服后运行 `scripts/activate_lightweight.py --archive-legacy --clean-development-backups`。该专用切换脚本使用默认 `WATCHER_DATA_DIR` 下的 `school_watcher.db`，不适用于自定义其他业务数据库文件名。
- 切换前保存压缩业务备份；逐表比对用户、订阅、已读、收藏、来源和通知原有字段的数量及摘要。旧调查库压缩后完整解压计算 SHA-256，确认一致才移除原文件。
- 旧调查归档在七天后第一次维护任务中清理。回退到旧程序必须先停止新网站及 worker，恢复匹配版本的业务备份，再解压旧调查库；不能用旧代码直接打开新结构的业务库。
- 用户此前留下的两份 `.bak` 历史备份没有混入“本次开发重复备份”清理范围。

## 本机验收结果（2026-09-21）

- 来源目录：201 所学校，旧调查库 5,247,250,432 字节，精简目录 133,279,744 字节，49 组核验样例通过；这不代表全校全部来源覆盖率已经达标。
- 业务库整理后 117,166,080 字节；两个运行数据库合计 250,445,824 字节（约 239MiB）。迁移时 5,571 条通知、6 条订阅、2,975 条已读等原有记录完全一致。
- 旧调查回退归档 878,980,224 字节（约 838MiB），保留至 2026-09-28 后维护任务清理。项目总占用应另外计入此归档、备份、日志和测试产物。
- 最终运行检查：项目全目录合计 1,540,564,051 字节（约 1.54GB／1.43GiB）。其中运行数据库及旁文件 250,929,232 字节，备份 356,781,414 字节，回退目录 878,980,467 字节，其他数据 34,501,772 字节，代码及其他项目文件 19,371,166 字节。该快照包含已有两份历史 `.bak`、迁移前备份和首份每日备份；完整逐文件清单见审计文件。后续正常采集和每日备份会改变总量。
- 305 项自动测试、旧结构迁移与模型比对、可选依赖缺失检查通过。浏览器验证了桌面及手机的订阅、阅读、收藏、归档、正文加载及失败重试、精简目录和存储入口。
- 20 个并发用户、10,000 条样例通知、持续十分钟：12,000 次 HTTP 请求，P95 为 130.97ms，无请求错误和数据库锁错误；独立进程同时完成 284 轮采集，重复采集样例仅保留一条。
- 压测使用固定的外部页面响应，验证真实解析、入库和任务执行；未将高校官网网络延迟或本机结果当成正式服务器承载保证。
- 独立 worker 的实际同步、每日来源检查、缓存维护和备份均已完成，无失败任务；网站与 worker 重启后健康检查正常。首份完整每日备份恢复到隔离目录后，业务库和 201 校目录完整性检查通过，恢复出 4 个账户、6 条订阅、5,769 条通知及 2,975 条已读记录。比迁移时多出的 198 条通知来自正常官网同步。

对应审计结果位于 `data/source-audits/runtime-catalogue-migration.json`、`lightweight-cutover.json`、`runtime-load.json`、`backup-restore.json` 和 `runtime-final.json`。
