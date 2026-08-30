# 🏫 学校通知扒取工具（School Notification Watcher）

聚合多所大学官网通知，**DeepSeek AI 智能摘要**，自动发现院系栏目，干净美观的 Web UI，手机电脑都能用。
**多用户 + 订阅驱动**：注册账户、订阅学校，有人订阅才抓取；每用户独立已读状态。

## ✨ 功能特点

- 🔍 **自动站点发现** — 无需手写选择器，输入学校官网即可自动分析导航结构、识别院系栏目、探测通知列表
- 🧠 **通用 DOM 结构分析** — 启发式算法自动识别任意网站的通知列表、正文区域、翻页方式，不依赖硬编码模板
- 🤖 **AI 智能摘要** — DeepSeek 为每条通知生成 ≤100 字摘要
- 🕷️ **双重反爬引擎** — `curl_cffi` 模拟 Chrome TLS 指纹 + Playwright Chromium 渲染 JS 页面（智能回退）
- 🩹 **选择器自愈** — Scrapling 保存元素签名，网站改版后自动修复失效选择器
- 🔄 **智能变更检测** — 内容哈希 + URL 指纹 + 日期比较，不遗漏更新
- ⚡ **增量抓取** — 连续命中已存在通知即停止翻页，二次全校抓取从 25 分钟降至秒级
- 📂 **部门树归类** — 通知按「导航分组 → 部门 → 年月」三级归档，父子部门收展
- 📰 **DAILY NEWS** — 近 7 天全校通知速览，按今天/昨天/前天滚动归类
- ✅ **一键已读** — 首页 / 学校页一键标记全部未读
- 🔒 **安全认证** — 密码登录 + 密保问题找回 + HTML 清洗 + 安全响应头
- 📱 **移动优先** — 响应式设计，手机浏览体验完美
- ⏰ **定时抓取** — APScheduler 后台定时检查更新（默认 30 分钟，可配置）

## 🛠️ 技术栈

| 层 | 技术 |
|------|------|
| 后端 | Python 3 + Flask（应用工厂 + 蓝图） |
| 数据库 | SQLite + Flask-SQLAlchemy + Flask-Migrate（Alembic） |
| 爬取 | BeautifulSoup4 + lxml + curl_cffi（TLS 指纹）+ Scrapling（选择器自愈）+ Playwright（JS 回退） |
| AI | DeepSeek API（OpenAI 兼容 SDK，`deepseek-chat`） |
| 调度 | APScheduler（后台定时任务） |
| 前端 | Jinja2 模板 + 原生 JS/CSS + SSE（实时进度） |

## 🚀 快速开始

### 1. 安装

双击 `setup.bat`，自动完成依赖安装、数据库迁移和 Playwright Chromium 安装。

> 依赖安装到全局 Python（`python` / `pip`），无需 venv。

或手动：

```bash
pip install -r requirements.txt

# 数据库迁移
flask --app app db upgrade

# 可选：安装 Playwright Chromium（用于 JS 渲染页面回退）
python -m playwright install chromium
```

### 2. 启动

双击 `run.bat`（自动启动服务并打开浏览器），或在终端：

```bash
python app.py
```

浏览器访问 **http://localhost:5000**

### 3. 首次使用

1. **登录**：多用户体系。存量部署的原访问密码自动成为 `admin` 账户密码；新用户可注册
2. **订阅学校**：「学校目录」订阅学校；提交新学校 = 创建 + 自动订阅 + 立即首抓（订阅驱动抓取）
3. **设置 API Key**：admin 打开「平台设置」填入 DeepSeek API Key（加密存储，需设 `FIELD_ENC_KEY` 环境变量启用加密）
   - 在 https://platform.deepseek.com 注册获取（以 `sk-` 开头）
4. **自动发现部门**：管理页「🔍 自动发现」→ 预览候选部门 → 勾选并应用
5. **抓取**：订阅后的学校按定时间隔自动抓取；admin 可手动「刷新」
6. **生产部署**：`waitress-serve --threads=8 --port=5000 app:app`（跨平台）；多进程时调度器自动文件锁单实例

### 4. 在手机上访问

#### 方法一：同一 WiFi（免费）

手机和电脑连同一 WiFi，手机浏览器访问：

```
http://你的电脑IP:5000
```

查看电脑 IP：终端运行 `ipconfig`，找 IPv4 地址。

#### 方法二：ngrok 内网穿透（推荐，免费）

1. 在 https://ngrok.com 注册，下载 ngrok
2. 运行：`ngrok http 5000`
3. 手机上访问 ngrok 生成的公网地址（如 `https://xxxx.ngrok-free.app`）

## 🧩 配置说明

### 学校配置（config.yaml）

`config.yaml` 作为种子配置，**启动时自动导入数据库**（只增不更新）。也可在「管理」页面通过 UI 或自动发现添加学校。

