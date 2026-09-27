# 统一抓取与多人部署运行手册

本版使用同一套业务代码：Windows 本机用 SQLite，Linux 多人服务用 PostgreSQL。HTTP、浏览器与人工验证共享持久任务身份；网页采集失败不会删除已有通知。代码与测试已经交付，但 Linux 容器、远程验证及指定 8 核 16GB 服务器的持续容量验收仍需在对应环境执行。具体实测与未通过项见 [验收记录](RUNTIME_ACCEPTANCE.md)。

## Windows 本机

经本轮验证的版本为 Python 3.14.3、Playwright 1.61.0；运行依赖及传递依赖分别锁定在 `requirements*.txt` 和 `requirements-constraints.txt`。

在项目目录安装：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-browser.txt
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe scripts\launch_desktop.py
```

桌面启动器会启动网站、一个兼任各角色的采集进程和独立浏览器监督进程；服务日志在数据目录。浏览器调用凭证首次生成到 `browser-service.token`，不会进入通知导出或完整数据库备份。默认浏览器并发 1；设置 `WATCHER_BROWSER=0` 可只运行静态采集。捆绑 Chromium 安装失败时，可以显式设置 `WATCHER_BROWSER_CHANNEL=msedge` 使用已安装 Edge；代码没有个人浏览器路径或固定伪装版本。

升级前停止旧网站、worker 和浏览器监督进程，再运行 `scripts/migrate_safely.py`。该工具升级前保存数据库快照。不要让新旧代码同时写同一个数据库。

手动分开启动服务时，需要给三个进程设置相同的 `WATCHER_BROWSER_TOKEN`（至少 32 字符）、`WATCHER_BROWSER_URL=http://127.0.0.1:8765`、`WATCHER_ORIGIN_BROKER_URL=http://127.0.0.1:5000/internal/browser-origin` 和 `WATCHER_BROWSER=1`。依次运行 `app.py`、`scripts/run_browser.py --supervise`、`scripts/run_worker.py`。API 和 VNC 端口仅限本机或内部网络。

## Linux Docker Compose

使用单台应用主机、本机持久化卷，不跨机器共享 SQLite/WAL 文件。Compose 提供 PostgreSQL、初始化、网站、HTTP worker、浏览器 worker、目录 worker、调度器、浏览器服务、受限出口代理和 Nginx。普通 worker 与网站相互独立；目录写入在主数据库租约和文件锁下串行执行。

1. 将 `deploy/server.env.example` 复制为 `deploy/server.env`。设置域名及四个不同随机密钥。可分别运行 `python -c "import secrets; print(secrets.token_hex(32))"` 生成；数据库密码用这种 URL 安全的值。真实配置文件已加入忽略规则。
2. 设置 `WATCHER_PUBLIC_ORIGIN=https://你的域名`，包含非默认端口时必须完全匹配浏览器 Origin，不要写路径。
3. 构建并启动：

```sh
docker compose --env-file deploy/server.env -f deploy/compose.yaml build
docker compose --env-file deploy/server.env -f deploy/compose.yaml up -d
```

4. 将现有 TLS 反向代理转发到 `127.0.0.1:8080`，保留 Host、Origin 和 `X-Forwarded-Proto`；WebSocket 验证路径关闭查询字符串日志。8080 只绑定主机回环地址，数据库、浏览器与 VNC 没有公开端口。浏览器仅通过出口代理访问公网，代理拒绝内部地址；Chromium 使用非 root 用户并保留沙箱。
5. 注册账户后，通过容器中的命令设为管理员：

```sh
docker compose --env-file deploy/server.env -f deploy/compose.yaml exec web python -m flask --app app promote-user 用户名
```

6. 检查 `/health/live` 与 `/health/ready`，然后使用管理页验证真实来源。首次初始化只运行一次迁移和 YAML 导入，多个网站进程不重复导入初始来源。

小规模起步默认浏览器并发 1、HTTP worker 4 个执行线程、一个网站进程。配置是否适合 2 核 8GB 仍取决于动态来源比例与频率，应按实际监测调整。容量测试配置可设置浏览器并发 4，并通过以下命令增加同机网站进程：

```sh
docker compose --env-file deploy/server.env -f deploy/compose.yaml up -d --scale web=4
docker compose --env-file deploy/server.env -f deploy/compose.yaml restart gateway
```

Nginx 重新解析网站服务的多个地址；所有实例共享会话密钥与 PostgreSQL。增加网站进程后要核对连接池总量，不能无限增加 worker。默认每个业务进程连接池 4、额外连接 4；多个角色与网站实例的合计应低于数据库连接限制。

镜像锁定 Python 3.14.3、PostgreSQL 18.6，`pg_dump/pg_restore` 安装同一主版本。浏览器构建时安装 Playwright 1.61.0 对应的 Chromium。OS 软件包仍接收对应发行版仓库更新；正式发布时应保存构建镜像摘要与 `dpkg-query -W` 清单，以重现实际发布镜像。此工作区没有 Docker，不能把 YAML 静态检查视为 Linux 部署成功。

