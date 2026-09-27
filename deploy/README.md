# 部署与维护

Windows 本机使用 SQLite；Linux 多人部署使用 PostgreSQL 和 Docker Compose。网站、采集进程与浏览器服务分别运行，共用同一个数据目录和实例配置。目录库使用单机持久化磁盘，不跨机器共享 SQLite/WAL 文件。

## Windows 本机

在项目根目录运行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-browser.txt
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe scripts\launch_desktop.py
```

也可使用 `scripts/setup.bat` 和 `scripts/run.bat`。桌面启动器负责迁移检查、进程启动和浏览器调用凭证，重复点击不会重复启动服务。浏览器凭证保存在数据目录的 `browser-service.token`；日志也位于数据目录。

默认浏览器并发为 1。设置 `WATCHER_BROWSER=0` 可只运行静态采集；使用已安装的 Edge 时设置 `WATCHER_BROWSER_CHANNEL=msedge`。

手动分开启动时，在三个终端中配置相同的环境变量：

| 环境变量 | 值 |
| --- | --- |
| `WATCHER_BROWSER` | `1` |
| `WATCHER_BROWSER_TOKEN` | 自行生成的同一随机凭证，至少 32 字符 |
| `WATCHER_BROWSER_URL` | `http://127.0.0.1:8765` |
| `WATCHER_ORIGIN_BROKER_URL` | `http://127.0.0.1:5000/internal/browser-origin` |

依次运行 `python app.py`、`python scripts/run_browser.py --supervise`、`python scripts/run_worker.py`。注册账户后，可运行 `python -m flask --app app promote-user 用户名` 设置管理员。

## Linux Docker Compose

1. 将 `deploy/server.env.example` 复制为 `deploy/server.env`。按示例填写域名，并为数据库密码、会话密钥、凭据加密密钥和浏览器凭证分别生成不同随机值：

   ```sh
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

2. 将 `WATCHER_PUBLIC_ORIGIN` 设置为完整的 HTTPS 网站来源，例如 `https://watcher.example.com`。使用非默认端口时需要包含端口，不附加路径。
3. 在项目根目录构建并启动：

   ```sh
   docker compose --env-file deploy/server.env -f deploy/compose.yaml build
   docker compose --env-file deploy/server.env -f deploy/compose.yaml up -d
   ```

4. 将 HTTPS 反向代理转发到 `127.0.0.1:8080`，保留 Host、Origin 和 `X-Forwarded-Proto`。远程验证需要 WebSocket 转发；该路径的访问日志不应记录查询字符串。可参考本目录的 Caddy 和 Nginx 配置。
5. 注册账户后设置管理员：

   ```sh
   docker compose --env-file deploy/server.env -f deploy/compose.yaml exec web python -m flask --app app promote-user 用户名
   ```

Compose 在持久化卷中保存业务数据和目录，初始化服务负责数据库迁移。数据库、浏览器 API 和 VNC 使用内部网络；外部只需开放 HTTPS 网站入口。浏览器使用非 root 用户、Chromium 沙箱及受限出口代理。

默认浏览器并发为 1，HTTP worker 并发为 4，网站为一个进程。根据实际队列等待、数据库连接数和内存调整配置。需要增加同机网站进程时：

```sh
docker compose --env-file deploy/server.env -f deploy/compose.yaml up -d --scale web=4
docker compose --env-file deploy/server.env -f deploy/compose.yaml restart gateway
```

所有进程使用相同会话密钥与 PostgreSQL 配置。实例数量和连接池总量应与服务器、数据库容量匹配；上线前在目标环境确认任务处理、浏览器验证和备份恢复。

## 升级与旧目录迁移

升级前停止网站、采集和浏览器进程，保留完整备份，再运行 `python scripts/migrate_safely.py`。新旧代码不要同时写入同一个数据库。Compose 部署由初始化服务执行迁移。

旧版本存在大型 `source_inventory.sqlite3` 调查库时：

1. 运行 `python scripts/build_runtime_catalog.py`，生成并核对 `source_catalog.build.sqlite3`。
2. 停止旧服务，运行 `python scripts/activate_lightweight.py --archive-legacy` 完成切换。
3. 确认账户、订阅、通知及新目录可用后再恢复服务。保管与旧程序匹配的业务备份和调查库归档，以便回退。

这两个工具使用 `backend/resources/source_baselines/` 中的目录校验数据。切换工具适用于当前数据目录中使用默认名称 `school_watcher.db` 的 SQLite 部署。

## 完整搬迁与备份恢复

管理页的通知 ZIP 用于增量合并，不包含账号和个人状态。完整实例搬迁应使用数据库迁移及完整备份工具。

SQLite 搬迁至 PostgreSQL 时，停止所有旧进程并准备空目标数据库。把目标连接地址放入 `WATCHER_MIGRATION_DATABASE_URL`，然后运行：

```sh
python scripts/migrate_database.py --source /old/data/school_watcher.db --catalog /old/data/source_catalog.sqlite3 --destination-data /new/data --stopped
```

工具从源库的一致快照中迁移账号、订阅、通知、配置与个人状态，并核对目标数据。将原 `SECRET_KEY` 和凭据加密密钥安全配置到新环境，详见 [AI 配置](AI_CONFIGURATION.md)；浏览器验证会话需要重新建立。确认目标数据后只启动新环境。

创建完整备份：

```sh
python scripts/backup_runtime.py
```

SQLite 使用一致备份，PostgreSQL 使用原生 `pg_dump`。备份包含配套的目录文件和校验清单，不包含环境文件、加密主密钥及浏览器凭证；这些配置需要单独保管。

恢复到新的空目录：

```sh
python scripts/backup_runtime.py --restore BACKUP.zip --destination EMPTY_DIRECTORY
# PostgreSQL：DATABASE_URL 指向另一个空数据库
python scripts/backup_runtime.py --restore BACKUP.zip --destination EMPTY_DIRECTORY --postgres
```

先在恢复环境核对账户、订阅、通知和个人状态，再停服切换。回退时使用匹配的旧代码和备份，不用旧程序直接打开已升级的数据库。

## 日常管理

- `/health/live` 检查网站存活，`/health/ready` 检查运行依赖和 worker 心跳。
- 系统管理 → 处理访问验证：管理员在专用浏览器中完成验证，再由系统检查内容并恢复任务。Linux 使用管理页中的远程浏览器，Windows 使用专用窗口。
- 系统管理 → 存储管理：查看用量、调整保留期、手动清理和导入导出通知。历史通知采用增量合并；抓取失败或官网撤下内容不会删除已有通知。
- 管理员接口 `/api/admin/runtime/metrics` 提供队列状态和角色心跳；容器资源可通过 `docker stats` 查看。
- AI 接口配置见 [AI 配置](AI_CONFIGURATION.md)，浏览器服务选项见 [浏览器服务说明](../backend/browser_service/README.md)。