```yaml
schools:
  - name: "中国石油大学（北京）克拉玛依校区"
    url: "https://www.cupk.edu.cn"
    enabled: true
    departments:
      - name: "校区通知公告"
        list_url: "https://www.cupk.edu.cn"
        list_selector: "li.c-notice__half-li"
        title_selector: "a"
        link_selector: "a"
        date_selector: ".c-notice__left-date"
        content_selector: "div.c-main__right"
```

> ⚠️ YAML 只增不更新，修改已有部门请通过 API `PUT /api/departments/<id>` 或管理界面完成。

### CSS 选择器字段说明

| 字段 | 说明 | 示例 |
|------|------|------|
| 列表项选择器 | 每条通知的容器 | `li.news-item` 或 `div.news-list > div` |
| 标题选择器 | 标题元素（相对列表项） | `a.news-title` 或 `h3 a` |
| 链接选择器 | 链接元素 | 同上 |
| 日期选择器 | 日期元素 | `span.date` 或 `.time` |
| 正文选择器 | 详情页正文容器 | `div.article-content` 或 `.entry-content` |

> 💡 管理界面内置「🧪 测试选择器」按钮，可实时验证 CSS 选择器是否匹配到条目。自动发现会自动完成以上配置，无需手动填写。

## 📁 项目结构

```
school-watcher/
├── app.py                     # Flask 入口
├── config.yaml                # 学校配置种子文件（启动时导入）
├── requirements.txt
├── scripts/                   # Windows 启动 / 安装脚本
│   ├── setup.bat              # 一键安装
│   ├── run.bat                # 一键启动
│   └── school-notifier.bat    # 桌面快捷方式指向的启动脚本
├── backend/                   # 后端包（分层）
│   ├── __init__.py            # create_app 应用工厂
│   ├── core/                  # 核心：路径常量 + YAML 导入 + 扩展单例
│   │   ├── config.py
│   │   └── extensions.py
│   ├── auth/                  # 认证与安全
│   │   ├── auth.py            # 密码 / 密保 / 速率限制 / 钩子
│   │   └── security.py        # 安全响应头
│   ├── ai/                    # DeepSeek 摘要
│   ├── database/              # ORM 模型 + db（5 张表）
│   ├── routes/                # 蓝图：页面 + API
│   ├── scheduler/             # APScheduler 定时任务
│   ├── scraper/               # 爬虫（按职责分包）
│   │   ├── engine.py          # 核心引擎
│   │   ├── change_detector.py # 变更检测
│   │   ├── sanitizer.py       # HTML 清洗
│   │   ├── fetchers/          # 抓取器（Playwright / Selenium）
│   │   ├── detectors/         # DOM 检测 / 分析器
│   │   ├── discovery/         # 站点发现
│   │   ├── selector/          # 选择器学习 / 自愈
│   │   └── progress/          # 抓取进度
│   └── services/              # 部门树构建 + 发现编排
├── frontend/                  # 前端
│   ├── static/                # CSS + JS + 图片
│   └── templates/             # HTML 模板（登录/首页/学校/通知/管理/设置/找回）
├── migrations/                # Alembic 数据库迁移
├── data/                      # SQLite 数据库 + 抓取数据（已 gitignore）
└── docs/                      # 项目文档（见 docs/README.md 文档导航）
```

## 🔌 主要 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/scrape/<id>` | 手动触发爬取（后台 + SSE 进度） |
| POST | `/api/scrape/all` | 触发全部启用学校爬取 |
| GET | `/api/scrape/<id>/events` | SSE 实时进度流 |
| POST | `/api/summarize` | 批量生成 AI 摘要 |
| GET/POST | `/api/schools` | 学校列表 / 添加学校 |
| GET | `/api/schools/suggest-url` | 校名 → 官网 URL 建议 |
| POST | `/api/schools/<id>/discover` | 触发站点自动发现 |
| POST | `/api/schools/<id>/discover/apply` | 应用发现的部门 |
| POST | `/api/departments/test-selectors` | 测试 CSS 选择器 |
| POST | `/api/announcements/read-all` | 一键已读 |

## 🧑‍💻 开发

```bash
# 修改 models.py 后生成迁移
python -m flask --app app db migrate -m "描述这次变更"

# 应用迁移
python -m flask --app app db upgrade
```

> 文档导航见 [docs/README.md](docs/README.md)，详细架构见 [docs/DATABASE.md](docs/DATABASE.md)，踩坑与历史见 [docs/HANDOFF.md](docs/HANDOFF.md)。

## ❓ 常见问题

**Q: 爬取不到通知？**
- 先确认学校/部门已通过「自动发现」配置好（空选择器时首次抓取会自动探测）
- 部分学校网站有反爬机制，可尝试在设置中调整；JS 渲染页面会自动触发 Playwright 回退

**Q: 摘要生成失败？**
- 确认 DeepSeek API Key 有效（以 `sk-` 开头）且余额充足
- 检查网络是否能访问 api.deepseek.com

**Q: 如何添加更多学校？**
- 「管理」页面 → 添加学校 → 「🔍 自动发现」→ 勾选应用 → 「刷新」

**Q: 忘记登录密码？**
- 登录页 →「忘记密码」→ 回答密保问题重置（需先在设置页配置密保问题）