## 管理员操作

- 系统管理 → 处理访问验证：打开真实官网浏览器，完成验证后点击“验证已完成”。系统重新读取目标内容，校验通过才恢复任务。Windows 打开专用窗口；Linux 管理页通过 noVNC 操作服务器窗口。一次只处理一个来源，10 分钟到期回收。完成、取消、超时会撤销票据并关闭连接。
- 系统管理 → 抓取任务：检查间隔 5–720 分钟，默认 30。任务错峰入队，正文优先级随等待时间让位；过载时合并需求，不重复请求同一来源。
- 系统管理 → 存储管理：支持手动清理与保留期修改。证据缓存上限 100MB，活动任务证据受保护；空间不足时不再写缓存，恢复可重新获取页面。保留期 0 只禁用按年龄淘汰，容量限制仍有效。目录旧版本至少保留 7 天，当前激活版本不清理。

自动挑战失败、持续拒绝或人工验证未完成，都会保留原有通知。真实网站可能绑定浏览器环境、访问地址与验证有效期；不要把会话跨机器迁移视为可靠恢复方式。

## 完整搬迁与恢复

通知 ZIP 用于数据库无关的增量合并，不包含账号和个人状态。完整搬迁工具另行保留账号、密码摘要、订阅、配置、收藏、归档、已读、历史通知及来源关系。

SQLite → PostgreSQL：先停止所有旧进程，准备空目标数据库与空目录；把目标连接地址放入 `WATCHER_MIGRATION_DATABASE_URL`，密码不要写在命令参数中。

```sh
python scripts/migrate_database.py --source /old/data/school_watcher.db --catalog /old/data/source_catalog.sqlite3 --destination-data /new/data --stopped
```

工具对源库做一致快照，在副本上升级、去重；目标建表、复制、行数验证、序列恢复处于一个 PostgreSQL 事务中。原源库保持不变。复制发布目录后重置活租约和过期验证，未完成任务重新入队。将原 `SECRET_KEY`、`FIELD_ENC_KEY` 安全配置到新环境；浏览器验证会话重新建立。确认新库后只启动新环境，避免双写。

完整备份：`python scripts/backup_runtime.py`。SQLite 使用一致备份 API；PostgreSQL 使用原生自定义格式 `pg_dump`。主库快照与目录激活指针属于同一版本，包中包含对应目录文件与 SHA-256 清单。环境文件和浏览器凭证不入包，需要单独安全保管。

恢复到空目录：

```sh
python scripts/backup_runtime.py --restore BACKUP.zip --destination EMPTY_DIRECTORY
# PostgreSQL：DATABASE_URL 指向另一个空数据库
python scripts/backup_runtime.py --restore BACKUP.zip --destination EMPTY_DIRECTORY --postgres
```

恢复不覆盖已有数据库。先在隔离目标验证账户、订阅、通知和个人状态，再停服切换；回滚使用配套的旧代码和旧备份，不能直接用旧程序打开升级后的数据库。

## 适配与监测

业务只调用 `backend.scraper.acquisition`。结果明确区分有效、确认空列表、需要脚本、人工验证、拒绝、网络错误、待适配。相对链接、分页和配置校验使用最终 URL 和同一份有效 HTML；挑战页面不得用于修复解析规则。

`WATCHER_SOURCE_PROFILES` 可指定版本化 JSON 配置，按来源、完整 URL 范围和用途匹配，配置冲突直接报错。公共 API 配置必须带已验证标记、证据地址、版本和字段映射；实际启用前核对通知身份、日期、原文与分页，不能猜测接口。配置摘要变化会拒绝旧任务提交。

`GET /api/admin/runtime/metrics`（管理员）返回任务状态/通道数量、最老待执行时间和角色心跳。浏览器私有 `/health` 返回存活会话与执行数量；容器内存使用 `docker stats` 记录。官网额度按页面访问占用来源域名，跳转和资源涉及的新域名也需数据库许可；同页的 JS、CSS、XHR 共用该页面许可，不逐个资源额外计一次访问间隔。

`.github/workflows/runtime.yml` 提供 Windows 与 Linux/PG/Compose 验证入口。容量脚本 `scripts/acceptance_load.py` 默认创建隔离 SQLite；设置 `WATCHER_LOAD_DATABASE_URI` 可改用专用空 PostgreSQL。`--url` 模式只读取已部署的测试环境，凭据通过 `--cookies` 文件提供。合成传输模式不启动真实浏览器，报告明确区分结果；最终应在 8 核 16GB 基准机以真实静态/动态/受限混合页面持续运行至少两个 30 分钟周期，保存队列等待、P95、浏览器内存和来源成功率。

实现依据：[Playwright 页面 API](https://playwright.dev/python/docs/api/class-page)、[浏览器部署与沙箱](https://playwright.dev/python/docs/docker)、[PostgreSQL Debian 官方仓库](https://www.postgresql.org/download/linux/debian/)、[Cloudflare 验证凭证限制](https://developers.cloudflare.com/cloudflare-challenges/concepts/clearance/)。
