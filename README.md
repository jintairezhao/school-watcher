# School Watcher · 学校通知

把关注的高校、学院和栏目，放进同一个通知收件箱。Windows 和 macOS 本机桌面应用，源码与安装包使用同一套界面和功能，无需账号。

**源码可见 · 仅限非商业用途**。适用于所有个人与机构，修改版本也受限制；使用前请阅读 [LICENSE](LICENSE)。

[快速开始](#快速开始) · [部署与维护](deploy/README.md) · [AI 配置](deploy/AI_CONFIGURATION.md) · [参与贡献](CONTRIBUTING.md) · [安全政策](SECURITY.md)

## 可以做什么

- **按学校和栏目订阅**：按官网分组、部门和栏目选择范围，在本机保留订阅、已读和收藏状态。旧版已归档通知会显示在收件箱中，内容和个人记录保持不变。
- **聚合阅读**：并排或专注阅读，支持搜索记录、即时匹配、学校／部门／时间／阅读状态筛选，以及浅色和深色主题。
- **静态与动态采集**：普通 HTTP 和独立 Playwright 服务协同处理公开页面；需要人工验证时，在应用提供的专用浏览器中处理。
- **自动发现与接入**：沿官网导航查找部门和通知栏目，首次正确抓取后即可使用；常见结构直接解析，特殊页面可选 AI 辅助，单个页面失败不影响其他栏目。
- **可选 AI 摘要**：用户主动请求生成，同一通知的有效摘要共享复用。没有 API 密钥也可订阅、抓取和阅读。
- **数据维护**：可调整缓存保留期、手动清理、导出通知并增量导入历史备份；完整实例另有数据库搬迁和备份工具。

同步采用增量并集：官网撤下通知、暂时抓取失败或导入历史备份，都不会因此删除已有通知。正文按需获取，缓存清理不删除通知索引和个人阅读记录。

## 适用范围与限制

内置 200 所高校的官网入口，支持追加学校。**学校在名单中不代表其所有部门和栏目都已验证可抓取**；订阅后会按需发现和采集，来源状态可在界面查看。

- 面向无需校内登录的公开信息。动态渲染、验证码、访问拒绝和官网改版可能需要人工处理或补充适配；不保证任意网站和挑战都可自动通过。
- 不与高校存在官方隶属或授权关系。通知以原文为准，AI 摘要可能有误，报名时间、资格和附件请核对官网。
- 使用者需遵守目标网站的访问规则和适用法律，合理设置频率，勿用于绕过账号权限或收集非公开资料。

## 快速开始

### Windows / macOS 桌面版

从 [Releases](https://github.com/jintairezhao/school-watcher/releases) 下载 Windows x64 安装程序，或适合 Apple 芯片／Intel Mac 的 DMG。桌面版在独立窗口中打开即用，无需账号和 Python，可直接管理系统。优先使用已有 Edge／Chrome，缺少时自动下载独立采集组件；应用菜单的「检查更新」可以下载新版并确认安装。全新安装不预选学校，从学校目录订阅后建立部门树并逐步接入栏目，AI 为可选辅助。安装说明、数据位置及版本发布流程见 [桌面版说明](deploy/DESKTOP.md)。

### 源码运行（与桌面版一致）

使用 Python 3.14.3。Windows 可依次双击 `scripts/setup.bat`、`scripts/run.bat`；也可在项目根目录运行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements/browser.txt -r requirements/desktop.txt
.venv\Scripts\python.exe app.py
```

macOS 使用 `.venv/bin/python` 执行相同安装与启动命令。程序直接打开桌面窗口，无需注册、登录或设置管理员。首次启动自动准备数据库和采集组件；退出窗口时停止本机服务。

源码与安装版默认使用同一桌面数据目录。旧源码的 `data/` 可以通过 `python app.py --data-dir "原数据目录的绝对路径"` 使用，已有数据不会被自动挪动。运行方式、升级和恢复见 [维护说明](deploy/README.md)。

0.2.0 已移除账号及多人网站功能，不再提供公网网站部署入口。历史数据库的内部归属记录保留，以兼容订阅、收藏和阅读记录。

## AI 配置

AI 是可选能力，不随项目提供密钥或免费额度。在本机系统管理中配置服务，密钥和用量归本机使用者管理。

在「系统管理」分别配置目录与摘要用途的服务，连接测试通过后启用。支持的服务与模型以管理页为准；密钥加密保存，消费由服务商计费。

通知摘要由显式操作触发，读取页面不会暗中创建付费摘要任务。启用后会向所选服务发送相应用途的页面证据或通知文本；详情、用量与密钥迁移见 [AI 配置说明](deploy/AI_CONFIGURATION.md)。

## 数据与升级

- [存储管理与通知合并](deploy/DATA_MANAGEMENT.md)：清理缓存、调整保留期、导出与增量导入。
- [完整搬迁与备份恢复](deploy/README.md#完整搬迁与备份恢复)：保留订阅、配置及阅读状态；通知导出包不能代替完整备份。
- [升级与旧目录迁移](deploy/README.md#升级与旧目录迁移)：先停服并备份，再执行迁移，避免新旧代码同时写库。

运行数据、凭据、浏览器会话、个人计划和执行记录不属于源码发布包。

## 开发与验证

项目按用途组织，目录入口如下：

| 位置 | 内容 |
| --- | --- |
| `app.py` | 桌面源码启动入口 |
| `backend/` | 路由、数据模型、采集、任务、浏览器服务和 AI 功能 |
| `frontend/` | 页面模板、样式、脚本与字体 |
| [`config/`](config/README.md) | 学校种子与公开抓取适配配置 |
| [`requirements/`](requirements/README.md) | 按基础、浏览器、桌面和测试场景拆分的依赖 |
| [`scripts/`](scripts/README.md) | 启动入口，以及分类存放的维护、来源、检查和资源工具 |
| [`deploy/`](deploy/README.md) | 桌面安装、发布、升级和维护说明 |
| `migrations/` | 数据库版本迁移 |
| [`tests/`](tests/README.md) | 自动测试、基础检查、浏览器检查与测试样本 |
| `licenses/` | 随项目分发的第三方许可原文 |
| `data/`、`.local/` | 本机运行数据与私人开发归档，均不发布 |

根目录的 `requirements.txt` 保留为基础安装入口。数据库、凭据和浏览器会话放在数据目录或环境配置中，不放进公开的 `config/`。

技术栈：Python、Flask、SQLAlchemy、Jinja、原生 JavaScript/CSS、Playwright。桌面数据使用 SQLite，目录发布库保留在同一本机数据位置。

```sh
python -m pip install -r requirements/test.txt
python -m unittest discover -s tests -p "test_*.py"
python tests/smoke/check_migration.py
python tests/smoke/check_minimal_runtime.py
```

请在独立开发目录执行测试。涉及历史数据库迁移、真实浏览器或安装包的验收不能由单元测试代替；跳过的测试也不计为通过。环境要求、贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，自动化定义见 [Runtime acceptance](.github/workflows/runtime.yml)。

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
