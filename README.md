# School Watcher · 学校通知

把关注的高校、学院和栏目，放进同一个通知收件箱。支持本机运行和自建服务器，按来源筛选、搜索、阅读、收藏与归档。

**源码可见 · 仅限非商业用途**。适用于所有个人与机构，修改版本也受限制；使用前请阅读 [LICENSE](LICENSE)。

[快速开始](#快速开始) · [部署与维护](deploy/README.md) · [AI 配置](deploy/AI_CONFIGURATION.md) · [参与贡献](CONTRIBUTING.md) · [安全政策](SECURITY.md)

## 可以做什么

- **按学校和栏目订阅**：同一来源共享采集，个人分别保留订阅、已读、收藏和归档状态。
- **聚合阅读**：并排或专注阅读，支持搜索记录、即时匹配、学校／部门／时间／阅读状态筛选，以及浅色和深色主题。
- **静态与动态采集**：普通 HTTP 和独立 Playwright 服务协同处理公开页面；需要人工验证时，由管理员在专用浏览器中处理。
- **目录发现与核实**：根据官网证据发现部门和栏目，保留来源路径；可选 AI 辅助分类，异常进入核实流程。
- **可选 AI 摘要**：用户主动请求生成，同一通知的有效摘要共享复用。没有 API 密钥也可订阅、抓取和阅读。
- **数据维护**：可调整缓存保留期、手动清理、导出通知并增量导入历史备份；完整实例另有数据库搬迁和备份工具。

同步采用增量并集：官网撤下通知、暂时抓取失败或导入历史备份，都不会因此删除已有通知。正文按需获取，缓存清理不删除通知索引和个人阅读记录。

## 适用范围与限制

内置 200 所高校的官网入口，支持追加学校。**学校在名单中不代表其所有部门和栏目都已验证可抓取**；订阅后会按需发现和采集，来源状态可在界面查看。

- 面向无需校内登录的公开信息。动态渲染、验证码、访问拒绝和官网改版可能需要管理员处理或补充适配；不保证任意网站和挑战都可自动通过。
- 不与高校存在官方隶属或授权关系。通知以原文为准，AI 摘要可能有误，报名时间、资格和附件请核对官网。
- 使用者需遵守目标网站的访问规则和适用法律，合理设置频率，勿用于绕过账号权限或收集非公开资料。
- 100 并发用户、1000 个来源是容量验收目标，不是已经验证的性能承诺；服务器大小应根据实际来源构成、队列和内存使用调整。

## 快速开始

### Windows / macOS 桌面版

桌面分发方案提供独立应用窗口、内置本机服务和 Chromium，并支持从 GitHub Release 检查和下载安装更新。安装包、数据位置及版本发布流程见 [桌面版说明](deploy/DESKTOP.md)。首次发布以前仍可使用下方源码安装方式；实际可用安装包以 [Releases](https://github.com/jintairezhao/school-watcher/releases) 为准。

### Windows 本机

使用 **Python 3.14.3**（当前依赖与 CI 配置版本）。首次安装需要联网下载 Python 依赖和匹配的 Chromium；日常界面字体由应用自身提供。

1. 下载仓库 ZIP 并解压，或安装 Git 后运行：

   ```sh
   git clone https://github.com/jintairezhao/school-watcher.git
   cd school-watcher
   ```

2. 双击 `scripts/setup.bat`。它会创建 `.venv`，安装基础与浏览器依赖、下载 Chromium，并执行数据库迁移。
3. 双击 `scripts/run.bat`，打开 [本地网站](http://127.0.0.1:5000)，注册账户。
4. 进入「学校目录」订阅学校，再选择关注的栏目，回到收件箱阅读。首次发现和采集需要时间。

也可在项目根目录用 PowerShell 执行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements/browser.txt
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe scripts\launch_desktop.py
```

桌面启动器管理网站、采集和浏览器进程，重复启动不会重复创建服务。默认仅本机可访问，使用 SQLite，无需 Redis、独立数据库或 AI 密钥。

如需管理来源、存储或 AI，在注册账户后，从项目根目录执行：

```powershell
.venv\Scripts\python.exe -m flask --app app promote-user 你的用户名
```

可选本机配置见 [.env.example](.env.example)。默认数据保存在 `data/`，本机密钥自动生成；不要覆盖已有实例的密钥，也不要分享数据目录或 `.env`。手动启动、端口和浏览器下载问题见 [部署说明](deploy/README.md#windows-本机)。

### Linux 服务器

使用 Docker Compose、PostgreSQL 和持久化磁盘。将 [服务器环境示例](deploy/server.env.example) 复制为 `deploy/server.env`，填写独立随机密钥和实际 HTTPS 网站地址，再运行：

```sh
docker compose --env-file deploy/server.env -f deploy/compose.yaml build
docker compose --env-file deploy/server.env -f deploy/compose.yaml up -d
```

公网 HTTPS 反向代理、管理员设置、远程访问验证、升级和恢复步骤见 [部署与维护](deploy/README.md#linux-docker-compose)。不要把数据库、浏览器服务或 VNC 端口直接暴露到公网。

Windows 是当前本机验证环境；Linux／PostgreSQL 提供部署配置与验收工作流，部署者仍需在目标环境完成启动和恢复验证。macOS 尚未验收。

## AI 配置

AI 是可选能力，不随项目提供密钥或免费额度。网站用户使用实例管理员配置的服务；下载到本地运行时，本地部署者就是该实例的管理员。

在「系统管理」分别配置目录与摘要用途的服务，连接测试通过后启用。支持的服务与模型以管理页为准；密钥加密保存，消费由服务商计费。

通知摘要由显式操作触发，读取页面不会暗中创建付费摘要任务。启用后会向所选服务发送相应用途的页面证据或通知文本；详情、用量与密钥迁移见 [AI 配置说明](deploy/AI_CONFIGURATION.md)。

## 数据与升级

- [存储管理与通知合并](deploy/DATA_MANAGEMENT.md)：清理缓存、调整保留期、导出与增量导入。
- [完整搬迁与备份恢复](deploy/README.md#完整搬迁与备份恢复)：保留账号、订阅、配置及个人状态；通知导出包不能代替完整备份。
- [升级与旧目录迁移](deploy/README.md#升级与旧目录迁移)：先停服并备份，再执行迁移，避免新旧代码同时写库。

运行数据、凭据、浏览器会话、个人计划和执行记录不属于源码发布包。

## 开发与验证

项目按用途组织，目录入口如下：

| 位置 | 内容 |
| --- | --- |
| `app.py` | 网站启动入口 |
| `backend/` | 路由、数据模型、采集、任务、浏览器服务和 AI 功能 |
| `frontend/` | 页面模板、样式、脚本与字体 |
| [`config/`](config/README.md) | 学校种子与公开抓取适配配置 |
| [`requirements/`](requirements/README.md) | 按基础、浏览器、服务器和测试场景拆分的依赖 |
| [`scripts/`](scripts/README.md) | 启动入口，以及分类存放的维护、来源、检查和资源工具 |
| [`deploy/`](deploy/README.md) | Docker 部署、代理、系统服务和运维说明 |
| `migrations/` | 数据库版本迁移 |
| [`tests/`](tests/README.md) | 自动测试、基础检查、浏览器检查与测试样本 |
| `licenses/` | 随项目分发的第三方许可原文 |
| `data/`、`.local/` | 本机运行数据与私人开发归档，均不发布 |

根目录的 `requirements.txt` 保留为基础安装入口。数据库、凭据和浏览器会话放在数据目录或环境配置中，不放进公开的 `config/`。

技术栈：Python、Flask、SQLAlchemy、Jinja、原生 JavaScript/CSS、Playwright。Windows 使用 SQLite；多人部署使用 PostgreSQL；目录发布库保留在同机持久化磁盘中。

```sh
python -m pip install -r requirements/test.txt
python -m unittest discover -s tests -p "test_*.py"
python tests/smoke/check_migration.py
python tests/smoke/check_minimal_runtime.py
```

请在独立开发目录执行测试。涉及 PostgreSQL、真实浏览器或生产容量的验收不能由单元测试代替；跳过的测试也不计为通过。环境要求、贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，自动化定义见 [Runtime acceptance](.github/workflows/runtime.yml)。

## 许可与第三方内容

项目自有代码与文档采用 **School Watcher Noncommercial License 1.0**，完整条款见 [LICENSE](LICENSE)。这是项目定制的非商业软件许可。

- 允许符合条款的非商业使用、学习、研究、修改与再分发；分发时必须保留完整许可证和版权声明。
- 禁止商业用途，包括企业业务运营、收费部署或托管、出售软件副本、广告变现，以及集成到商业产品或服务。未获利或未向最终用户收费，不自动等于非商业用途。
- 限制适用于所有主体，学校、公益机构、科研机构及政府机构的商业项目也不例外。
- 修改、改名或重新打包不解除适用于本项目及其修改版本的非商业限制。

以上为阅读说明，具体授权范围以英文 LICENSE 为准。该定制条款尚未经法律专业人士审核，不保证适用于所有法域。

限制商业使用的许可证不符合 [OSI 开源定义](https://opensource.org/osd)，因此这里使用“源码可见”而非“OSI 开源”的表述。

字体、部分图标和浏览器容器配置分别遵循其原始许可证，详见 [第三方声明](THIRD_PARTY_NOTICES.md)。高校网页、通知和附件的权利属于各自权利人，项目代码的许可证不授予这些内容的再利用权。

问题和建议请提交 [Issue](https://github.com/jintairezhao/school-watcher/issues)；涉及凭据或漏洞请遵循 [安全政策](SECURITY.md)，不要公开敏感数据。
