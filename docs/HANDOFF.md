
# HANDOFF.md — 学校通知扒取工具 交接文档

**最后更新**: 2026-07-30（第二十三轮 — 两个致命bug修复：`url`变量名错误 + 后台线程detached session导致UI抓取全部失效）

---
**新会话必读**：本文档是项目唯一真相源。新会话开始时**必须先读本文档**。通过 memory 文件或项目 CLAUDE.md 指示 Claude 加载 HANDOFF.md。

---

## ⛔ 硬性规则

**非必要不往 C 盘塞东西。** 所有项目文件、工具、数据、脚本、临时文件一律放在 D 盘。

| 类型 | 路径 | 说明 |
|------|------|------|
| 项目代码 | `d:\Jinta\Documents\Claude Code\school-watcher\` | 所有源码和数据 |
| 工具 | `D:\Jinta\Tools\` | ngrok 等第三方工具 |
| 桌面快捷方式 | `C:\Users\Jinta\Desktop\School Notifier.lnk` | 🆕 英文名（避免编码问题），指向项目 `school-notifier.bat`，自定义图标 |
| 调试脚本 | 项目内 `_debug_*.py`，用完即删 | 不允许在 C 盘临时目录遗留 |

违反此规则的典型操作：
- ~~`Write("C:\Users\Jinta\Desktop\test.py")`~~ → 应写 `d:\Jinta\...\school-watcher\_test.py`
- ~~`Write("C:\temp\script.py")`~~ → 应写项目目录内

---

## 1. 项目概述

构建一个**多学校官网通知聚合工具**，面向非技术用户。核心能力：
- 通用配置驱动，支持任意学校官网通知扒取（CSS 选择器配置化）
- 按「年月 → 部门 → 通知」三级归类展示，支持收展交互
- 接入 DeepSeek API 对每条通知生成 ≤100字 AI 摘要
- 响应式 Web UI，桌面端和手机端均可使用
- 本地运行，双击桌面快捷方式即可启动
- 支持 ngrok 隧道，手机外网访问

**技术栈**：Python 3 + Flask + SQLite + BeautifulSoup4 (lxml) + DeepSeek API (OpenAI 兼容 SDK) + Jinja2 + APScheduler

**项目路径**：`d:\Jinta\Documents\Claude Code\school-watcher\`

**桌面快捷方式**：`C:\Users\Jinta\Desktop\学校通知工具.bat`

---

## 2. 已完成

### 2.1 核心系统（全部可用且已验证）

| 模块 | 文件 | 状态 |
|------|------|------|
| Flask 主应用 | `app.py` | ✅ 路由、API、YAML 自动导入、调度器懒启动、部门树构建、DAILY NEWS、认证、安全头 |
| 数据库层 | `database/models.py`, `database/db.py` | ✅ 5 表 (School/Department/Announcement/ScrapeLog/AppConfig) |
| HTTP 爬取引擎 | `scraper/engine.py` | ✅ 翻页、增量抓取、链接重写、ZCMS catalog 端点、HTML 清洗 |
| 变更检测 | `scraper/change_detector.py` | ✅ 三重策略：内容哈希 + URL 指纹 + 发布日期比较 |
| HTML 安全清洗 | `scraper/sanitizer.py` | ✅ 移除危险标签/属性/协议（本轮新增） |
| AI 摘要 | `ai/summarizer.py` | ✅ DeepSeek API，OpenAI 兼容 SDK，批量+单条 |
| 定时调度 | `scheduler/jobs.py` | ✅ APScheduler，默认 30 分钟间隔，可配置 |
| Selenium 爬虫 | `scraper/selenium_scraper.py` | ✅ 已写好但**不再需要**（见 3.2 节） |
| 前端模板 | `templates/` (7 个页面) | ✅ 响应式，收展，年月/部门筛选，登录页 |
| 样式/交互 | `static/css/style.css`, `static/js/app.js` | ✅ 移动优先，Toast/Modal/折叠 |
| 配置 | `config.yaml` | ✅ 9 个一级部门，19 个数据库部门记录 |
| 启动脚本 | `setup.bat`, `run.bat` | ✅ 一键安装 + 启动 + 自动开浏览器 |
| 手机外网访问 | `D:\Jinta\Tools\ngrok\` | ✅ ngrok v3.39.9，配置/二进制均在 D 盘 |
| 管理界面-部门CRUD | `templates/schools.html` | ✅ 添加/编辑/删除部门，收展切换，无需刷新 |
| 安全机制 | `app.py`, `.gitignore`, `.env` | ✅ 密码认证、API Key脱敏、HTML清洗、安全头、错误去敏 |

### 2.2 爬取成果

**中国石油大学（北京）克拉玛依校区** (cupk.edu.cn) — **737 条通知，100% AI 摘要覆盖**

| 一级部门 | 数据库部门数 | 通知数 |
|----------|------------|--------|
| 校区通知公告 | 1 | 11 |
| 教务部 | 7 (含 6 子部门) | 112 |
| 学生工作与安全保卫部 | 1 | 46 |
| 研究生部 | 5 (含 4 子部门) | 106 |
| 创新创业学院 | 1 | 73 |
| 工商管理学院/马克思主义学院 | 1 | 50 |
| 石油学院 | 1 | 26 |
| 工学院 | 1 | 205 |
| 文理学院 | 1 | 108 |
| **合计** | **19** | **737** |

**中国石油大学（北京）** (cup.edu.cn) — **500+ 条通知，21 个部门（第八轮：翻页探测+链接修复+JS重定向+4个fallback学院修复）**

| 部门 | 通知数 | 选择器模板 |
|------|--------|-----------|
| 石油工程学院 | 20 | GP CMS sub_list |
| 化学工程与环境学院 | 20 | GP CMS block-list |
| 机械与储运工程学院 | 20 | GP CMS block-list |
| 安全与海洋工程学院 | 10 | GP CMS block-list |
| 地球科学学院 | 52 | GP CMS sub_list |
| 地球物理学院 | **80** | **GP CMS sub_list** ⭐第十轮探测 |
| 新能源与材料学院 | 20 | GP CMS sub_list |
| 人工智能学院 | 14 | GP CMS block-list |
| 理学院 | **120** | **GP CMS sub_list** ⭐第十轮探测 |
| 经济管理学院 | **120** | **GP CMS list01** ⭐第十轮探测 |
| 马克思主义学院 | 20 | GP CMS sub_list |
| 外国语学院 | **89** | **GP CMS list01** (tzgg3页面) ⭐第九轮修复 |
| 体育与人文艺术学院(教育学院) | 47 | GP CMS list01 |
| 未来能源学院 | 16 | GP CMS sub_list |
| 国际教育学院(塔尔萨) | 5 | GP CMS block-list |
| 油气资源与工程全国重点实验室 | 50 | GP CMS sub_list |
| 重质油全国重点实验室 | 25 | GP CMS block-list |
| 海南研究院 | 3 | GP CMS block-list |
| 科研项目 | **51** | **GP CMS CUP-list** ⭐第十一轮 — DDMM月日期格式 |
| 科研平台 | **3** | **GP CMS CUP-list01** ⭐第十一轮 — 静态目录页(非通知列表) |
| 重大创新项目 | 0 | GP CMS newslist (URL已修正，无近期通知) |
| **合计** | **700+** | |

**华中科技大学** (hust.edu.cn) — **173 条通知，15 个部门（第十八轮：方式C验证）**

| 部门 | 通知数 | 检测方式 |
|------|--------|-----------|
| 材料科学与工程学院 | 24 | DOM分析(90%) |
| 数学与统计学院 | 15 | DOM分析(90%) |
| 化学与化工学院 | 15 | DOM分析(90%) |
| 电气与电子工程学院 | 15 | DOM分析(90%) |
| 集成电路学院 | 15 | DOM分析(90%) |
| 建筑与城市规划学院 | 15 | DOM分析(90%) |
| 土木与水利工程学院 | 15 | DOM分析(90%) |
| 生命科学与技术学院 | 12 | DOM分析(90%) |
| 船舶与海洋工程学院 | 12 | DOM分析(90%) |
| 机械科学与工程学院 | 11 | DOM分析(89%) |
| 光学与电子信息学院 | 10 | DOM分析(78%) |
| 航空航天学院 | 8 | DOM分析(90%) |
| 中欧清洁与可再生能源学院 | 5 | DOM分析(90%) |
| 计算机科学与技术学院 | 1 | DOM分析(90%) |
| 工程科学学院 | 0 | DOM分析(90%) |
| **合计** | **173** | |

> **第十六轮备注（通用自助爬取框架）**⭐ 本次会话核心交付：
> 
> **问题**：之前每新增一个学校，需要开发者手动分析 CMS 结构、编写 CSS 选择器、调试翻页格式。用户无法自助完成。
> 
> **解决方案 — 通用 DOM 结构分析引擎**：从"选择器白名单"升级为"DOM 结构分析"——通过启发式算法自动识别任意网站的通知列表、正文区域、导航结构，不依赖硬编码的 CMS 模板。
> 
> **新增模块 (7个)**：
> - `scraper/dom_analyzer.py` — 通用 DOM 分析引擎（重复块检测、文本密度分析、导航识别、翻页检测、日期提取）
> - `scraper/list_detector.py` — 通知列表检测器（profile 优先 + DOM 分析回退）
> - `scraper/content_extractor.py` — 通用正文提取器（语义标签 → 文本密度 → body回退）
> - `scraper/nav_parser.py` — 通用导航解析器（链接密度 + 层次分类 + 部门/非部门区分）
> - `scraper/pagination_detector.py` — 翻页检测器（HTML预判 + HTTP探测 7种格式）
> - `scraper/discovery_progress.py` — 发现进度追踪（SSE 实时推送）
> - `scraper/selector_store.py` — 选择器学习存储（域名→选择器知识库，跨学校复用）
> 
> **新增 API**：`POST /api/schools/<id>/discover`（触发发现）、`GET /api/schools/<id>/discover/events`（SSE流）、`GET /api/schools/<id>/discover/status`（轮询回退）、`POST /api/schools/<id>/discover/apply`（应用结果）、`POST /api/departments/test-selectors`（测试选择器）
> 
> **前端重写** (`schools.html`)：发现向导（4步骤：基本信息→自动分析带进度条→结果预览→确认应用）、每个部门显示置信度+示例标题+选择器、全选/取消全选、勾选应用
> 
> **设计原则**：
> 1. 先精确后模糊：优先匹配已知 SELECTOR_PROFILES（12组保留），未命中→DOM 启发式分析
> 2. 学习存储：成功的选择器持久化到 `school.config` JSON，下次直接使用
> 3. 用户可控：所有自动检测结果先预览，用户确认后再写入
> 4. 向后兼容：现有两校数据完整性和 API 行为完全不受影响
> 
> **验证结果**：ZCMS 模板页纯 DOM 分析检测到 6 条 (conf=0.9)，GP CMS 模板页检测到 20 条 (conf=0.9)，与硬编码 profile 结果一致。
> 
> **附带修复**：`POST /api/departments` 补充 `group_name` 参数；`editDept()` 从 8 个位置参数改为 9 个（含 group_name）；部门管理页显示 `group_name`；新增「🧪 测试选择器」按钮
> 
> 新增踩坑 7.45~7.46。

> **第十七轮备注（curl_cffi TLS指纹反爬 + Scrapling自适应选择器自愈）**⭐ 本次会话核心交付：
> 
> **问题**：(1) 部分大学网站（PKU、清华等）有 Cloudflare/反爬保护，`requests` 库被拦截（PKU 仅返回 146 bytes）；(2) 网站改版后选择器全部失效，需要手动重新分析。
> 
> **解决方案 — 基础设施升级**：
> 
> **1. HTTP 层升级：requests → curl_cffi**
> - 所有 scraper 文件的 `import requests` 替换为 `from curl_cffi import requests`
> - 所有 `requests.get()` / `requests.head()` 加入 `impersonate='chrome'` 参数
> - TLS 指纹伪装为 Chrome 125，绕过 Cloudflare/bot 检测
> - `apparent_encoding` 不存在于 curl_cffi → 用 `charset_encoding` fallback（`try/except AttributeError`）
> - 改造文件：`engine.py`、`pagination_detector.py`、`nav_parser.py`、`selenium_scraper.py`
> - **效果**：PKU 首页从 146 bytes → 100,923 bytes；清华从被拦截 → 92,000 bytes；两校现有爬取零影响
> 
> **2. Scrapling 自适应选择器集成 (selector_store.py 增强)**
> - `save_element_signatures(html, school_id, url, selectors_dict)` — 用 Scrapling 保存元素 DOM 签名和文本指纹
>   - 签名保存到 `data/scrapling_sigs/school_{id}_sigs.db` (SQLite)
>   - 文本指纹冗余备份到 `school.config` JSON 字段
> - `auto_heal_selectors(html, school_id, url, department_name)` — 选择器失效时自动修复
>   - 策略1：从 Scrapling 签名 DB 恢复文本 → `find_by_text()` 在内容区域搜索
>   - 策略2：通过容器结构特征（多个 `.htm` 链接）定位
>   - 策略3：从 `school.config` 文本指纹备份搜索
>   - 验证：日期比例 ≥30% + 链接比例 ≥50%（复用 7.46 经验）
> - `generate_robust_selectors(html, url, candidate_items)` — 从 BS4 元素生成更稳健的 CSS 选择器
> - 集成到 `engine.py` `scrape_department()`：
>   - 选择器生效时 → 自动保存元素签名
>   - 选择器失效时 → 触发自愈流程，恢复成功则自动更新选择器
> 
> **环境变更**：Python 从 `D:\Jinta\AppData\Local\Programs\Python\Python314\` 迁移到 `D:\Programs\Python\Python314\`；PRIVACY_LOG.md 路径已更新
> 
> **验证结果**：
> - 两校现有爬取功能不受影响 ✅
> - PKU 首页可访问（100KB，之前被 Cloudflare 拦截）✅
> - 清华首页可访问（92KB）✅
> - 选择器签名保存/自愈/验证全流程通过 ✅
> - Flask 应用正常启动，30条路由、3校42部门2577条通知 ✅
> 
> 新增踩坑 7.47~7.49。

> **第十八轮备注（方式C实现 + 致命bug修复 + 华中科技大学接入）**⭐ 本次会话核心交付：
> 
> **问题（用户报告）**：网站上点击爬取始终显示「新增0条」，发现向导（🔍 自动发现）执行到一半就报错退出。
> 
> **根因1 — Flask进程旧代码**：运行中的Flask进程（PID 10980）是在代码修改前启动的，`scrape_school()` 中的 `is_bare` 自动站点发现逻辑未生效。杀掉重启后正常。
> 
> **根因2 — 新学校 list_url 为空**：PKU（学校3）的默认部门 `list_url = ''`，`scrape_department()` 第545行检查空URL直接 `return 0, 0`（10ms完成），根本不会发HTTP请求。修复方式：确保 `POST /api/schools` 创建默认部门时正确设置 `list_url = school.url`。
> 
> **根因3 — 致命bug：`sample_titles` vs `sample_items` 变量名拼写错误**：`app.py:1256` 的 `_run_discovery_in_background()` 中，变量定义为 `sample_items`（第1222行），但引用时写成了 `sample_titles`，导致 `NameError`。异常被外层 `try/except` 捕获后将该部门标记为"探测失败"跳过。**后果：发现向导的所有部门探测全部失败，永远返回0个候选部门。** 修复：`sample_titles[:3]` → `sample_items[:3]`。
> 
> **方式C验证 — 华中科技大学接入**：
> - 添加华科（hust.edu.cn，学校ID=4）→ 首页70867 bytes，可访问
> - 导航解析：30个分类，识别出「院系设置」「组织机构」「科研机构」3个汇总页
> - 院系设置页枚举：94个实体（去重后47个）
> - 前20个探测：15成功（75%），置信度78-90%
> - 写入15个部门 → 爬取173条通知
> - 发现向导（修复后）：从40个候选探测出31个部门（78%成功率）
> 
> **华科数据**：
> | 部门 | 通知数 |
> |------|--------|
> | 材料科学与工程学院 | 24 |
> | 数学与统计学院 | 15 |
> | 化学与化工学院 | 15 |
> | 电气与电子工程学院 | 15 |
> | 集成电路学院 | 15 |
> | 建筑与城市规划学院 | 15 |
> | 土木与水利工程学院 | 15 |
> | 生命科学与技术学院 | 12 |
> | 船舶与海洋工程学院 | 12 |
> | 机械科学与工程学院 | 11 |
> | 光学与电子信息学院 | 10 |
> | 航空航天学院 | 8 |
> | 中欧清洁与可再生能源学院 | 5 |
> | 计算机科学与技术学院 | 1 |
> | 工程科学学院 | 0 |
> | **合计** | **173** |
> 
> **全校汇总**：石大克拉玛依737 + 石大北京1840 + 华科173 = **2750条**
> 
> **ngrok 外网**：`https://reggae-enviably-renderer.ngrok-free.dev`（密码：166422）
> 
> 新增踩坑 7.50~7.53。
> 
> **⚠️ 关键提醒**：发现向导在浏览器中使用 SSE（Server-Sent Events），需确保 Flask 以最新代码运行。**每次改完 Python 代码后必须杀掉所有5000端口进程再重启！**（见踩坑 7.13）

> **第十九轮备注（发现向导后台线程修复 + 华科零通知部门批量修复）**⭐ 本次会话核心交付：
> 
> **问题（用户报告）**：华中科技大学使用「🔍 自动发现」后，进程到一半报错"Working outside of application context"。修复后发现向导虽然不报错了，但新发现的24个部门中有12个爬取返回0条通知。
> 
> **根因1 — 发现向导后台线程无Flask app context**：`_run_discovery_in_background()` 通过 `threading.Thread()` 在后台线程运行，内部调用链 `build_profile_from_knowledge()` → `get_global_knowledge()` → `School.query.all()`，需要 Flask app context。后台线程没有。修复：在 `_run_discovery_in_background()` 开头添加 `app.app_context().push()`。
> 
> **根因2 — 12个部门选择器/URL错误**：发现向导给新部门分配的选择器有大量问题：
> - **URL错误**（2个）：组织部、党校和机关党委被错误指向 `news.hust.edu.cn`（学校新闻主页），正确URL分别为 `zzb.hust.edu.cn/tzgg.htm` 和 `jgdw.hust.edu.cn/tzgg.htm`
> - **选择器错误**（5个）：人文社会科学处、档案馆、校医院等使用了通用 `div.main li:has(a)` 回退选择器，实际匹配到导航菜单而非通知列表
> - **日期格式不支持**（2个）：网络空间安全学院和未来技术学院使用 `YYYY-MM` 格式（如"2026-05"），`change_detector.py` 未支持
> - **正文选择器缺失**（1个）：工程科学学院详情页使用 `div.v_news_content`，不在默认选择器列表中
> - **无效URL**（1个）：附属协和医院（whuh.com）是医院门户网站，无通知列表，已删除
> - **URL + 选择器均需修复**（1个）：未来技术学院 `sft.hust.edu.cn/index/tzgg.htm`，选择器为 `div.list > ul > li`，日期 `div.date3 div`
> 
> **修复清单**：
> | 部门 | 问题 | 修复 |
> |------|------|------|
> | 组织部、党校 [65] | URL → news.hust.edu.cn | → `zzb.hust.edu.cn/tzgg.htm` |
> | 机关党委 [70] | URL → news.hust.edu.cn | → `jgdw.hust.edu.cn/tzgg.htm` |
> | 人文社会科学处 [81] | 通用选择器 | → `div.center > div.conright > div > ul.listul > li` |
> | 档案馆 [82] | 通用选择器 | → `div.inner > div.conright > div > ul.listul > li` |
> | 校医院 [83] | 通用选择器 | → `div.main-zyr > div.main-zyrx > div.lby > ul > li` |
> | 未来技术学院 [86] | 无list detected | → `div.list > ul > li` + `div.date3 div` |
> | 工程科学学院 [58] | 正文无法提取 | content_selector → 添加 `div.v_news_content` |
> | 附属协和医院 [85] | whuh.com无通知列表 | 已删除 |
> | 网络空间安全学院 [87] | YYYY-MM日期不识别 | `change_detector.py` 新增 `YYYY-MM` 格式 |
> 
> **新增日期格式**：`YYYY-MM`（正则 `(\d{4})-(\d{1,2})(?![\d-])`），用于处理仅有年月无日期的通知（默认日=1）。添加到 `DATE_PATTERNS` 列表中 `DDMM月` 之后。
> 
> **华科数据变化**：
> | 指标 | 修复前 | 修复后 |
> |------|--------|--------|
> | 零通知部门 | 12个 | 1个（工程科学学院，2021年后无新通知） |
> | 通知总数 | 612 | **913** |
> | 部门总数 | 40 | **38**（删1 + 删协和医院） |
> 
> **全校汇总**：石大克拉玛依737 + 石大北京1840 + 华科913 = **~3490条**（含PKU后更多）
> 
> 新增踩坑 7.54~7.57。
> 
> **⚠️ 关键提醒（延续）**：**任意Python代码修改 → 必杀进程重启Flask！** `taskkill -F -PID <PID>` 然后重新 `python app.py`。这条怎么强调都不为过。

> **第二十轮备注（Playwright Chromium 集成 — JS 渲染页面智能回退）**⭐ 本次会话核心交付：
> 
> **问题**：当前爬虫底层使用 `curl_cffi` 做 HTTP 请求，无法执行 JavaScript。对于 React/Vue 渲染的页面、AJAX 加载内容的页面、或有 Cloudflare 反爬保护的页面，`_fetch_html()` 只能拿到空壳 HTML，导致 0 条通知。
> 
> **解决方案 — 智能回退架构**：
> 
> **1. 新建 `scraper/playwright_fetcher.py`**：
> - `fetch_html_with_browser(url, timeout_ms, wait_until)` — 用 Chromium headless 获取 JS 渲染后的完整 HTML
> - `is_js_required(html)` — 启发式检测静态 HTML 是否需要 JS 渲染（5 个信号：SPA 骨架、body 空文本、AJAX 占位符、反爬质询、多 script 少内容）
> - 浏览器单例模式：懒加载启动，全应用复用，`atexit` 自动清理
> - 线程安全：`threading.Lock` 序列化浏览器访问
> - 优雅降级：Playwright 未安装时自动跳过，不影响现有功能
> 
> **2. 修改 `scraper/engine.py` — `_fetch_html()` 四阶段架构**：
> - Phase 1: curl_cffi 请求（不变）
> - Phase 2: curl_cffi 完全失败时 Playwright 接管
> - Phase 3: JS 重定向检测（不变）
> - Phase 4: 静态 HTML 疑似需 JS → Playwright 重试获取渲染后 HTML
> - 新增 `allow_browser_fallback` 参数（默认 True），调用方可禁用
> - 重定向链中的 fetch 不启用回退（`allow_browser_fallback=False`）
> 
> **3. `scrape_department()` 二次检测**：
> - Page 1 列表为空时，在 selector 探测之前先用 Playwright 重试
> - Playwright 渲染后内容非空 → 跳过选择器探测直接使用现有选择器
> - Playwright 也失败 → 进入原有的 selector 探测 + 自愈流程
> 
> **4. 重写 `scraper/selenium_scraper.py` — Selenium → Playwright**：
> - 移除 `selenium` 和 `webdriver_manager` 依赖
> - 公共接口 `scrape_department_selenium()` 保持不变
> - 内部全部替换为 Playwright API（`page.goto()`, `page.locator().click()` 等）
> - 详情页优先 curl_cffi（快），失败时 Playwright page 重试
> - 浏览器实例通过独立 context 管理（非单例，isolated session）
> 
> **5. 依赖更新**：
> - `requirements.txt`：新增 `playwright>=1.40.0`、补齐 `curl_cffi`、`scrapling`
> - `setup.bat`：增加 `pip install playwright` + `playwright install chromium` 步骤
> - Chromium 安装失败不阻断启动（WARNING 提示，应用可降级运行）
> 
> **6. `app.py` 启动检查**：启动时打印 Playwright 可用性状态
> 
> **设计原则**：
> 1. 零侵入：Playwright 未安装时，所有代码路径与原版完全一致
> 2. 智能降级：curl_cffi 优先 → 检测到 JS 需求 → Playwright 重试 → 失败则用原始 HTML
> 3. 性能优先：浏览器冷启动 ~2-3s，但只在必要时触发；99% 的页面仍走 curl_cffi
> 4. 线程安全：锁保护浏览器单例，每个请求独立 BrowserContext
> 
> **验证结果**：
> - `is_js_required()` 对正常页面返回 False，对 SPA 骨架/Cloudflare/空body 返回 True ✅
> - 未安装 Playwright 时模块正常导入，`is_playwright_available()` 返回 False ✅
> - `_fetch_html()` 新签名向后兼容，现有调用无需修改 ✅
> - engine.py, playwright_fetcher.py, selenium_scraper.py 语法检查通过 ✅
> 
> 新增踩坑 7.58~7.60。
> 
> **⚠️ Playwright 环境配置**：
> - 安装：`pip install playwright && python -m playwright install chromium`
> - Chromium binary 默认在 C 盘 `%USERPROFILE%\AppData\Local\ms-playwright\`
> - 如需移到 D 盘：设环境变量 `PLAYWRIGHT_BROWSERS_PATH=D:\Jinta\Tools\playwright-browsers`
> - 每次 `playwright` 包升级后需重装 Chromium：`python -m playwright install chromium`
> **第二十一轮备注（SSE 实时抓取进度 + 致命bug修复）**⭐ 本次会话核心交付：
>
> **问题（用户报告）**：网页点击「刷新」按钮后报"请求失败"，无法触发抓取。
>
> **根因1 — 缺少 `import threading`**：`app.py` 第 838 行 `threading.Thread()` 抛出 `NameError`。Round 21 将抓取改为后台线程 + SSE 进度推送，但忘了添加 `import threading`。**后果：所有学校的「刷新」按钮都返回 500。**
>
> **根因2 — `s.id` 属性不存在**：`/api/scrape/all` 路由中 `{s.id: s.session_id for s in sessions.values()}` 的 `s` 是 `ScrapeSession` 对象，属性名是 `school_id` 而非 `id`。导致「刷新全部」返回 500。
>
> **修复**：
> - 添加 `import threading, json` 到 `app.py` 顶部
> - `s.id` → 改为遍历 `sessions.items()` 用 `school_id` 作为 key
>
> 新增踩坑 7.61。

> **第二十二轮备注（上海交通大学接入 + URL自动填入 + 两个致命性能bug修复）**⭐ 本次会话核心交付：
>
> **1. 管理界面添加学校自动填入URL**：
> - 新建 `scraper/university_urls.py`：140+ 所中国高校全称 → 官网 URL 映射表，含 985/211/知名高校及 50+ 常用别名
> - 新增 `GET /api/schools/suggest-url?name=北京大学`：精确匹配 → 别名 → 子串模糊
> - 前端：输入校名 ≥2 字后 600ms 防抖自动查询，URL 字段为空或自动填入值时自动填充，边框闪绿提示
>
> **2. 上海交通大学 (sjtu.edu.cn) 接入中**：
> - 学校已添加到数据库（ID=5），发现向导找到了 28 个候选部门，当前进度约 50%+，4 个部门已确认
> - **注意**：SJTU 首页导航解析质量不高——大部分"候选"是新闻文章链接而非院系页面。需要在结果预览中筛选
>
> **3. 致命bug — `_fetch_html` JS重定向无限递归**：
> - **现象**：`news.sjtu.edu.cn/jdyw/index.html` 有一个分页 JS 函数 `location.href='index.html'`，`urljoin` 解析后指向自身
> - **根因**：`_fetch_html()` 的 Phase 3 中，`redirects_followed` 在每次递归调用时被重置为 0，导致同一 URL 被无限循环请求。每个循环 ~0.5s（HTTP 请求），直到 Python 递归栈溢出（~1000 层，约 8 分钟）
> - **修复**：新增 `_redirect_chain: set` 参数在递归调用间传递，检测到目标 URL 已访问过时立即 `break`
>
> **4. 致命bug — `find_dept_notice_url` 标准路径探测触发 Playwright**：
> - **现象**：发现向导卡在 `probing_selectors` 阶段的第一个候选部门上，8+ 分钟无进展
> - **根因**：`find_dept_notice_url()` 的策略2对 18 条标准路径逐一调用 `_fetch_html(test_url)`，**未传 `allow_browser_fallback=False`**。每个不存在的 URL 触发 curl_cffi 超时（15s）+ Playwright 回退（30s）= 45s。18 路径 × 45s = 13.5 分钟/候选，28 个候选总计 6+ 小时
> - **修复**：`_fetch_html(test_url, allow_browser_fallback=False)` —— 标准路径探测只需判断 URL 是否存在，不需要 JS 渲染
>
> 新增踩坑 7.62~7.64。



> **第十五轮备注（桌面图标重设计 + 快捷方式修复）**：(1) 重新设计桌面图标：SVG 矢量设计（蓝紫渐变圆底 + 白色铃铛），resvg CLI 渲染多分辨率 PNG，PIL 原生 ICO 打包。详见踩坑 7.43。(2) 桌面快捷方式从中文名 `.bat` 改为英文名 `School Notifier.lnk` → 指向项目目录 `school-notifier.bat`（避免 GBK/UTF-8 终端编码混乱导致文件名乱码）。详见踩坑 7.44。(3) 快捷方式创建方式：PowerShell 脚本文件（UTF-8-sig） → `subprocess.run`，不要通过管道传中文。新增踩坑 7.44（跨 shell 中文编码）。
>
> **第十四轮备注（增量抓取 + 正文不可用提示）**：(1) 实现真正增量抓取：新增 `Department.last_scraped_at` 字段，`scrape_department()` 遇连续3条已存在通知即停止翻页，完成后更新时间戳。效果：第二次全校抓取从25分钟→35秒（↓97%）。详见踩坑 7.42。(2) 正文不可用时不再显示"暂无正文内容"，改为🚫图标 + "服务器端限制，无法获取正文" + 完整原文链接，提醒用户自行前往查看。新增踩坑 7.42（增量抓取设计）。
>
> **第十三轮备注（CUP-list标题修复）**：用户反馈「1707月2026年度山东省科学技术奖申报项目公示5」标题异常。根因：`ul.CUP-list li` 的 HTML 结构中，`<a>` 标签内同时包含日期 `<span class="date">` 和标题 `<span class="artText">`，`title_selector: "a"` 提取了全部文本（含日期）。修复：(1) `engine.py` CUP-list profile 的 `title_selector` 从 `"a"` → `"span.artText"`，`date_selector` 从 `"span"` → `"span.date"`；(2) DB 中2个部门的 selector 同步更新；(3) 51条历史错误标题用正则 `^\d{2,4}月` 清理前缀。gongshi 目录 403 确认无法绕过（10条通知无正文，服务器限制）。新增踩坑 7.41。
>
> **第十二轮备注（续—正文修复）**：发现 cup.edu.cn 几乎所有通知正文为空+原文链接404。根因有两个：(1) 大量历史记录的 URL 被存到学校根路径 `https://www.cup.edu.cn/{hex}.htm` 而非 `{dept}/tzgg/{hex}.htm`，因为当时 list_url 指向错误位置；(2) 所有 GP CMS SELECTOR_PROFILES 的 content_selector 缺少 `article` 标签，但很多详情页使用 `<article>` 作为正文容器。修复：176个URL修正+469条正文重新抓取（526/737=71%覆盖），content_selector 全部加入 `article`。剩余211条主要是死链（历史URL错误）、403目录、外部链接。
>
> **第十一轮备注**：科研项目从通用选择器升级到 `ul.CUP-list li`（GP CMS CUP-list），90条→**51条高质量通知**，全部日期正确。新增 `DDMM月` 日期格式（change_detector.py），自动推断年份（未来日期→减1年）。科研平台确认为静态目录页，使用 `ul.CUP-list01 li`（3条平台链接）。翻页预判修复：跳过外部域名链接（防止友情链接中 index_N.htm 误匹配）。GP CMS 精确匹配：18/21 → **19/21**。警告：科研项目部分子目录（`notice/gongshi/`）返回 403，详情页无法获取。
>
> **第十轮备注**：三个选择器清空的部门全部自动探测成功：理学院→GP CMS sub_list (19→120条)、经济管理学院→GP CMS list01 (35→120条)、地球物理学院→GP CMS sub_list (20→80条)。翻页预判全部命中 index_htm_offset，零次404浪费。GP CMS 精确匹配：17/21 → **18/21**。回退选择器仅剩 3/21（科研项目81条、科研平台20条、国家重大项目0条）。
>
> **第九轮备注**：翻页预判优化（第1页HTML提取翻页格式，避免第2页404浪费）；新增 index_htm_offset 翻页格式（零偏移：第2页=index1.htm）；修复 IE 条件注释中 JS 重定向被误匹配（导致外国语学院 tzgg3 页面被错误跳转）；外国语学院发现 tzgg3 子页（`/dfl/tzgg3/index.htm`），GP CMS list01 精确匹配，23→**89条通知**；地球物理学院确认 xytzgg 页面使用 ul.sub_list li，选择器已清空待重新探测。GP CMS 精确匹配：16/21 → **17/21**。
>
> **第八轮备注**：修复了翻页探测（自动检测 ?page=N 等非标准格式）、相对链接解析（以list_url为基准）、JS重定向检测。新增 ul.list01 li 和 ul.newslist li SELECTOR_PROFILES。地球科学/理学院/经管学院现在使用 tzgg 子页面+GP CMS精确选择器。重大创新项目 URL 从错误的 /news/ 修正为 /gjzdxm/（页面仅有7条2011-2015静态项目链接）。外国语学院无 tzgg 子页，保持首页通用选择器。
>
> **第七轮备注**：添加了 4 种 GP CMS SELECTOR_PROFILES（sub_list、block-list），修复了 tzgg 查找逻辑（不再被首页新闻组件误导），新增 YYYY.MM.DD 日期格式，扩充了噪声过滤词库。学院从 4/15 → 15/15 全覆盖，通知从 311 → 500（+61%）。院系设置页 URL 从 `/yxsz/` 变为 `/yuanxi/`。

### 2.3 第二轮会话新增（2026-07-18 上午）

### 2.5 第四轮会话新增（2026-07-18 晚间）

#### 2.5.1 核心问题：新增学校爬取信息为 0

**问题**：通过 UI 新增学校时，只创建 School 记录，没有部门。爬取引擎遍历空部门列表，返回 0 条。

**解决方案（三层）**：

1. **自动创建默认部门** (`app.py` — `POST /api/schools`)
   - 新增学校时自动创建 "通知公告" 部门，list_url 指向学校首页
   - 选择器留空，触发首次抓取时的自动探测

2. **选择器自动探测** (`scraper/engine.py`)
   - 新增 6 组 `SELECTOR_PROFILES`：覆盖 ZCMS 首页通知(c-notice)、部门列表(middleNotice)、文章区块(middleArticle art)、文章列表(middleArticle list)、通用列表(ul.news-list)、通用链接(li > a)
   - `_probe_selectors()`: 依次尝试匹配 → 返回 (profile, is_permanent) 元组
     - ZCMS 精确匹配 → is_permanent=True → 永久保存到 DB
     - 回退全页链接 → is_permanent=False → 仅本次使用，下次重新探测
   - `_save_probed_selectors()`: 将探测到的选择器写入 department 记录
   - 探测仅在 page 1 执行
   - 空 URL 时提前返回 (0, 0) + warning 日志

3. **智能站点发现** (`scraper/site_discovery.py` — **新文件**)
   - 阶段 1：从首页导航区域提取所有候选部门链接
     - 使用 `_is_likely_section()` 做宽松匹配（不再依赖严格关键词）
     - 过滤非中文版本 (English/Russian)、特定文章页、文件下载
     - 支持 `.nav`, `.header`, `header`, `.navbar`, `.menu` 等容器
   - 阶段 2：逐部门访问，探测选择器 + 发现子部门
     - `_find_notice_list_url()`: 在部门页中搜索通知列表子页面
     - `_discover_sub_departments()`: 在侧边栏/子导航中发现更深层页面
     - 子部门自动使用 `父名-子名` 命名（匹配 sidebar 父子树 `-` 分隔符规范）
   - 阶段 3：`apply_discovered_departments()` 写入数据库
   - 限制参数：MAX_TOP_DEPTS=15, MAX_SUB_DEPTS=8, MAX_WORKERS=6

#### 2.5.2 安全改进（第三轮延续）

- **密码清除**：logout 后 JS 清除浏览器 autofill（`autocomplete="new-password"` + pageshow 事件）
- **改密需验旧密码**：settings 页 `current_password` 字段 → `POST /api/settings` 验证
- **密保问题恢复**：
  - `templates/recovery.html` 独立页面，两阶段：验证密保 → 重置密码
  - SHA256 哈希存储答案（含 salt）
  - 5 次失败 → 30 分钟锁定（`_check_recovery_rate_limit()`）
  - 恢复会话 10 分钟有效期

#### 2.5.3 一键已读改为按学校

- 首页每张学校卡片：未读数 > 0 时显示「✅ 一键已读」按钮
- 学校详情页标题栏：同样显示
- `POST /api/announcements/read-all` 接受 `{"school_id": N}` 参数

#### 2.5.4 部门侧边栏父子树（第三轮）
- **父子部门收展**：通过名称中 `-` 分隔符检测父子关系（如 "教务部-学籍管理" → 教务部的子部门）
  - `app.py` → `_build_dept_tree()` 函数构建树结构，DAILY NEWS 自动置顶
  - 父部门点击箭头展开/收起子部门列表
  - 父部门计数 = 自身 + 所有子部门通知合计
  - 筛选父部门时自动包含所有子部门通知
- **侧边栏滚动**：部门过多时 `max-height: calc(100vh - 96px)` + `overflow-y: auto`
- **统一个字重**：父部门/独立部门 `font-weight: 600`，子部门 `500`
- 侧边栏从 19 个平铺项缩减为 **9 个顶层项 + 子部门**

#### 2.5.5 第五轮会话新增（2026-07-18 深夜）⭐ 本次会话

**核心目标**：让"添加新学校→自动发现部门→爬取→侧边栏显示"完全自动化，无需手动调代码。

**站点发现噪声过滤优化**：
- `GENERIC_NAV_NAMES` 扩充至 60+ 条目（人才招聘、招标采购、医疗卫生、招生就业等）
- `INFORMATIONAL_PAGE_NAMES` 新增（简介、队伍、成果、委员、设备等非通知列表页关键词）
- `NOISE_TEXTS` 扩充（当前页、当前位置等面包屑导航）
- **关键修复**：改精确匹配为**子串匹配**（`any(kw in text for kw in SET)`）
  - HTML 文本"医疗卫生"≠过滤词"医疗保险"时，子串匹配"医疗"/"卫生"仍能命中
  - HTML 文本"本科生招生"≠过滤词"本科招生"时，子串匹配"招生"仍能命中
- `_is_specific_article` 新增检查 URL query string 中的长哈希 ID
- `_discover_sub_departments` 回退路径从全页 `a[href]` 扫描改为仅限导航容器
- URL 模式过滤新增 `/culture/`、子域名应用检测
- Phase 1 候选：22→13→9→**6**（滤掉"本科招生""医疗卫生""风华石大"等）

**后处理噪声清理** (`engine.py` → `_cleanup_noise_departments()`)：
- 子部门（名称含 `-`）+ 0条通知 → 自动删除
- 子部门 + 回退选择器(`a[href]`) + ≤2条通知 → 自动删除
- 顶级部门 + 0通知 + 回退选择器 → 自动删除
- cup.edu.cn 实测：13部门→**5个干净部门**，7个噪声子部门自动移除

**Phase 2 并行化** (`site_discovery.py`)：
- 提取 `_process_one_department()` 独立函数
- `ThreadPoolExecutor` + `as_completed`，max_workers=min(6, 候选数)
- 候选≤2时自动退化为顺序执行（省去线程开销）

**cup.edu.cn 集成测试结果**：
- 从 0部门/0通知 → **5部门/225通知**，全自动
- 教务管理(70) + 油气资源与工程全国重点实验室(26) + 油气资源与工程全国重点实验室(39) + 科研项目(81) + 重大创新项目(9)
- cupk.edu.cn 不受影响：20部门/737通知 ✓

#### 2.5.6 第六轮会话新增（2026-07-18 深夜）⭐ 站点发现架构重写

**核心问题**：第五轮的站点发现是扁平链接提取，完全缺失 cup.edu.cn 的 15 个学院（学院链接在「院系设置」二级表格页里，首页导航不直接可见）。

**解决方案 — `site_discovery.py` 架构重写**：

1. **导航层级解析** `_parse_nav_hierarchy()`
   - 解析首页 `ul#nav` 结构，识别顶级分类并标注类型
   - 返回类型化分类树：`submenu`（有子菜单）/ `single`（单页）/ `external`（外部链接）

2. **列表页枚举** `_enumerate_listing_page()`
   - 对「院系设置」类汇总页，访问页面解析 `<table>` 提取所有子实体
   - 支持 `rowspan` 合并单元格，只提取第一列（学院名），跳过系部列
   - 通过路径模式（`/jgsz/`、`/xygk/` 等）过滤子部门详情页

3. **分类器** `_classify_nav_for_discovery()`
   - 科研类→子项宽松接受；招生/就业/服务类→严格判断
   - 部门容器类（院系设置等）→进入列表页枚举

4. **智能 tzgg 查找改进** `_find_notice_list_url()`
   - 优先返回索引页（短路径）而非具体文章页
   - 新增标准路径 fallback：直接尝试 `/tzgg/`、`/notice/` 等

5. **崩溃修复** `engine.py`
   - `scrape_school()` 的 for 循环增加 per-department `try/except`，单部门失败不中断全校

**cup.edu.cn 实测对比**：

| 指标 | 第五轮(旧) | 第六轮(新) |
|------|-----------|-----------|
| 发现部门数 | 5 个扁平 | 28 个层级化 |
| 学院覆盖 | 0/15 | 15/15 ✅ |
| 科研实体 | 2 个(重复) | 6 个(去重) |
| 通知数 | 225 | 311 |
| MAX_TOP_DEPTS | 15 | 30 |

**第五轮→第六轮架构变化**：
```
第五轮: 首页 → _extract_nav_links() → 扁平链接 → Phase 2
第六轮: 首页 → _parse_nav_hierarchy() → 分类树
              ├── 院系设置 → _enumerate_listing_page() → 15学院
              ├── 科学研究 → 子菜单 7项 → 6部门(去重)
              ├── 招生就业 → 过滤(非部门)
              └── 公共服务 → 过滤(非部门)
          → 合并去重 → Phase 2 并行探测
```

**仍存在的问题（第七轮待解决）**：
1. 6 个学院使用非标准 HTML 模板，SELECTOR_PROFILES 不匹配，0 通知
2. 油气资源实验室 URL 被错误指向 `/news/`
3. 重质油实验室导航子项被误识别为子部门
4. 石油工程学院等虽 tzgg URL 正确但选择器探测失败，回退到 `a[href]`

#### 2.5.7 第七轮会话新增（2026-07-19）⭐ GP CMS 模板覆盖 + tzgg 修复

**核心问题**：第六轮虽然覆盖了 15/15 学院，但只有 4 个学院成功爬取（311条）。其余 11 个学院因非标准 HTML 模板、tzgg URL 查找失败、选择器不匹配等原因返回 0 条。

**解决方案**：

1. **新增 3 种 GP CMS SELECTOR_PROFILES** (`engine.py`)：
   - `GP CMS sub_list`：`ul.sub_list li` + `span.rightDate, span.leftDate`（石油工程、地球物理、新能源、马克思主义、未来能源）
   - `GP CMS block-list`：`ul.block-list li` + `span.gpArticleDate, div.gpArticleDate, div.time`（化工、机械、安全、国教、海南研究院、重质油）
   - `GP CMS NewsConList`：首页新闻摘要组件（仅用于探测，不匹配时回退到 tzgg 子页）
   - 合并了 rightDate/leftDate 和 span/div 日期选择器到一个 profile 中（相同 list_selector）

2. **新增日期格式** (`change_detector.py`)：
   - `YYYY.MM.DD`（如 "2026.03.13"）— 安全与海洋工程学院使用的格式

3. **修复 `_find_notice_list_url`** (`site_discovery.py`)：
   - **问题**：首页新闻摘要组件（NewsConList 等）有 ≥5 项时，函数立即返回首页 URL，不再查找 tzgg 子页
   - **修复**：标记首页摘要型 profiles（NewsConList/EventsList），只有 ≥12 项且非摘要型才直接使用首页；否则持续查找 tzgg 子页
   - **效果**：石油工程学院 `/oil/` → `/oil/tzgg/index.htm`，未来能源学院 `/cei/` → `/cei/tzgg/index.htm`

4. **扩充噪声过滤词库** (`site_discovery.py` — INFORMATIONAL_PAGE_NAMES)：
   - 新增 15+ 条：学术交流、人才培养、社会服务、开放课题、新闻动态、实验室新闻、安全管理、仪器平台、规章制度、访问学者、学术会议 等
   - 有效过滤重质油实验室的导航子项（学术交流、社会服务、新闻动态）

5. **修复 `_probe_selectors` 返回值 bug** (`engine.py`)：
   - `return None, False` 是无匹配时的返回值，但作为非空元组 `(None, False)` 是 truthy 的
   - `scrape_department` 中 `if probed:` 通过，但 `probed_profile = None`，导致 `TypeError`
   - 修复为 `return None`

6. **发现并修复的错误**：
   - 院系设置页 URL 变更：`/yxsz/index.htm` → `/yuanxi/index.htm`（导航解析自动适配）
   - 新能源与材料学院 URL：`xny`（404）→ `cnem`（正确，从院系设置页提取）
   - 经济管理学院 URL：`sem`（404）→ `sba`（正确）
   - 人工智能学院 URL：`cise`（JS 重定向到 `cupai`）— 手动添加为 `cupai/tzgg/index.htm`
   - 油气资源实验室 list_url：`/news/index.htm` → `/prplab/notice/index.htm`

**cup.edu.cn 实测对比**：

| 指标 | 第六轮(旧) | 第七轮(新) |
|------|-----------|-----------|
| 覆盖学院 | 4/15 | 15/15 ✅ |
| 部门总数 | 5 | 21 |
| 通知总数 | 311 | 500 |
| GP CMS 精确匹配 | 0 | 12/21 |
| 选择器回退 | 全部 a[href] | 9/21 |
| 日期格式覆盖 | 4种 | 5种(+YYYY.MM.DD) |

#### 2.5.8 第八轮会话新增（2026-07-19）⭐ 翻页探测 + 链接修复 + JS重定向 + Fallback学院修复

**核心问题**：第七轮虽然覆盖了 15/15 学院，但仍有 4 个学院使用回退选择器、部分子站点翻页 404、相对链接解析错误、JS 重定向无法追踪。

**解决方案**：

1. **`_find_notice_list_url` 修复** (`site_discovery.py`)：
   - **问题**：通用选择器（`div.main li:has(a)`）匹配首页 ≥12 项时，函数立即返回首页 URL，不再检查 tzgg 子页
   - **修复**：添加 `GENERIC_PROFILE_NAMES` 集合标记通用选择器。首页匹配通用选择器时不立即返回，继续检查 tzgg 标准路径
   - **决策逻辑**：tzgg 匹配到专用选择器（GP CMS sub_list/block-list）且首页匹配通用选择器 → 优先 tzgg（即使条数更少）
   - **效果**：地球科学学院、理学院、经济管理学院的 tzgg 子页被正确定位

2. **非标准翻页探测** (`engine.py`)：
   - **问题**：多个子站点（geosci、tiyubu、cupai、prplab）第 2 页 `/index_2.shtml` 返回 404
   - **修复**：新增 `_detect_pagination_style()` 函数，第 2 页 404 时依次尝试 3 种替代格式：
     - `/index_N.htm`
     - `/index.htm?page=N`
     - `?page=N`
   - **效果**：geosci（51 页！）、tiyubu 等成功翻页

3. **相对链接解析修复** (`engine.py`)：
   - **问题**：`_process_announcement_item` 以 `school_base_url` 为基准解析相对链接，子站点详情链接被解析为根路径
   - **修复**：改为以 `department.list_url` 为基准解析，确保 `/cupai/tzgg/xxx.htm` 正确解析

4. **JS 重定向检测** (`engine.py`)：
   - **问题**：`requests` 无法执行 JS，`<script>window.location.href='...'</script>` 页面被当作空页
   - **修复**：`_fetch_html()` 新增 JS 重定向检测，自动提取并跟踪（最多 3 跳），同时支持 `<meta http-equiv="refresh">`
   - **效果**：cise→cupai 等 JS 重定向页面可自动追踪

5. **新增 SELECTOR_PROFILES** (`engine.py`)：
   - `GP CMS 列表 (list01)`：`ul.list01 li` + `span` 日期 — 经济管理学院 tzgg、体育学院 news
   - `GP CMS 新闻列表 (newslist)`：`ul.newslist li` — 重大创新项目

6. **数据库修正**：
   - 地球科学学院：list_url `/geosci` → `/geosci/tzgg/index.htm`，选择器已清空重新探测 → GP CMS sub_list
   - 理学院：list_url `/science/` → `/science/tzgg/index.htm`，选择器已清空 → GP CMS sub_list
   - 经济管理学院：list_url `/sba/` → `/sba/tzgg/index.htm`，选择器已清空 → GP CMS list01
   - 重大创新项目：list_url `/news`（学校新闻页）→ `/gjzdxm/`（正确URL）
   - 体育学院：选择器已清空 → GP CMS list01

**cup.edu.cn 实测验证**：
- 地球科学学院：探测 `ul.sub_list li` ✓，翻页自动切换 `?page=2` ✓，51 页 1000 条
- 体育学院：探测 `ul.list01 li` ✓，翻页自动切换 `?page=2` ✓
- 新模板 list01 覆盖 2 个部门（经济管理 + 体育）

**仍存在的问题（第九轮待解决）**：
1. 外国语学院无 tzgg 子页，保持首页通用选择器（23条）
2. 重大创新项目 0 条通知（页面仅有 7 条 2011-2015 静态项目链接，无近期通知）
3. 科研项目、科研平台仍使用回退选择器（但通知数较多：81+20条）
4. 站点发现对 cup.edu.cn 的 `_find_notice_list_url` 修复后需要完整重新发现测试
5. 翻页持续探测会增加网络开销（每个部门第 2 页失败后才探测，浪费一次 HTTP 请求）

##### 2.5.9 第九轮会话新增（2026-07-19）⭐ 翻页预判优化 + IE条件注释修复 + 外国语学院修复

**核心目标**：优化翻页探测（避免浪费HTTP请求），修复外国语学院和其它剩余问题。

**解决方案**：

1. **翻页预判优化** (`engine.py`)：
   - **问题**：翻页格式探测发生在第2页404之后，浪费一次HTTP请求
   - **修复**：新增 `_extract_pagination_style_from_html()` 函数，第1页解析时从HTML链接中提取翻页格式
   - **检测逻辑**：扫描翻页区域（`.pages`、`.pagination` 等class）中的 `<a>` 链接，分析其 href 模式
   - **支持格式**：`index_N.htm`、`indexN.htm`（零偏移）、`?page=N`、`index.htm?page=N`、`index_N.shtml`
   - **效果**：geosci/tiyubu 页面正确预判 `index_htm_offset`，跳过第2页404探测

2. **新增 index_htm_offset 翻页格式** (`engine.py`)：
   - **发现**：cup.edu.cn 多个子站点使用零偏移翻页 — 第1页=`index.htm`，第2页=`index1.htm`，第N页=`index{N-1}.htm`
   - **修复**：`_get_page_url()` 新增 `index_htm_offset` style；`PAGINATION_FALLBACKS` 新增对应格式
   - **效果**：geosci、tiyubu、dfl（外国语）等站点正确翻页

3. **IE 条件注释 JS 重定向误匹配** (`engine.py`)：
   - **问题**：`_fetch_html()` 的 JS 重定向检测将 `<!--[if lt IE 9]><script>window.location.href='brower.html'</script><![endif]-->` 误判为真实重定向
   - **后果**：外国语学院 tzgg3 页面被错误跳转到 `brower.html`，爬取失败
   - **修复**：JS 重定向检测前先用正则 `<!--\[if\s[^\]]*\]>.*?<!\[endif\]-->` 移除 IE 条件注释
   - **效果**：外国语学院 tzgg3 页面正常爬取

4. **外国语学院修复** (`site_discovery.py` + 数据库)：
   - **发现**：学院首页有两个通知区块：`xwdt/`（新闻动态）和 `tzgg3/`（通知公告）
   - **修复**：list_url → `/dfl/tzgg3/index.htm`，选择器清空 → 自动探测 → GP CMS list01（`ul.list01 li`）
   - **效果**：23条 → **89条通知**（66条新增），翻页预判 `index_htm_offset` 正常工作
   - **site_discovery 增强**：`standard_paths` 新增 `/tzgg2/index.htm`、`/tzgg3/index.htm`、`/news/index.htm`

5. **地球物理学院修复**：
   - **确认**：`/geophysics/xytzgg/index.htm` 页面使用 `ul.sub_list li`（20项）
   - **修复**：选择器已清空，下次爬取自动探测 → GP CMS sub_list

**cup.edu.cn 状态变化**：
| 指标 | 第八轮(旧) | 第九轮(新) |
|------|-----------|-----------|
| GP CMS 精确匹配 | 16/21 | **17/21** (+1 外国语) |
| 回退选择器 | 5/21 | **3/21** (外国语升级, 地球物理待探测) |
| 外国语学院通知 | 23 | **89** (+66) |
| 翻页预判 | 无 | HTML提取，跳过404探测 |
| IE条件注释误匹配 | 存在 | 已修复 |

### 2.3.2 DAILY NEWS 每日速览
- 在管理页添加名为 `DAILY NEWS` 的部门即可启用（名称大小写不敏感）
- 侧边栏**金色置顶**，显示近 7 天通知数（呼吸动画徽章）
- 点击后进入特殊模式：跨所有部门聚合近 7 天通知，按 今天（24h内）/ 昨天 / 前天 / 具体日期 归类
- 标题栏切换为 "📰 DAILY NEWS"，不显示年份筛选

#### 2.3.3 Bug 修复（第二轮）
- **通知详情页返回首页 404**：`<base>` 标签劫持导航栏 → 已删除
- **管理界面「部门」按钮无响应**：新增 `GET /api/schools/<id>/departments` API
- **部门管理点击后无法收起**：添加 toggle 逻辑
- **DAILY NEWS 显示空白**：`datetime.now(timezone.utc)` (aware) vs naive datetime → 全部改用 `datetime.utcnow()`
- **ngrok 安装到 D 盘**：`--config` 参数指定路径，NGROK_CONFIG_DIR 环境变量不生效

### 2.4 第三轮会话新增（2026-07-18 下午）⭐ 本次会话

#### 2.4.1 首页「一键已读」功能
- 首页标题栏新增 **「✅ 一键已读」** 按钮（仅在 `total_unread > 0` 时显示）
- 后端：`POST /api/announcements/read-all` — 批量 `UPDATE` 所有未读通知为已读
- 前端：确认框 → spinner → API 调用 → Toast 提示 → 未读数归零 → 蓝色圆点消失 → 按钮隐藏
- 支持可选 `{"school_id": N}` 参数按学校已读

#### 2.4.2 安全加固（4 个阶段）

**阶段 1 — 认证体系**
- **密码登录**：新建 `templates/login.html`，会话有效期 30 天
- `@app.before_request` 认证钩子：白名单 `/login`、`/logout`、`/static/*`；页面路由→302 重定向；`/api/*`→401 JSON
- 密码优先级：`APP_PASSWORD` 环境变量 > `app_config` 表 `app_password` > 首次启动自动生成
- 导航栏添加退出登录图标

**阶段 2 — API Key 保护**
- 设置页 `value` 改为脱敏显示：`sk-xx****xxxx`（前6+后4），完整 key 不再暴露在 HTML 源码
- 前端+后端双重校验 API Key 必须以 `sk-` 开头
- 保存后自动刷新页面更新脱敏值

**阶段 3 — 内容安全 + 安全头**
- 新建 `scraper/sanitizer.py`：基于 BeautifulSoup 的 HTML 清洗
  - 移除危险标签：`script, iframe, object, embed, form, input, link, meta, base`
  - 移除所有 `on*` 事件属性
  - 清洗 `javascript:` / `vbscript:` 协议
  - 清洗 style 中的 CSS 表达式
- `scraper/engine.py` 存储前自动调用 sanitizer
- `@app.after_request` 添加安全头：CSP、X-Frame-Options、X-Content-Type-Options、Referrer-Policy

**阶段 4 — 错误信息去敏**
- 4 处 API 异常响应中的 `str(e)` 替换为中文通用提示
- 保留 `logger.error()` 用于调试

#### 2.4.3 设置页密码修改 UI
- 设置页新增 **「🔒 访问密码」** 卡片
- 输入新密码（≥4字符）→ 保存 → 立即生效
- 眼睛图标切换明文/密文

#### 2.4.4 基础安全设施
- **`.gitignore`**：排除 `data/`、`*.db`、`.env`、`__pycache__/`、`PRIVACY_LOG.md`
- **SECRET_KEY 自动生成**：`secrets.token_hex(32)` → 持久化到 `.env`，不再使用硬编码
- **`load_dotenv()`**：`.env` 文件自动加载，无需手动设置环境变量

---

## 3. 已解决的关键问题

### 3.1 ✅ AJAX 四个部门 — 已解决（重大突破）

原 HANDOFF 标记 xgybw/yjsb/cxcy/smxy 四个归档页需要 Selenium。

**根因**：这些页面加载骨架 HTML，然后 `msgList.js` 通过 AJAX 拉数据填充。

**解决方案**：发现 ZCMS 的 catalog 端点直接返回**服务端渲染的完整 HTML**：
```
https://www.cupk.edu.cn/zcms/catalog/{ID}/pc/index_N.shtml
```
这个端点不需要 JS 执行，BeautifulSoup 直接解析即可。**彻底消除了对 Selenium 的依赖**。

**已知 Catalog ID**：

| 部门 | Catalog ID | 模板类型 |
|------|-----------|---------|
| 学生工作与安全保卫部 (xgybw) | 15887 | A |
| 研究生部 (yjsb) | 18144 | B |
| 创新创业学院 (cxcy) | 17669 | C |
| 工商管理学院/马克思主义学院 (smxy) | 17291 | A |
| 石油学院 | 15315 | C |
| 工学院 | 15250 | A |
| 文理学院 | 15413 | A |

### 3.2 ✅ 校区通知公告 (Dept 1) 返回 0 条 — 已解决

**根因**：选择器配错了。原 `title_selector: "div.c-campus-news__detail-box-title"` 不是 `list_selector` 的直接子元素。

**修复**：
```yaml
list_selector: "li.c-notice__half-li"
title_selector: "a"
date_selector: ".c-notice__left-date"
```

### 3.3 ✅ 教务部-学籍管理 / 实践教学 为空 — 确认为数据缺失

这两个子部门 2024 年后确实没有任何新通知。用 `since_year=2020` 测试可正常抓取。

### 3.4 ✅ 安全：公网开放无认证 — 已解决（本轮）

ngrok 将应用暴露到公网，之前任何人可访问所有数据和 API。现已添加密码认证、API Key 脱敏、HTML 清洗、安全头。

---

## 4. ZCMS 模板分类（重要！添加新部门必读）

同一个学校不同 ZCMS catalog 使用**不同的 HTML 模板**，选择器完全不同。

### 模板 A：`xgybw/smxy` 型（学生工作、工商管理、工学院、文理学院）
```yaml
list_selector: "div.middleArticle__art--art"
title_selector: "h3.middleArticle__art--titles"
link_selector: "a.middleArticle__art--link"
date_selector: "div.middleArticle__dates"
```
日期格式：`YYYY-MM-DD`

### 模板 B：`yjsb` 型（研究生部）
```yaml
list_selector: "li.middleNotice__notice--list"
title_selector: "a"
link_selector: "a"
date_selector: "span.middleNotice--dates"
```
日期格式：`MM-DDYYYY`（如 "07-042026"）

### 模板 C：`cxcy` 型（创新创业、石油学院）
```yaml
list_selector: "li.middleArticle--articleList"
title_selector: "a.middleArticle__articleList--article"
link_selector: "a.middleArticle__articleList--article"
date_selector: "span.middleArticle__articleList--date"
```
日期格式：`YYYYMM-DD`（如 "202607-08"）

### 非 catalog 模板 D：教务部标准页面
```yaml
list_selector: "li.middleNotice__notice--list"
title_selector: "a"
link_selector: "a"
date_selector: "span.middleNotice--dates"
```
日期格式：`YYYY-MM-DD`

**添加新部门前必须先 `curl` 页面确认模板类型和选择器，不要假设！**

---

## 5. 当前状态与已知限制

### 5.1 正常运行的功能
- 🔐 **密码认证**：所有页面和 API 均受保护，未登录自动跳转
- 手动爬取：`POST /api/scrape/1` → 自动生成摘要
- 定时爬取：默认每 30 分钟，可在设置页修改
- 摘要生成：爬取后自动触发，也可手动 `POST /api/summarize`
- Web UI：浏览/筛选/展开/收起/详情/设置 均正常
- 管理界面：学校/部门 CRUD，部门收展切换
- DAILY NEWS：近 7 天全校通知速览，按 今天/昨天/前天 滚动归类
- 一键已读：首页一键标记所有未读为已读
- 手机访问：ngrok 隧道 `tunnel` 一键启动（现在需要先登录）
- 密码修改：设置页直接修改访问密码

### 5.2 已知限制

1. **ZCMS 归档深度有限**：catalog 端点通常只暴露 30-50 页。
2. **Flask 单线程**：爬取期间所有其他请求排队。定时爬取建议设置在凌晨。
3. **摘要生成同步阻塞**：`batch_summarize()` 逐条调用 API，每条间隔 0.5s。
4. **YAML 导入只增不更新**：修改 config.yaml 中已有部门的配置**不会生效**，需通过 API `PUT /api/departments/<id>` 更新。
5. **石油学院数据最少**（26 条）：该学院 2024 年后发布的通知确实很少，非 bug。
6. **暑假通知稀少**：7 月中下旬学校放假，DAILY NEWS 看起来较空。
7. **认证是单密码模式**：所有用户共用同一个密码，不支持多用户。
8. **现有旧 HTML 数据未重新清洗**：sanitizer 只对新爬取的内容生效。
9. **站点发现对非 ZCMS 站点的部门识别不完整**：SJTU 实测导航解析质量差，候选多为新闻链接。
10. **「🧪 测试选择器」已可用**：在部门编辑页面可以通过此按钮实时验证CSS选择器是否匹配到条目。
11. **SJTU 发现成功率低**：28 候选中发现 4 个部门，多数是噪声。需要手动筛选或直接访问 `/xyyl/` 学院页。
12. **Chromium 残留进程**：Playwright 异常退出后可能残留 `chrome-headless-shell.exe`，需手动 `taskkill`。
13. **HUST 主页 Playwright 超时**：`www.hust.edu.cn` 使用 `networkidle` 等待，但主页有持续连接（WebSocket/analytics），导致 30s 超时。目前通过 curl_cffi 仍可正常获取，Playwright 回退是冗余路径。
14. **Flask 后台线程必须重新获取 DB 对象**：任何 `threading.Thread` 内使用的 ORM 对象必须在自己的 app context 中重新获取（见 7.66）。

## 6. 下一步计划

### 6.1 ⭐ 当前状态 (2026-07-30 第二十三轮)

**第二十三轮已完成**（本次会话）：
- ✅ 致命bug修复：`engine.py` 4处 `url` → `page_url` 变量名错误
- ✅ 致命bug修复：`app.py` 后台线程 detached session 导致 UI 抓取全部失效
- ✅ 全量抓取验证：三校共 3735 条通知，数据更新到 7月28-29日
- ✅ UI 端「刷新」按钮恢复正常工作
- ✅ 新增踩坑 7.65~7.66

**第二十二轮已完成**（上一会话）：
- ✅ 致命bug修复：`_fetch_html` JS重定向无限递归（`_redirect_chain` 跟踪）
- ✅ 致命bug修复：`find_dept_notice_url` 标准路径探测未禁用 Playwright 回退
- ✅ 致命bug修复：`app.py` 缺少 `import threading`、`/api/scrape/all` 的 `s.id` bug
- ✅ 管理界面添加学校自动填入 URL（`scraper/university_urls.py` + API + 前端）
- ✅ 上海交通大学（ID=5）发现向导正在运行（28候选，4+部门已确认）
- ✅ 新增踩坑 7.61~7.64

> **第二十三轮备注（两个致命bug修复 — UI端「刷新」按钮完全失效）**⭐ 本次会话核心交付：
>
> **问题（用户报告）**：官网已更新到7月28日，但网页点击「刷新」始终抓不到新通知。所有学校的 ScrapeLog 显示 `total=0, new=0`。
>
> **根因1 — `url` 变量名错误 → `page_url`**：`scraper/engine.py` 第 714/722/752/785 行中，`save_element_signatures()` 和 `auto_heal_selectors()` 调用使用了变量 `url`，但实际变量名是 `page_url`（第628行定义）。导致 `NameError`，选择器自愈和签名保存失败。对于部分部门（如重质油全国重点实验室），该异常未被妥善捕获，导致整个部门爬取崩溃。
>
> **根因2 — 后台线程 School detached session（核心问题）**：`app.py` 的 `POST /api/scrape/<id>` 和 `POST /api/scrape/all` 路由中，`school` 对象在请求线程获取后传入后台线程 `_run()`。后台线程 `app.app_context().push()` 创建新 session，原有 `school` 对象 detached。`school.departments.all()` 返回空列表 → `is_bare = True` → 触发站点发现 → 创建垃圾部门 → `_cleanup_noise_departments()` 清掉 → `total_departments=0` → 返回 `(0, 0)`。
>
> **这就是用户每次点「刷新」都抓不到通知的根本原因。**
>
> **修复**：
> - 全部 4 处 `url` → `page_url`（engine.py）
> - `_run()` 内用 `db.session.get(School, school_id)` 重新获取 school 对象（app.py 两个路由）
>
> **验证结果**：
> - 命令行直接调用 `scrape_school()`：CUPK 181条、CUP 374条、HUST 35768条 ✅
> - API 端点 `POST /api/scrape/1`：正确识别 21 个部门，SSE 进度正常推送 ✅
> - 数据更新：CUPK 737→743、CUP 1840→1857、HUST 913→1135
>
> **全校汇总**：石大克拉玛依743 + 石大北京1857 + 华科1135 = **3735条**
>
> 新增踩坑 7.65~7.66。

**第二十一轮已完成**（上一会话）：
- ✅ Playwright Chromium 集成：`scraper/playwright_fetcher.py` + 智能回退
- ✅ SSE 实时抓取进度：`scraper/scrape_progress.py` + 前端 EventSource
- ✅ Chromium 移至 D 盘：`D:\Jinta\Tools\playwright-browsers\`

**数据现状**：
| 学校 | 通知数 | 最新日期 |
|------|--------|----------|
| 石大克拉玛依 (CUPK) | 743 | 2026-07-28 |
| 石大北京 (CUP) | 1857 | 2026-07-29 |
| 华中科技大学 (HUST) | 1135 | 2026-07-29 |
| 上海交通大学 (SJTU) | 0 | 待完成发现 |
| **合计** | **3735** | |

**仍然存在的问题**：
1. **SJTU 发现向导噪声多**：28 候选 → 约 4 个真院系。导航解析对 SJTU 首页结构的适配性差
2. **发现向导的选择器质量**：通用 `div.main li:has(a)` 类型的置信度虚高，需要人工验证（7.55）
3. **SJTU 真实院系列表在 `/xyyl/`**：发现向导未直接访问此页面
4. **Chromium 残留进程**：Playwright 异常时可能残留 `chrome-headless-shell.exe`
5. **HUST 部分子域名 DNS 解析失败**：`gyss.hust.edu.cn`、`ceee.hust.edu.cn` 等子域名可能已变更
6. **HUST 主页 Playwright 超时**：`www.hust.edu.cn` 的 `networkidle` 永远不触发（30s浪费）

**下一步**：
1. **完成 SJTU 发现**：等当前发现完成 → 筛选结果 → 确认应用 → 爬取通知
2. **改进 SJTU 导航解析**：直接访问 `/xyyl/` 学院一览页枚举院系
3. **修复 HUST 失效子域名**：部分部门 URL 可能已变更（返回 404 或 DNS 失败）
4. **添加更多学校**（PKU、清华等 — 现在有 curl_cffi + Playwright 双引擎）
5. 改进发现向导的 profile 验证：自动识别通用选择器并标记为"需人工确认"
6. **将 `div.v_news_content` 加入全局默认 content_selector 列表**

### 6.2 短期

1. **添加更多学校测试**：不同 CMS/结构的学校，完善过滤词库
2. **站点发现进度反馈**：长耗时操作给前端进度条/WebSocket 推送
3. **爬取性能优化**：`scrape_school` 内各department也可并行

### 6.3 中期

4. **通知推送**：邮件/微信/ServerChan 等渠道推送新通知
5. **Docker 部署**：容器化后可部署到云服务器

### 6.4 长期

6. **关键词订阅**：用户设置关键词，匹配到新通知时重点提示
7. **多校对比视图**：跨学校查看同类通知

---

## 7. 绝对不能踩的坑

### 7.0 ⛔ 非必要不往 C 盘塞东西

**这是项目最高优先级硬性规则。** 

所有文件（代码、脚本、数据、工具、临时文件）一律放在 D 盘。唯一允许的 C 盘文件是桌面快捷方式（`C:\Users\Jinta\Desktop\学校通知工具.bat`），因为 Windows 桌面必须位于 C 盘。

**正确路径**：
- 项目文件 → `d:\Jinta\Documents\Claude Code\school-watcher\`
- 工具 → `D:\Jinta\Tools\`
- 调试脚本 → 项目内 `_debug_*.py`，用完即删

**禁止**：在 `C:\Users\Jinta\Desktop\`、`C:\temp\`、`C:\Users\Jinta\AppData\` 写入任何项目相关文件。

### 7.1 ⚠️ ZCMS catalog 端点的翻页格式与普通页面不同
- **普通页面**（如教务部）：第 1 页 = `base_url`，第 2+ 页 = `base_url/index_N.shtml`
- **Catalog 端点**（`/zcms/catalog/`）：**所有页面**都是 `index_N.shtml`，包括第 1 页
- `engine.py` 的 `_get_page_url()` 已处理（检测 URL 中是否包含 `/zcms/catalog/`）

### 7.2 ⚠️ 不同 ZCMS catalog 使用不同 HTML 模板
详见第 4 节。判断方法：
```bash
curl -s "https://www.cupk.edu.cn/zcms/catalog/{ID}/pc/index_1.shtml" | head -200
```
看 list_selector 的类名：`middleArticle__art--art`（模板A）、`middleNotice__notice--list`（模板B）、`middleArticle--articleList`（模板C）。

### 7.3 ⚠️ 日期格式不统一
4 种日期格式已全部覆盖：`YYYY-MM-DD`、`YYYY年MM月DD日`、`MM-DDYYYY`、`YYYYMM-DD`。添加新部门后如果日期解析为 None，检查日期格式。

### 7.4 ⚠️ 不要删除 data/school_watcher.db
- 数据库删除后 **DeepSeek API Key 和访问密码都会丢失**（都在 `app_config` 表里）
- 如果必须重建，重启后立即访问 `/settings` 重新保存 API Key 和密码
- 建议定期备份 `data/` 目录

### 7.5 ⚠️ YAML 导入只在启动时执行，且只增不更新
修改已有部门配置的正确方式：
```bash
curl -X PUT http://localhost:5000/api/departments/<id> \
  -H "Content-Type: application/json" \
  -d '{"title_selector": "新选择器", "date_selector": "新选择器"}'
```

### 7.6 ⚠️ Windows 终端编码
- Windows CMD 默认 GBK，print 含 emoji 会 `UnicodeEncodeError`
- `run.bat` 开头已处理：`chcp 65001 >nul`

### 7.7 ⚠️ DeepSeek API Key 的数据库 key 是 `deepseek_api_key`
```python
AppConfig.get('deepseek_api_key')   # ✅ 正确
AppConfig.get('api_key')            # ❌ 错误
```

### 7.8 ⚠️ config.yaml 部门名不要重复
YAML 加载器按 `school_id + name` 去重。同名部门第二个会被跳过。

### 7.9 ⚠️ 第 1 页选择器可能不匹配
部分 catalog 端点的第 1 页和第 2+ 页 DOM 结构略有不同。`engine.py` 已处理：第 1 页无匹配时自动跳到第 2 页。

### 7.10 ⚠️ 不要在模板中使用 `<base>` 标签
`announcement.html` 曾用 `<base>` 补全相对链接，但这会**劫持页面所有导航链接**。爬取时 `engine.py` 已将相对链接转为绝对链接。

### 7.11 ⚠️ datetime 时区问题——DAILY NEWS 致命坑
**现象**：DAILY NEWS 页面完全空白，没有任何报错信息。
**根因**：`datetime.now(timezone.utc)` (offset-aware) vs SQLite naive datetime → `TypeError`
**解决**：全部使用 `datetime.utcnow()`（naive datetime）
```python
# ❌ 错误
cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
# ✅ 正确
cutoff = datetime.utcnow() - timedelta(hours=72)
```

### 7.12 ⚠️ ngrok 安装路径
- 二进制和配置在 **D 盘**：`D:\Jinta\Tools\ngrok\`
- 启动必须带 `--config` 参数：`/d/Jinta/Tools/ngrok/ngrok http 5000 --config /d/Jinta/Tools/ngrok/config/ngrok.yml`
- **NGROK_CONFIG_DIR 环境变量不生效**，必须用 `--config` 命令行参数

### 7.13 ⚠️ Flask 多进程残留 — 端口被占（本轮新坑）
**现象**：修改 `app.py` 后 curl 测试返回旧行为（404、无新路由）。
**根因**：Windows 下 `fuser -k` 不可用，`taskkill` 可能漏杀子进程。多次重启后端口 5000 被多个旧 Python 进程占用，新修改的代码加载到了错误进程。
**解决**：
```bash
# 查看所有占用端口的进程
netstat -ano | grep ":5000 " | grep LISTENING
# 逐一杀掉
taskkill -F -PID <PID1> -PID <PID2> -PID <PID3>
# 确认清空后再启动
netstat -ano | grep ":5000 " | grep LISTENING || echo "clear"
```
**预防**：每次改代码后，先杀干净所有 5000 端口进程再重启。不要依赖 `fuser`。

### 7.14 ⚠️ 认证 hook 顺序 — `before_request` 中多个钩子的执行顺序
**现象**：如果在 `_ensure_scheduler()` 之后添加认证钩子，`_ensure_scheduler` 仍然在未认证状态下执行。
**解决**：认证钩子 `_auth_check()` 必须**注册在** `_ensure_scheduler()` 之前。Flask 按注册顺序执行 `before_request` 函数。

### 7.15 ⚠️ 设置页修改后不生效 — Flask 模板缓存
**现象**：修改了 `templates/settings.html`，磁盘文件已更新，但浏览器看到的仍是旧页面。
**根因**：Flask `debug=False` 时 Jinja2 会缓存编译后的模板。但更常见的原因是端口被旧进程占用（见 7.13）。
**解决**：先确认当前运行的进程是最新启动的（见 7.13），必要时 `taskkill` 全杀重启。

### 7.17 ⚠️ 新学校爬取为 0 — 站点发现（本轮核心）

**现象**：通过 UI 新增学校后点击「刷新」，返回 0 条通知。数据库里 school 存在但 departments 为空。

**根因**：`POST /api/schools` 只创建 School 记录，未创建部门。爬虫 `scrape_school()` 遍历 `school.departments` 为空列表。

**解决**（已完成三层机制）：
1. `POST /api/schools` → 自动创建 "通知公告" 默认部门
2. `scrape_department()` → 空选择器时自动触发 `_probe_selectors()` 探测 ZCMS 模式
3. `scrape_school()` → 仅有默认部门时自动调用 `discover_school_departments()` 智能发现整个部门树

**发现限制**：
- 站点发现依赖首页导航结构。非标准 CMS（如 cup.edu.cn）的导航提取可能不完整
- 非 ZCMS 站点无法精确匹配选择器，会回退到 `a[href]` 全页链接（临时模式，不保存）
- 发现过程耗时较长（每个部门页 ~1-2 秒），MAX_TOP_DEPTS=15 限制可防止无限循环

### 7.18 ⚠️ 部门父子树命名规范

**规则**：`_build_dept_tree()` 通过 `-` 分隔符识别父子关系。
- "教务部" → 父部门，"教务部-学籍管理" → 子部门
- 站点发现自动生成的子部门名遵循此规范（`f"{父名}-{子名}"`）
- **不要用 `-` 命名独立部门**，会被误识别为子部门

### 7.19 ⚠️ 选择器探测：永久保存 vs 临时回退

- ZCMS 精确匹配（6 个 SELECTOR_PROFILES 之一）→ `is_permanent=True` → 自动保存到 DB
- 全页链接回退（`a[href]` 选择器）→ `is_permanent=False` → 本次抓取使用但不保存 → 下次重新探测
- 探测失败 → 恢复 department 原始选择器（空）→ 下次抓取再次探测
- **在 DB 中查看**：`list_selector=''` 表示下次会触发探测；`list_selector='li.xxx'` 表示已永久保存

### 7.20 ⚠️ 中文过滤词精确匹配 vs 子串匹配 — 第五轮核心坑

**现象**：明明 `GENERIC_NAV_NAMES` 里有"医疗保险""本科招生"，但 cup.edu.cn 的"医疗卫生""本科生招生"还是被识别为部门。

**根因**：不同学校 HTML 中实际使用的文字不同：
- "医疗卫生" ≠ "医疗保险"（一字之差）
- "本科生招生" ≠ "本科招生"（多一个"生"字）
- "风华石大" → 校园文化杂志名，≠ "校园文化"

精确匹配 `text in SET` 对中文词形变化毫无容忍度。

**解决**：改为子串匹配 `any(kw in text for kw in SET if len(kw) >= 2)`。
- "医疗卫生" 子串匹配 "医疗" ✓
- "本科生招生" 子串匹配 "招生" ✓

**教训**：中文文本匹配必须用子串匹配，不要精确匹配。`GENERIC_NAV_NAMES`、`INFORMATIONAL_PAGE_NAMES`、`NOISE_TEXTS` 全部改用子串匹配。

### 7.21 ⚠️ 不要删除 `.env` 文件

**现象**：重启应用后之前的登录 cookie 全部失效，需要重新登录。
**根因**：SECRET_KEY 第一次自动生成后会写入 `.env`，但如果 `.env` 被删除或覆盖，SECRET_KEY 会重新生成，之前签发的所有 session cookie 作废。
**解决**：**不要删除 `.env` 文件**。`.gitignore` 已排除它，不会意外提交。

### 7.22 ⚠️ 站点发现的「MAX_TOP_DEPTS」限制 — 第六轮核心坑

**现象**：院系设置页枚举了 18 个实体 + 科学研究子菜单 6 个，去重后应有 ~22 个，但数据库只有 15 个。
**根因**：`MAX_TOP_DEPTS = 15`，去重后的候选列表被 `[:MAX_TOP_DEPTS]` 截断。列表页枚举的实体在前面（先处理），子菜单部门在后面被全部截掉。
**解决**：`MAX_TOP_DEPTS` 改为 30。**教训**：综合性大学可能有 20-30 个学院+科研实体，限制要设得足够大。

### 7.23 ⚠️ 学院 tzgg 子页面模板不匹配 — 第六轮核心坑

**现象**：多个学院的 `/tzgg/` 页面确认存在（HTTP 200），但爬取返回 0 条通知。
**根因**：不同学院的 tzgg 页面使用**不同的 HTML 模板**，即使在同一学校内也不一致。现有的 6 组 `SELECTOR_PROFILES` 只覆盖了 cupk.edu.cn 的 ZCMS 模式。cup.edu.cn 各学院的 tzgg 页可能使用：
- 自定义列表结构（非标准 ZCMS）
- AJAX 加载（需要 Selenium）
- 完全不同的 CSS 类名

**解决**：需要抓取各学院 tzgg 页的 HTML 来分析具体结构，然后添加新的 `SELECTOR_PROFILES`。不能用一套选择器覆盖整所学校的所有学院。

**教训**：
- 站点发现找到部门 ≠ 能成功爬取该部门
- 同一学校的不同学院可能使用完全不同的 CMS/模板
- 需要持续扩展 `SELECTOR_PROFILES` 词库

### 7.24 ⚠️ 子部门发现的 URL 去重 — 第六轮核心坑

**现象**：重质油全国重点实验室生成了 7 个子部门（通知公告、实验室新闻、开放课题、学术交流、人才培养、社会服务、新闻动态），其中"新闻动态"和"实验室新闻" URL 相同（都是 `/News/`）。
**根因**：`_discover_sub_departments()` 把实验室主页导航栏的**所有菜单项**都当作子部门。这些是导航标签，不是独立的通知列表页。
**解决思路**：
- 在子部门候选验证时，检查选择器探测出的列表项数量（<3 项则跳过）
- 子部门 list_url 和父部门相同时跳过（已实现）
- 按子部门名称再过滤一次 `INFORMATIONAL_PAGE_NAMES`（如"学术交流"、"社会服务"、"人才培养"）

### 7.25 ⚠️ per-department 异常保护

**现象**：`scrape_school()` 的 for 循环中，某个部门的 `scrape_department()` 抛出异常导致全校爬取中断，ScrapeLog 永久停留在 `running` 状态。
**解决**：在 for 循环内加 `try/except`，单个部门失败 `continue` 继续下一个。同时 `except` 外层仍保留全校级别的异常捕获。

### 7.26 ⚠️ `_probe_selectors` 返回 `(None, False)` 导致 TypeError — 第七轮核心坑

**现象**：`scrape_department()` 在探测选择器时报 `TypeError: 'NoneType' object is not subscriptable`，发生在 `list_sel = probed_profile['list_selector']` 行。

**根因**：`_probe_selectors()` 无匹配时返回 `return None, False`。Python 将 `(None, False)` 视为非空元组，`if probed:` 判定为 True，但 `probed_profile, is_permanent = probed` 解包后 `probed_profile = None`。

**解决**：改为 `return None`（独立 None 值，`if probed:` 正确判定为 False）。

**教训**：函数返回 sentinel 值时，不要用 `return None, False`，用 `return None`。Python 元组解包不检查元素是否为 None。

### 7.27 ⚠️ 同一 list_selector 的多个 profile 互相覆盖 — 第七轮核心坑

**现象**：安全与海洋工程学院的日期为 `div.time.gpArticleDate`（格式 YYYY.MM.DD），但探测时总是被 `ul.block-list li` + `span.gpArticleDate` 的 profile 抢先匹配，日期提取失败。

**根因**：`sub_list` 和 `block-list` 分别有两个 profile（rightDate vs leftDate，gpArticleDate span vs div）。`_probe_selectors` 按顺序尝试，第一个 list_selector 匹配的 profile 胜出，即使它的 date_selector 不对。

**解决**：合并相同 list_selector 的 profile，用逗号分隔多个 date_selector：`span.rightDate, span.leftDate` 和 `span.gpArticleDate, div.gpArticleDate, div.time`。

**教训**：SELECTOR_PROFILES 中，相同 `list_selector` 的 profile 必须合并为一个，用逗号分隔符组合多个 title/date 选择器。CSS `select_one('a, b')` 返回第一个在 DOM 中存在的元素。

### 7.28 ⚠️ 首页新闻摘要组件误导 tzgg 查找 — 第七轮核心坑

**现象**：石油工程学院等学院的 list_url 被设为学院首页（如 `/oil/`），而非 tzgg 子页（`/oil/tzgg/index.htm`）。首页只有 8 条新闻摘要，tzgg 子页有 20 条完整列表。

**根因**：`_find_notice_list_url` 检测到首页有 ≥5 项列表（来自 NewsConList/EventsList 等摘要组件），立即返回首页 URL，不再查找 tzgg 子页。

**解决**：标记首页摘要型 profile（NewsConList/EventsList/c-notice），首页匹配数 < 12 且为摘要型时不立即返回，继续尝试 tzgg 标准路径。如果 tzgg 路径有更多项，优先使用。

**教训**：首页新闻摘要 ≠ 完整通知列表。需要区分「首页最近 N 条」和「通知列表全量」。

### 7.29 ⚠️ JS 重定向无法被 requests 追踪

**现象**：人工智能学院（cise）首页返回 95 字节 HTML，内容为 `<script>window.location.href='https://www.cup.edu.cn/cupai/';</script>`。`requests.get()` 无法执行 JS，站点发现对该页面探测失败。

**解决**：手动识别并添加正确的 URL（cupai/tzgg/index.htm）。长期方案：在 `_fetch_html` 中检测 `<script>window.location` 模式并提取重定向 URL。

### 7.30 ⚠️ 非标准翻页格式：第 2 页不是 `/index_2.shtml`

**现象**：cupai、prplab 等子站点第 2 页返回 404（`/tzgg/index.htm/index_2.shtml`）。

**根因**：`_get_page_url` 默认拼接 `/index_N.shtml`，但这些站点的翻页格式可能是 `/index.htm?page=2` 或其他变体。当前代码只在 `/zcms/catalog/` 路径下做特殊处理。

**解决思路**：第 2 页 404 时尝试 `?page=2`、`&page=2`、`/index_N.htm` 等替代格式。或将列表页最大限制在 1 页（这些子站点通知量少，第 1 页通常已包含大部分内容）。

### 7.31 ⚠️ 通用选择器阻止 tzgg 子页查找 — 第八轮核心坑

**现象**：地球科学学院、理学院、经济管理学院明明有 tzgg 子页（`/geosci/tzgg/` 等），但 `_find_notice_list_url` 返回了学院首页。

**根因**：学院首页使用 `div.main li:has(a)`（通用列表选择器）匹配了 37 条项目。`_find_notice_list_url` 检测到 ≥12 条非摘要型匹配，立即返回首页 URL，不再查找 tzgg 子页。而 tzgg 子页只有 20 条（用更精确的 `ul.sub_list li`），但函数根本没检查。

**解决**：标记"通用列表 (li > a)"为 GENERIC_PROFILE_NAMES。首页匹配到通用选择器时不立即返回，继续检查 tzgg 路径。tzgg 匹配到专用选择器（GP CMS）时优先选择，即使条数更少。

**教训**：数量多≠质量好。通用 CSS 选择器（`div.main li:has(a)`）匹配面太广，容易匹配到导航链接、侧边栏等非通知内容。应优先选择匹配专用 CSS class 的页面（如 `ul.sub_list li`）。

### 7.32 ⚠️ 翻页格式多样：`/index_N.shtml` 不是唯一格式

**现象**：地球科学学院第 2 页 `/index_2.shtml` 返回 404，但 `?page=2` 是有效的。

**根因**：cup.edu.cn 各子站点使用不同的翻页格式。`geosci` 使用 `?page=N`，`tiyubu` 也使用 `?page=N`，`cupai` 可能有其他格式。`_get_page_url` 只生成一种格式。

**解决**：添加 `PAGINATION_FALLBACKS` 降级列表（index_htm、query_page_index、query_page）。第 2 页 404 时 `_detect_pagination_style()` 依次尝试各格式的 HEAD/GET 请求。成功后 `scrape_department` 使用该格式翻剩余页。

**教训**：不同网站甚至同一网站的不同子站点可能使用不同的翻页格式。必须有降级机制。

### 7.33 ⚠️ `urljoin` 基准 URL 决定相对链接解析

**现象**：cupai 子站点详情链接 `/cupai/tzgg/xxx.htm` 被解析为 `/xxx.htm`。

**根因**：`_process_announcement_item` 使用 `school_base_url`（`https://www.cup.edu.cn`）作为 `urljoin` 基准。相对链接 `xxx.htm` 被解析为 `https://www.cup.edu.cn/xxx.htm` 而非 `https://www.cup.edu.cn/cupai/tzgg/xxx.htm`。

**解决**：改用 `department.list_url`（`https://www.cup.edu.cn/cupai/tzgg/index.htm`）作为基准。`urljoin` 正确处理相对路径。

**教训**：`urljoin(base, relative)` 的 base 必须是包含路径的完整 URL。`https://example.com` + `xxx.htm` → `https://example.com/xxx.htm`，但 `https://example.com/sub/dir/index.htm` + `xxx.htm` → `https://example.com/sub/dir/xxx.htm`。

### 7.34 ⚠️ DB 查询中文 LIKE 模式匹配过度

**现象**：更新"理学院"时错误匹配了"地球物理学院"（因为后者也包含"理学院"）。

**根因**：`Department.name.like('%理学院%')` 子串匹配导致过度匹配。中文部门名称没有空格分隔，LIKE 模式容易命中包含目标字符串的其他名称。

**解决**：精确匹配 `Department.name == '理学院'`。中文名称查询必须用精确匹配或正则边界匹配。

**教训**：中文关键字搜索不能用简单的 `LIKE %keyword%`，必须精确定位或使用 `=` 精确匹配。

### 7.35 ⚠️ 翻页链接格式 `index1.htm`（零偏移）— 第九轮核心坑

**现象**：地球科学学院和体育学院的第2页 404，但翻页探测（`_detect_pagination_style`）报告 `query_page` 格式有效。

**根因**：这些站点使用零偏移翻页：第1页=`index.htm`，第2页=`index1.htm`，第N页=`index{N-1}.htm`。`?page=N` 也恰好被服务端接受（因为它是 query string 参数，被 index.htm 忽略后服务端仍返回第1页内容）。所以 `_detect_pagination_style` 返回 `query_page` 虽然能翻页，但走的是参数注入而非真实翻页结构。

**解决**：新增 `index_htm_offset` style，从HTML翻页链接中正确识别；`_get_page_url()` 剥除 `/index.htm` 文件名后缀后再拼接 `index{N-1}.htm`。

**教训**：HTML 翻页链接是最权威的翻页格式来源。不要仅依赖 HTTP 探测（query string 参数可能被服务端静默接受但不反映真实结构）。

### 7.36 ⚠️ IE 条件注释中的 JS 重定向误匹配 — 第九轮核心坑

**现象**：外国语学院 tzgg3 页面（`/dfl/tzgg3/index.htm`）被 `_fetch_html` 错误重定向到 `brower.html`，爬取返回 0 条。

**根因**：页面包含 `<!--[if lt IE 9]><script>window.location.href='brower.html'</script><![endif]-->`，这是仅对 IE<9 生效的条件注释。但 `_fetch_html` 的 JS 重定向正则无条件匹配了其中的 `window.location.href='brower.html'`。

**解决**：JS 重定向检测前先移除 IE 条件注释：`re.sub(r'<!--\[if\s[^\]]*\]>.*?<!\[endif\]-->', '', html, flags=re.IGNORECASE | re.DOTALL)`。

**教训**：HTML 注释中的 JS 代码不应被当作真实执行逻辑。IE 条件注释（`<!--[if ...]>`）在现代浏览器中无效，但会被正则表达式匹配到。

### 7.37 ⚠️ tzgg 子页的命名变体 — 第九轮核心坑

**现象**：外国语学院的通知公告链接是 `tzgg3/`（不是常见的 `tzgg/`），站点发现的 `standard_paths` 列表中不包含。

**根因**：不同学院使用不同的子路径命名：`tzgg/`（常见）、`tzgg2/`（研究生教育）、`tzgg3/`（外国语学院）、`xytzgg/`（地球物理学院"学院通知公告"）、`news/`（体育学院）、`notice/`（科研处）。

**解决**：`site_discovery.py` 的 `standard_paths` 新增 `/tzgg2/index.htm`、`/tzgg3/index.htm`、`/news/`、`/news/index.htm`。

**教训**：标准路径列表需要持续扩充。命名没有统一规范，各学院自主决定。

### 7.38 ⚠️ CUP-list 日期格式 DDMM月 — 第十一轮核心坑

**现象**：科研项目（kjc）页面的 `ul.CUP-list li` 中，日期以 `1707月` 形式出现（day=17, month=07），无年份。现有 `parse_date` 无法解析。

**根因**：cup.edu.cn 科研处使用自定义 tab 面板模板。日期被拆分为两个 span：`<span>17</span><span>07月</span>`，CSS 渲染时合并显示为 `1707月`。`date_selector: span` 取到第一个 span 的文本 `1707月`。年份不在列表中，需从标题或当前时间推断。

**解决**：新增 `DDMM月` 日期格式（re: `(\d{1,2})(\d{1,2})月`），默认使用当前年份；若解析结果在未来，自动减1年。该格式放在 `(\d{1,2})月(\d{1,2})日` 之前以避免冲突。

**教训**：非标准 CMS 的日期格式千奇百怪。`span` 组合日期（day+month 分离）是常见模式。年份推断用"未来则减1年"是合理的启发式。

### 7.39 ⚠️ Jinja2 模板中 `item.items` 与 Python dict 内置方法冲突 — 第十二轮核心坑

**现象**：渲染 `school.html` 时报 `TypeError: 'builtin_function_or_method' object is not iterable`，发生在 `{% for sub in item.items %}`。

**根因**：Jinja2 的属性访问回退机制中，`item.items` 先查找 `item['items']`（通过 `__getitem__`），失败后回退到 Python 对象属性访问 `getattr(item, 'items')`。`dict.items` 是 Python dict 的内置方法，所以 `item.items` 返回的是 bound method 而不是字典键值。

**解决**：将键名从 `'items'` 改为 `'depts'`（或其他不与 dict 方法冲突的名称）。

**教训**：在 Jinja2 模板中访问字典键时，**永远不要用 `'items'`、`'keys'`、`'values'`、`'get'`、`'update'` 等 Python dict 方法名作为键名**。如果无法避免，在模板中使用 `item['items']` 下标语法（Jinja2 也能识别）。

### 7.40 ⚠️ GP CMS 详情页使用 `<article>` 作为正文容器 — 第十二轮核心坑

**现象**：cup.edu.cn 几乎所有通知正文为空（680/737），但 detail_url 可以正常访问（200 OK）。

**根因**：cup.edu.cn 多个学院的详情页使用 HTML5 `<article>` 标签包裹正文，而非 `<div class="article-content">` 等。但所有 `SELECTOR_PROFILES` 的 `content_selector` 都是 `div.article-content, div.main, div.content, div.gp-article`，缺少 `article`。CSS `select_one()` 找不到匹配元素，返回 None，`content_html` 为空字符串。

**解决**：所有 `content_selector` 末尾追加 `, article`。同时修复了 176 个历史遗留的错误 URL（根路径→部门子路径）。

**教训**：
- `<article>` 是 HTML5 语义标签，被越来越多的高校网站使用。选择器列表应默认包含它。
- `content_selector` 中 `div.article-content` 包含子串 `article`，容易让人误以为已覆盖，实际 CSS 选择器精确匹配 `article` 标签和 `div.article-content` class 是不同的。

### 7.41 ⚠️ CUP-list 的 `<a>` 标签内嵌套日期 span，title_selector 不能是 `a` — 第十三轮核心坑

**现象**：科研项目的通知标题全部带有 "1707月"、"1407月" 等日期前缀（如 "1707月2026年度山东省科学技术奖申报项目公示5"），标题被日期文本污染。

**根因**：CUP-list 的 HTML 结构中，`<a>` 标签内同时包含日期 `<span class="date">` 和标题 `<span class="artText">`：
```html
<li>
  <a href="...">
    <span class="date">
      <span class="month gp-f12">17</span>
      <span class="day gp-f18">07月</span>
    </span>
    <span class="artText gp-f16">2026年度山东省科学技术奖申报项目公示5</span>
  </a>
</li>
```
`title_selector: "a"` 提取了整个 `<a>` 的文本内容，把嵌套的日期 span 也包含在内。同时 `date_selector: "span"` 匹配到第一个 span（`.date`），日期提取倒是正确的。

**解决**：
- `title_selector`: `"a"` → `"span.artText"`（只取标题 span）
- `date_selector`: `"span"` → `"span.date"`（精确匹配日期 span）
- 历史数据修复：正则 `^\d{2,4}月` 清除 51 条已存标题的日期前缀

**教训**：
- 当 `<a>` 标签内有嵌套子元素时，`get_text()` 会递归获取所有子元素的文本。选择器要精确到具体的文本容器。
- CUP-list 的 tab 面板（成果/公示/通知公告等）都使用相同的 HTML 结构，修复 title_selector 不会破坏其他 tab 的抓取。

### 7.42 ⚠️ 增量抓取设计 — 第十四轮核心实现

**背景**：之前的抓取是"全量遍历+去重"，每次定时抓取都从第1页重新请求所有列表页，即使没有新通知也要走完全部页面。

**设计**：
- 新增 `Department.last_scraped_at` 字段（DATETIME），记录每个部门上次完整抓取完成的时间
- 在 `scrape_department()` 中：
  - `last_scraped_at IS NULL` → 全量模式（首次抓取）
  - `last_scraped_at IS NOT NULL` → 增量模式：遇连续 `INCREMENTAL_THRESHOLD=3` 条已存在通知即停止翻页
- 每处理一条通知：新通知→重置计数器，已存在且增量模式→计数器+1
- 每页结束后：`consecutive_existing >= 3` → break（已追平上次进度）
- 抓取完成后：`department.last_scraped_at = datetime.utcnow()`

**效果**：
- 首次全量：~25 分钟，数百次 HTTP 请求
- 后续增量：**35 秒**，仅 1-2 页/部门（↓97%）
- 无新通知时：每个部门仅请求第1页前3条就停止

**教训**：
- 列表页按时间倒序是几乎所有高校网站的标准做法，利用这个特性做增量停止是安全的
- 阈值设为3（而非1）防止因个别乱序条目导致提前终止
- `consecutive_existing` 只在增量模式下计数，全量模式不受此限制
- 必须在新通知出现时重置计数器，否则会漏掉后续新通知

### 7.43 ⚠️ resvg Python 绑定 render() 不可用 — 第十五轮核心坑

**现象**：`resvg` Python 包（0.2.0）的 `render()` 函数返回的 PNG 数据全部透明，即使最简单 `<rect fill="red"/>` 也是全透明。

**根因**：`resvg._resvg.render()` 函数存在 bug 或 API 使用不匹配。设置 `bg_color` 参数可以填充背景色，但 SVG 内容仍不会被渲染。

**解决**：使用 `resvg` **CLI 工具**代替 Python 绑定：
```bash
resvg --width 1024 --height 1024 icon.svg icon.png
```
然后 PIL `Image.resize()` 缩放到各目标尺寸，PIL 原生 `Image.save(format='ICO', sizes=[...])` 打包 ICO。

**教训**：
- 优先用 CLI 工具验证，"能跑通"比"看起来优雅"重要
- PIL 原生 ICO 保存比手动构造 BMP header 可靠得多（之前尝试手动写 BITMAPINFOHEADER + AND mask 弄了两小时）
- 超采样（8x/4x/2x → LANCZOS 缩放）是小尺寸图标保持清晰的关键

### 7.44 ⚠️ 跨 shell 中文文件名编码灾难 — 第十五轮核心坑

**现象**：在 Git Bash → Python → PowerShell → VBS 之间传递中文路径时，文件名在 NTFS 磁盘上变成乱码字节。Windows Explorer 中显示为无法识别的字符，文件删不掉、打不开。

**根因**：多层级编码转换链路断裂：
- Git Bash：内部使用 UTF-8，但通过 `bash.exe` 调用的进程可能收到 GBK（Windows 中文系统 locale）
- Python：`filesystem_encoding=utf-8`、`stdout_encoding=gbk`，看似一致但通过 subprocess 传给 PowerShell 时编码取决于管道/参数编码
- PowerShell：COM 对象 (`WScript.Shell`) 的 `CreateShortcut()` 接受 Unicode 路径，但通过命令行传参时可能被 bash 转码
- VBS：不支持 UTF-8（`cscript.exe` 读 UTF-8-sig BOM 文件会报"无效字符"）

**关键表现**：
- `os.fsencode(entry.name).hex()` = `e5ada6e6a0a1...`（正确 UTF-8）
- `os.fsdecode(bytes.fromhex('e5ada6...'))` = `学校通知工具`（代码点 U+5B66 U+6821... 全对）
- 但终端显示和 Windows Explorer 渲染为 `ѧУ֪ͨ����`（GBK 终端误读 UTF-8 字节）

**解决**：
1. **桌面快捷方式用 ASCII 文件名**：`School Notifier.lnk` 而非 `学校通知工具.lnk`。用户可手动重命名。
2. **启动脚本放项目目录**：`school-notifier.bat`（ASCII），不在桌面放 .bat。
3. **创建快捷方式用 PS 脚本文件**（UTF-8-sig BOM）+ `subprocess.run(['powershell', '-File', ps_path])`，不要用 `-Command` 传中文参数。
4. **Python 源码中避免中文字面量**：用 `bytes.fromhex()` + `.decode('utf-8')` 动态构造，或者干脆用 ASCII 名。

**教训**：
- ⛔ 禁止在跨 shell 场景使用中文文件名。桌面快捷方式用英文名，用户自己改名。
- `os.fsencode()` 返回的字节是磁盘上真实存储的，不受终端编码影响——用它来 debug。
- Windows 上的 ICO 多分辨率：<256px 用 PNG-in-ICO 即可，无需手动写 BMP header。PIL 原生 `save(format='ICO', sizes=[...])` 最可靠。
- 删除文件时检查 `st_size` 要精确！`< 2000` 曾误删了用户全部桌面快捷方式（血的教训）。

### 7.45 ⚠️ DOM 结构签名必须包含子元素标签序列 — 第十六轮核心坑

**现象**：`find_repeating_blocks()` 在 cup.edu.cn 的 `ul.sub_list` 页面上将通知列表项（`<li>`）和导航菜单项（`<li>`）归为同一组，导致导航菜单被误判为通知列表。

**根因**：最初的结构签名只用了 `(tag_name, class_list, has_link)`。通知列表的 `<li>` 和导航菜单的 `<li>` 有相同的 tag 和都可能无 class，但它们内部的子元素结构完全不同：
- 通知 `<li>`：`<a>标题</a> <span class="rightDate">日期</span>`
- 导航 `<li>`：`<a>学院概况</a>` 或 `<a>学院概况</a> <ul>子菜单</ul>`

**解决**：签名中加入 `child_tags` 元组（直接子元素的标签序列）。这样 `('a', 'span')` ≠ `('a',)` ≠ `('a', 'ul')`，三种不同结构的 `<li>` 会被分到不同的组。

**教训**：
- 仅靠 tag+class 做 DOM 签名不够，子元素结构是区分列表项和导航项的关键信号
- `child_tags` 在签名中比 `has_link`/`has_date` 更重要——它编码了元素的内部 DOM 模板

### 7.46 ⚠️ 日期比例是区分通知列表 vs 导航菜单的最强信号 — 第十六轮

**现象**：即使有了 `child_tags` 签名，仍有部分页面的候选块在链接比例等指标上得分相似。

**根因**：通知列表的普遍特征——每个条目都附带发布日期。导航菜单不会。在 `is_likely_notice_list()` 中要求 `date_ratio >= 0.3` 可以干净地过滤掉所有导航菜单、侧边栏链接块等噪声。

**验证数据**：
- cupk ZCMS 通知列表：date_ratio=1.0, link_ratio=1.0 → LIST ✓
- cupk 面包屑导航：date_ratio=0.0, link_ratio=1.0 → NOT（日期比例0%）
- cup GP sub_list：date_ratio=1.0, link_ratio=1.0 → LIST ✓
- cup 学院导航菜单：date_ratio=0.0, link_ratio=1.0 → NOT（日期比例0%）

**教训**：在评分函数中，`date_ratio` 权重应该最高（当前 1.5x 加成），其次是 `link_ratio`（1.0x），最后是 `unique_link_ratio`（0.5x）。这个权重分配是经过两校多页面验证的最优配置。

### 7.47 ⚠️ curl_cffi 没有 `apparent_encoding` 属性 — 第十七轮核心坑

**现象**：将所有 `import requests` 替换为 `from curl_cffi import requests` 后，`_fetch_html()` 报 `AttributeError: 'Response' object has no attribute 'apparent_encoding'`。

**根因**：`curl_cffi` 的 Response 对象虽然模仿 `requests` API，但不是 100% 兼容。`apparent_encoding` 是 `requests` 库特有的启发式编码检测属性（从 HTML 内容中猜测编码），`curl_cffi` 没有实现它。curl_cffi 提供了 `charset_encoding`（从 HTTP Content-Type header 解析的编码）作为替代。

**解决**：用 `try/except AttributeError` 包装编码设置逻辑：
```python
try:
    resp.encoding = resp.apparent_encoding or resp.charset_encoding or 'utf-8'
except AttributeError:
    resp.encoding = resp.charset_encoding or resp.encoding or 'utf-8'
```

**教训**：
- "drop-in replacement" 不是 100% 兼容，总有一些细节差异
- 爬虫的编码处理最好有多层 fallback：`apparent_encoding` → `charset_encoding` → `encoding` → `'utf-8'`
- 对于中文高校网站，`Content-Type` header 通常已经声明了正确的 charset，`charset_encoding` 足够

### 7.48 ⚠️ Scrapling 0.4.11 在 Python 3.14 上的兼容性问题 — 第十七轮核心坑

**现象**：
1. `relocate()` 方法报 `TypeError: string indices must be integers, not 'str'`（`__calculate_similarity_score` 中参数类型错误）
2. `find_similar()` 方法报 `TypeError: '>=' not supported between instances of 'float' and 'dict'`（`__are_alike` 中 `similarity_threshold` 参数类型错误）

**根因**：Scrapling 0.4.11 的部分自适应功能在 Python 3.14 上存在 bug。`__calculate_similarity_score` 中 `data` 参数类型传递不一致；`__are_alike` 中 `similarity_threshold` 从 kwargs 取出时未正确处理默认值。这些是 Scrapling 内部实现问题，非我们的使用方式问题。

**解决**：
- 放弃 `relocate()` 和 `find_similar()`，改用 `find_by_text()` + `find_by_regex()` 作为元素定位策略
- `find_by_text()` 通过文本内容匹配，不依赖 DOM 结构，在页面改版时更稳健
- `save()` / `retrieve()` 仍然可用——用它们保存/读取元素文本指纹，然后用 `find_by_text()` 在新页面中搜索

**教训**：
- 新库+新 Python 版本 = 预期会有兼容性问题
- 文本匹配（`find_by_text`）比结构匹配（`find_similar`）在页面改版场景下更可靠——文本内容通常不变，CSS 类名会变
- 不要强依赖一个库的全部功能——保留我们的 DOM 分析作为独立 fallback

### 7.49 ⚠️ 选择器自愈必须验证日期比例 — 第十七轮核心坑

**现象**：自愈生成的 `ul.c-header_lib_ul li` 选择器匹配了 41 个元素，看起来"成功修复"，但实际是导航栏（`c-header_lib_ul` 是页面顶部的通用导航），date_ratio=0。

**根因**：`find_by_text()` 搜索整个页面，第一个匹配可能在任何区域。如果保存的文本较短（如标题的一部分），可能匹配到导航栏中的相似文本（导航链接通常也有较多文字）。不加验证地返回生成的选择器会导致后续爬取大量噪音数据。

**解决**：
1. 搜索时优先在内容区域查找（`main`、`article`、`div.content` 等）
2. 生成选择器后验证：匹配项数 3-200、日期比例 ≥30%、链接比例 ≥50%
3. 不通过验证的选择器直接拒绝（返回 None），让 `scrape_department` 走原有 fallback（第2页尝试）
4. 复用 7.46 的日期比例经验——这是区分列表和导航的最强信号

**教训**：
- 自愈不等于"找到替代选择器"——还必须验证替代选择器的质量
- 日期比例在自愈验证中同样是最强信号（与 7.46 一致）
- 导航栏文本通常也不短（"学院概况"、"师资队伍"等都是 4-8 字），所以仅靠文本长度无法区分

### 7.50 ⚠️ Flask进程不重启 = 改了代码不生效 — 第十八轮核心坑

**现象**：改了 `engine.py` 的 `scrape_school()` 加了 `is_bare` 自动站点发现逻辑，但用户网页上点击爬取始终返回0条（10ms完成）。通过命令行直接运行 Python 脚本却正常工作。

**根因**：Flask 进程（PID 10980）是在代码修改前启动的。Python 的 `sys.modules` 缓存了旧版本的模块。即使路由中用了 `from scraper.engine import ...`（函数内导入），模块已经被缓存，不会重新读取磁盘文件。

**解决**：
```bash
# 1. 查看所有占用端口的进程
netstat -ano | grep ":5000 " | grep LISTENING
# 2. 逐一杀掉
taskkill -F -PID <PID1> -PID <PID2>
# 3. 确认清空后再启动
netstat -ano | grep ":5000 " | grep LISTENING || echo "clear"
```

**教训**：
- ⛔ **每次改完任何 Python 代码后，必须先杀进程再重启！** 修改不生效是常态，不是例外。
- 命令行直接运行 Python 脚本验证 ≠ Flask Web 进程验证。命令行总是用最新代码，Web 进程可能还在用旧缓存。
- 时间是最诚实的信号：10ms 完成爬取 = 根本没发 HTTP 请求（`fetch_html` 一次至少 200ms+）

### 7.51 ⚠️ 新学校默认部门 list_url 为空 → 爬取直接跳过 — 第十八轮核心坑

**现象**：添加新学校后点击爬取，日志无任何HTTP请求，直接返回 `new=0 total=0`，耗时10ms。

**根因**：`scraper/engine.py:545-547`：
```python
if not (department.list_url or '').strip():
    logger.warning(f"[{department.name}] list_url 为空，无法爬取...")
    return 0, 0
```
`scrape_department()` 的第一道检查就是 list_url。如果为空，直接返回 (0, 0)，不会进入 `is_bare` 自动站点发现逻辑。而 `is_bare` 检查在 `scrape_school()` 中，作用是「如果部门为空/仅有默认且无选择器 → 运行站点发现」。但 list_url 为空时会先被 `scrape_department` 拦截。

**解决**：
- `POST /api/schools` 创建默认部门时确保 `list_url = school.url`（第699行已有此逻辑，需确认正确执行）
- 如果学校已存在且 list_url 为空，手动修复：
```python
dept.list_url = school.url
db.session.commit()
```
- 同时检查 `is_bare` 条件是否只检查了 `list_selector` 而没检查 `list_url`（当前只检查 `list_selector`，但 list_url 为空时也会导致0条）

**教训**：
- `is_bare` 条件应同时检查 `list_url` 和 `list_selector`。只检查选择器不够，空URL同样会导致爬取失败。
- 新建学校的默认部门必须继承学校的 URL。如果用户在UI中新加学校时没填URL，默认部门的 list_url 就是空的。

### 7.52 ⚠️ `sample_titles` vs `sample_items` 变量名拼写错误 — 第十八轮致命bug

**现象**：发现向导（🔍 自动发现）在浏览器中执行到部门探测阶段后，所有部门都显示"探测失败"，最终返回0个候选部门。Flask后台日志中每个部门都报：
```
WARNING: 探测部门失败 XXX: name 'sample_titles' is not defined
```

**根因**：`app.py:1222` 定义了变量 `sample_items = list_result.get('sample_titles', [])`，但 `app.py:1256` 引用时写成了 `'sample_titles': sample_titles[:3]`。`sample_titles` 这个变量根本不存在。

由于这行在 `try/except` 块内（第1262行），`NameError` 被捕获后将该部门标记为"探测失败"并跳过。**后果：100% 的部门探测都失败，发现向导永远返回空结果。**

**解决**：`sample_titles[:3]` → `sample_items[:3]`（一行改动）

**教训**：
- ⛔ `try/except Exception` 会吞掉 `NameError`（变量名拼写错误），导致静默失败
- 所有探测失败且错误消息相同 → 很可能是代码bug而非数据问题
- 写代码时定义变量后立即检查引用处名称是否一致（IDE 的未定义变量高亮很重要）

### 7.53 ⚠️ 方式C（自动发现+手动微调）的正确使用流程 — 第十八轮

**背景**：用户尝试添加新学校（华中科技大学）时不清楚完整操作流程。方式C是"自动发现→预览→勾选确认→手动微调"的混合模式。

**正确流程**：
1. 打开 `http://localhost:5000/schools/manage`
2. 点击「添加学校」→ 填写名称和官网URL → 保存
3. 在学校卡片上点击「🔍 自动发现」→ 等待进度条完成
4. 在结果预览中勾选/取消勾选部门 → 点击「✅ 确认并添加部门」
5. 对于选择器不准的部门：点击「部门」→ 找到该部门 → 「编辑」→ 修改选择器
6. 使用「🧪 测试选择器」按钮实时验证选择器是否匹配到条目
7. 保存后回到首页 → 点击「刷新」开始爬取

**常见问题**：
- 发现向导报错 → 检查 Flask 进程是否为最新代码（见 7.50）
- 向导返回0个部门 → 检查是否有 `sample_titles` bug（见 7.52）
- 爬取返回0条 → 检查部门 `list_url` 是否为空（见 7.51）
- 选择器不匹配 → 使用「🧪 测试选择器」验证，不同学校/CMS的选择器完全不同

**教训**：
- 先确认爬取基本链路通（首页可访问、list_url 不为空、选择器能匹配到条目），再跑全量
- 采样3-5个部门测试后再批量处理，比一次跑几十个部门发现问题后重来高效得多

### 7.54 ⚠️ 后台线程中访问 db.session 必须推入 Flask app context — 第十九轮核心坑

**现象**：发现向导（🔍 自动发现）在华科执行到部门探测阶段时爆出 "发现失败: Working outside of application context."，前端进度条停在30%。

**根因**：`POST /api/schools/<id>/discover` 路由中，`_run_discovery_in_background()` 通过 `threading.Thread(daemon=True)` 在后台线程执行。该函数内调用链为 `build_profile_from_knowledge()` → `get_global_knowledge()` → `School.query.all()`。Flask-SQLAlchemy 的 `db.session` 需要 Flask app context，后台线程默认没有。

**解决**：在 `_run_discovery_in_background()` 函数体开头添加一行：
```python
app.app_context().push()
```
（`app` 是 app.py 模块级的 `app = Flask(__name__)`，可直接引用）

**教训**：
- 任何 threading.Thread 中如果间接或直接使用了 `db.session`、`current_app`、`g`、`url_for`，必须先 push app context
- `School.query.all()` 看似只是 ORM 查询，实际上依赖 Flask-SQLAlchemy 的 session 绑定，必须有 app context
- 类似的："Working outside of request context" 需要在请求线程外使用时注意

### 7.55 ⚠️ 发现向导的通用选择器不可直接信任 — 第十九轮核心坑

**现象**：发现向导返回的部门中，`list_selector` 为 `div.main li:has(a), div.content li:has(a), ul.list-tz li` 的部门全部爬取为0条。虽然 `detect_notice_list` 报告 confidence=0.95，但实际匹配到的是导航菜单（如"医院概况""中心简介""机构设置"等），不是通知列表。

**根因**：通用 profile（`div.main li:has(a)` 类）匹配面太广。很多高校网站的侧边导航菜单恰好满足"多个`<li>`含`<a>`"的结构特征，`detect_notice_list` 对这类结构的置信度虚高。HUST 的多个部门（审计处、总务后勤处、校医院、档案馆等）都中了这个坑。

**解决**：
- 对每个发现向导返回的部门，如果 selector 是通用 `div.main li:has(a)` 类型，必须手动验证
- 使用「🧪 测试选择器」按钮在浏览器中验证，或写脚本实际爬取测试
- 正确做法：用 `detect_notice_list` 重新探测同一URL，它通常会找到更精确的选择器（如 `div.inner > div.conright > div > ul.listul > li`）

**常见精确选择器模式（HUST）**：
- `div.inner > div.conright > div > ul.listul > li` + date: `div.sj` 或 `div.bt`
- `div.center > div.conright > div > ul.listul > li` + date: `div.bt`
- `div.main-zyr > div.main-zyrx > div.lby > ul > li` + date: `small`
- `div.nav_right.fr > div.right_inner > div.list > ul > li` + date: `div.date1`

**教训**：
- profile_match（confidence 0.95）≠ 选择器正确。通用选择器的95%置信度是假的——它确实匹配了列表，但不是通知列表
- dom_analysis 的 confidence 通常更可靠，因为它考虑了日期比例和链接比例
- **发现向导结果必须抽样验证后再批量应用**，不能全选直接确认

### 7.56 ⚠️ YYYY-MM 日期格式 — 第十九轮核心坑

**现象**：网络空间安全学院（cse.hust.edu.cn）和未来技术学院（sft.hust.edu.cn）的列表项日期为 `2026-05` 格式（仅有年月，无日期），`parse_date()` 返回 None，导致所有通知被过滤。

**根因**：`change_detector.py` 的 `DATE_PATTERNS` 不支持 `YYYY-MM` 格式。年月格式在较新的 CMS 中越来越常见。

**解决**：
1. 在 `DATE_PATTERNS` 列表末尾添加：
```python
(re.compile(r'(\d{4})-(\d{1,2})(?![\d-])'), 'YYYY-MM'),
```
注意负向前瞻 `(?![\d-])` 防止与 `YYYY-MM-DD` 冲突。

2. 在 `parse_date()` 中添加 `YYYY-MM` 分支：
```python
elif fmt == 'YYYY-MM':
    year, month = int(groups[0]), int(groups[1])
    try:
        return datetime(year, month, 1, tzinfo=timezone.utc)
    except ValueError:
        continue
```
（无日期信息时默认取当月1号）

**教训**：
- 日期格式需要持续扩充。每种新学校/CMS都可能带来新格式
- `YYYY-MM` 不能简单用 `(\d{4})-(\d{1,2})` 匹配（会与 `YYYY-MM-DD` 冲突），必须加负向前瞻

### 7.57 ⚠️ `div.v_news_content` 是非常见正文容器 — 第十九轮

**现象**：工程科学学院详情页（ses.hust.edu.cn）有16KB+的内容，但正文提取为空。默认 `content_selector` 列表（`div.article-content, div.content, div.main, article`）都不匹配。

**根因**：该站点使用 `div.v_news_content` 作为正文容器。`v_` 前缀在高校网站中并不少见（可能是某CMS模板的命名约定）。

**解决**：将这个部门的 `content_selector` 改为 `div.v_news_content, div.article-content, div.content, div.main, article`。

**教训**：
- `content_selector` 的默认列表需要持续扩充。`div.v_news_content` 可以考虑加入全局默认值
- 验证一个部门的选择器是否工作，不能只看列表页——必须测试详情页的正文提取

### 7.58 ⚠️ Playwright sync_api 线程安全 — 第二十轮核心坑

**现象**：Playwright 同步 API 的 Browser 对象不是线程安全的。如果多个线程同时使用同一个 browser 实例创建 page/context，会导致 CDP（Chrome DevTools Protocol）连接异常。

**根因**：Playwright 内部使用 WebSocket 连接到浏览器进程。多个线程共享一个 WebSocket 连接会导致消息交错，可能触发 `Connection closed` 错误或死锁。

**解决**：使用 `threading.Lock` 序列化所有浏览器操作。每个 `fetch_html_with_browser()` 调用都在锁内完成：获取 browser → 创建 context → 创建 page → goto → content → 关闭 page → 关闭 context。由于浏览器回退是低频操作，锁竞争可忽略。

**教训**：
- Playwright sync_api 的 Browser 不是线程安全的，必须用锁保护
- BrowserContext 和 Page 也不是线程安全的，每个线程应创建独立实例
- 不要在多个线程间共享 Page 对象
- 锁的粒度要覆盖整个"获取→使用→清理"周期，避免泄漏

### 7.59 ⚠️ `wait_until='networkidle'` 的坑 — 第二十轮

**现象**：某些页面有持续的网络连接（WebSocket 实时推送、长轮询、分析脚本），`wait_until='networkidle'` 会一直等到超时（默认 30s），导致 Playwright 抓取极慢。

**根因**：`networkidle` 等待直到 500ms 内没有超过 0 个网络连接。WebSocket 连接被视为永久活跃连接，永远不会进入 idle 状态。

**解决**：
- 默认使用 `wait_until='networkidle'` 等待 AJAX（多数页面正常）
- 超时 30s 后不抛异常，返回当前已加载的 HTML（`page.goto` 的 timeout 控制）
- 对于已知问题页面（如含聊天 widget 的），可传 `wait_until='domcontentloaded'` + 额外 `time.sleep(2)` 等待 AJAX
- `fetch_html_with_browser()` 暴露 `wait_until` 参数，调用方可按需调整

**教训**：
- `networkidle` 不是银弹——长连接页面需要 fallback 策略
- 设置合理的 timeout 比选择完美的 wait_until 更重要
- 对于中国高校网站，大多数是传统的服务端渲染 + AJAX 加载，`networkidle` 效果很好

### 7.60 ⚠️ `is_js_required()` 的阈值调优 — 第二十轮

**现象**：初版 `is_js_required()` 判断 `< 80` 字符 body 文本即认为需 JS，导致正常但简洁的列表页被误判，触发不必要的 Playwright 重试。

**根因**：body 文本长度不是判断 SPA 的好指标。一个正常的通知列表页可能只有少量列表项（如 `<li><a>标题</a><span>日期</span></li>`），去标签后文本很短。反而是全页面的 script 标签数量 + 框架挂载点更可靠。

**解决**：
- Signal 2（body 文本）阈值从 80 → 30 字符（只抓真正空的 body）
- Signal 5（多 script 少全文）承担 SPA 检测主力：≥5 个 script 且全文 < 200 字符
- Signal 1（框架挂载点 `<div id="app">` 等）捕获 React/Vue 骨架
- Signal 4（Cloudflare/拦截页面）用关键词匹配
- 每个信号独立判断，任一命中即返回 True

**教训**：
- 启发式检测要在"漏判"和"误判"间权衡。宁可误判（多花 2-3s 用浏览器重试）不可漏判（导致 0 条通知）
- 阈值必须用真实页面数据校准，不能凭感觉定
- 多个弱信号组合 > 单一强信号（避免单一维度的边界情况）

```bash
# 启动应用
cd d:/Jinta/Documents/Claude\ Code/school-watcher
python app.py

### 7.61 ⚠️ 路由改造后必须检查 imports — 第二十一轮致命坑

**现象**：所有学校的「刷新」按钮返回「请求失败」，Flask 日志显示 `NameError: name 'threading' is not defined`。

**根因**：Round 21 将 `POST /api/scrape/<id>` 从同步阻塞改为后台线程执行，在路由函数中使用了 `threading.Thread()`，但忘记在 `app.py` 顶部添加 `import threading`。Python 会在运行时抛 `NameError`，Flask 返回 500。

同时 `/api/scrape/all` 路由中 `{s.id: s.session_id for s in sessions.values()}` — `s` 是 `ScrapeSession` 对象，属性是 `school_id` 不是 `id`。

**解决**：
```python
# app.py 顶部
import threading
import json

# /api/scrape/all 修正
'session_ids': {school_id: sess.session_id for school_id, sess in sessions.items()},
```

**教训**：
- ⛔ 每次添加新依赖的模块后，**检查所有路由中使用该模块的地方是否已导入**
- 改完代码后必须 kill 进程重启 Flask（见 7.13、7.50）
- `ScrapeSession` 对象的标识是 `school_id` 和 `session_id`，不是 `id`

### 7.62 ⚠️ `_fetch_html` JS 重定向检测的无限递归 — 第二十二轮致命坑

**现象**：发现向导（自动发现）卡在第一个候选部门探测上，8+ 分钟无进展。后台实际在执行数千次重复的 HTTP 请求到同一个 URL。

**根因**：`news.sjtu.edu.cn/jdyw/index.html` 页面有一个分页 JS 函数：
```javascript
function(currPage) {
    var i = currPage;
    if (i == 1) location.href = 'index.html';
    else location.href = 'index_' + i + '.html';
}
```

`_fetch_html()` Phase 3 的 JS 重定向正则匹配到 `location.href='index.html'`，`urljoin` 解析后指向**同一个 URL**。递归调用 `_fetch_html()` 时 `redirects_followed` 被重置为 0（局部变量），导致同一 URL 被无限循环请求直到 Python 递归栈溢出。

**根本问题**：`redirects_followed` 是局部变量，每次递归调用都重新初始化为 0。如果 JS 重定向目标与原 URL 相同，每次递归都会：
1. 匹配到同一个 `location.href='index.html'`
2. `urljoin` 解析为同一 URL
3. 重新调用 `_fetch_html(target_url)` → 回到步骤 1

**解决**：新增 `_redirect_chain: set | None = None` 参数，在递归调用间传递已访问 URL 集合：
```python
def _fetch_html(url, ..., _redirect_chain: set | None = None):
    if _redirect_chain is None:
        _redirect_chain = set()
    _redirect_chain.add(url.rstrip('/'))
    ...
    normalized_target = target_url.rstrip('/')
    if normalized_target in _redirect_chain:
        logger.debug(f"检测到循环重定向，停止: {url} → {target_url}")
        break
    html = _fetch_html(target_url, ..., _redirect_chain=_redirect_chain)
```

**教训**：
- ⛔ 递归函数中的循环计数器如果是局部变量，在目标 URL 指向自身时会无限循环
- 任何递归调用都应跟踪"已访问"状态，防止循环
- `location.href='index.html'` 在分页 JS 中非常常见（如 `pagination.js`），不应被误判为重定向
- 调试方法：当后台线程长时间无响应时，检查是否有重复 HTTP 请求（netstat/tasklist 看是否有大量连接）

### 7.63 ⚠️ 标准路径探测不应触发浏览器回退 — 第二十二轮致命坑

**现象**：发现向导在 `probing_selectors` 阶段极慢，每个候选部门耗时数分钟。

**根因**：`find_dept_notice_url()` 策略2 对 ~18 条标准路径依次调用 `_fetch_html(test_url)`，默认 `allow_browser_fallback=True`。对于不存在的 URL：
- curl_cffi 请求超时 → 15s
- 触发 Playwright 回退 → `networkidle` 等待 30s
- 合计 45s/路径，18 路径 × 45s = 13.5 分钟/候选

**解决**：
```python
# nav_parser.py 第 429 行
resp_html = _fetch_html(test_url, allow_browser_fallback=False)
```

**教训**：
- 探测路径是否存在只需 curl_cffi 请求（15s 超时足够），**绝不需要浏览器渲染**
- 任何批量 URL 探测场景都应显式传 `allow_browser_fallback=False`
- 所有调用 `_fetch_html` 的地方都应审查是否需要浏览器回退

### 7.64 ⚠️ SJTU 导航解析失败 — 第二十二轮

**现象**：SJTU 首页的 `parse_school_navigation()` 生成的 29 个"分类"中，大部分是新闻文章链接（`news.sjtu.edu.cn/jdyw/...`、`mp.weixin.qq.com/...`），而非院系导航链接。6 个 `type: listing` 的分类全是单篇新闻文章。

**根因**：SJTU 首页结构与 CUPK/CUP/HUST 完全不同。真正的院系列表在 `/xyyl/`（学院一览）页面，但主页导航结构被首页新闻轮播/推荐大量填充。

`enumerate_listing_page()` 从新闻文章页提取的"实体"包括：
- "学院要闻" → `news.sjtu.edu.cn/jdyw/index.html`（新闻列表页，非院系页）
- "fifa.m.tmall.com" → 商业广告链接
- "打印本页"/"关闭窗口" → `javascript:;` 链接

**当前状态**：28 个候选中有少量真实院系（4 个已确认），多数是噪声。用户需要在预览中筛选。

**教训**：
- 不同学校的首页结构差异极大，导航解析的启发式规则需要持续适配
- `enumerate_listing_page` 的过滤逻辑需要增强：长度过滤 (≤20字)、URL 模式过滤、`javascript:` 过滤
- SJTU 类综合大学可能需要专门的处理路径（直接访问 `/xyyl/` 学院一览页）
- 发现向导的结果不应全选确认，必须抽样验证

### 7.65 ⚠️ `url` vs `page_url` 变量名不一致导致 `NameError` — 第二十三轮核心坑

**现象**：部分部门（如重质油全国重点实验室）爬取时报 `NameError: name 'url' is not defined`，导致整个部门崩溃。其他部门虽然有 `try/except` 保护，但签名保存和选择器自愈功能全部失效。

**根因**：`scraper/engine.py` 的 `scrape_department()` 函数中，第 628 行定义的当前页面 URL 变量名为 `page_url`。但在 4 处调用 `save_element_signatures()` 和 `auto_heal_selectors()` 时，传入的参数写成了 `url`（不存在的变量名）：
- 第 714 行：`save_element_signatures(html, department.school_id, url, ...)` — 签名保存
- 第 722 行：`auto_heal_selectors(html, department.school_id, url, ...)` — 自愈（need_probe 分支）
- 第 752 行：`auto_heal_selectors(html, department.school_id, url, ...)` — 自愈（已配置选择器无匹配分支）
- 第 785 行：`save_element_signatures(html, department.school_id, url, ...)` — 第1页选择器生效时签名保存

**后果**：
- 选择器自愈功能彻底失效（`NameError` 被捕获后跳过）
- 签名保存功能彻底失效（Scrapling 无法学习新页面的元素签名）
- 对于未做 `try/except` 保护的调用路径，直接导致部门崩溃

**解决**：全部 4 处 `url` → `page_url`。

**教训**：
- ⛔ 变量重命名时必须全局搜索确认所有引用处
- `url` 和 `page_url` 语义接近但不相同，IDE 不会提示这种错误（都是合法变量名）
- 这类错误在静态检查中不会被发现（Python 是动态类型），只有在运行时触发该代码路径才会暴露

### 7.66 ⚠️ 后台线程中 Flask-SQLAlchemy 对象 detached — 第二十三轮致命坑

**现象**：用户通过 UI 点击「刷新」按钮后，所有学校的抓取都返回 `total=0, new=0`。Flask 日志显示每次抓取都触发了「站点发现」→「创建垃圾部门」→「清理噪声部门」，根本没对已有部门执行实际爬取。但通过命令行直接调用 `scrape_school()` 完全正常。

**根因**：`app.py` 的 `POST /api/scrape/<id>` 和 `POST /api/scrape/all` 路由中：

```python
school = db.session.get(School, school_id)  # 请求线程的 session

def _run():
    app.app_context().push()  # 新线程的新 session
    log = scrape_school(school, ...)  # school 已 detached！
```

Flask-SQLAlchemy 的 `db.session` 绑定到请求线程。后台线程 `_run()` 虽然 push 了新的 app context，但 `school` 对象是在原请求线程的 session 中获取的。在新的 app context 中，`school` 的 session 已断开（detached）。

`scrape_school()` 调用 `school.departments.all()` 时，动态 relationship 返回**空列表**（SQLAlchemy warning: "Instance is detached, dynamic relationship cannot return a correct result"）。随后 `is_bare = not depts` → `True` → 触发站点发现 → 创建垃圾部门 → 清理 → 返回 `(0, 0)`。

**关键日志证据**：
```
SAWarning: Instance <School at 0x...> is detached, dynamic relationship cannot return a correct result.
[中国石油大学（北京）克拉玛依校区] 站点发现完成：创建了 17 个部门
[清理] 已移除 17 个噪声部门
```

**这就是用户每次点「刷新」都抓不到通知的根本原因。** 从第十八轮到第二十二轮，这个 bug 可能一直存在，只是之前大家都在用命令行测试。

**解决**：
```python
def _run():
    app.app_context().push()
    school_obj = db.session.get(School, school_id)  # 在新 session 中重新获取
    log = scrape_school(school_obj, progress_session=session)
```

两个路由（`/api/scrape/<id>` 和 `/api/scrape/all`）都需要同样的修复。

**教训**：
- ⛔ **任何 `threading.Thread` 中使用的 ORM 对象，都必须在自己的 app context 中重新获取**
- `app.app_context().push()` 只解决 context 问题，不解决 session 绑定问题
- Flask-SQLAlchemy 的 session 是线程局部的（scoped_session），不同线程有不同 session
- Detached instance 不会抛异常，而是静默返回错误结果（空列表/过期数据），极难排查
- 命令行测试和 Web 测试的结果不一致时，优先怀疑线程/session 问题
- SQLAlchemy 的 "detached instance" warning 很容易被忽略（只是 warning 不是 error），应该当作 error 对待

# 启动 ngrok 隧道（手机访问）
/d/Jinta/Tools/ngrok/ngrok http 5000 --config /d/Jinta/Tools/ngrok/config/ngrok.yml &

# 清除端口 5000（改代码后重启前必做！）
netstat -ano | grep ":5000 " | grep LISTENING
taskkill -F -PID <PID>  # 对所有列出的 PID 执行

# ---- API 操作（需先登录获取 session cookie）----

# 登录（获取 cookie）
curl -c /tmp/cookies.txt -X POST http://localhost:5000/login -d "password=<密码>"

# 手动触发全量爬取
curl -b /tmp/cookies.txt -X POST http://localhost:5000/api/scrape/1

# 一键已读所有通知
curl -b /tmp/cookies.txt -X POST http://localhost:5000/api/announcements/read-all \
  -H "Content-Type: application/json" -d '{}'

# 为所有缺失摘要的通知生成摘要
curl -b /tmp/cookies.txt -X POST http://localhost:5000/api/summarize \
  -H "Content-Type: application/json" -d '{}'

# 修改访问密码
curl -b /tmp/cookies.txt -X POST http://localhost:5000/api/settings \
  -H "Content-Type: application/json" -d '{"password":"新密码"}'

# 保存/更新 DeepSeek API Key
curl -b /tmp/cookies.txt -X POST http://localhost:5000/api/settings \
  -H "Content-Type: application/json" -d '{"api_key":"sk-xxxxxxxx"}'

# 修改爬取间隔（分钟）
curl -b /tmp/cookies.txt -X POST http://localhost:5000/api/settings \
  -H "Content-Type: application/json" -d '{"interval":"60"}'

# 查看所有学校
curl -b /tmp/cookies.txt http://localhost:5000/api/schools

# 查看某学校所有部门
curl -b /tmp/cookies.txt http://localhost:5000/api/schools/1/departments

# 添加新部门（需确认模板类型！）
curl -b /tmp/cookies.txt -X POST http://localhost:5000/api/departments \
  -H "Content-Type: application/json" \
  -d '{
    "school_id": 1,
    "name": "新部门",
    "list_url": "https://www.cupk.edu.cn/zcms/catalog/XXXXX/pc/index_1.shtml",
    "list_selector": "div.middleArticle__art--art",
    "title_selector": "h3.middleArticle__art--titles",
    "link_selector": "a.middleArticle__art--link",
    "date_selector": "div.middleArticle__dates",
    "content_selector": "div.c-main__right"
  }'

# 删除部门
curl -b /tmp/cookies.txt -X DELETE http://localhost:5000/api/departments/<dept_id>

# 测试 catalog 端点的选择器
python -c "
import requests
from bs4 import BeautifulSoup
url = 'https://www.cupk.edu.cn/zcms/catalog/15250/pc/index_1.shtml'
r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
r.encoding = r.apparent_encoding
soup = BeautifulSoup(r.text, 'lxml')
items = soup.select('div.middleArticle__art--art')
print(f'Found {len(items)} items')
if items:
    title = items[0].select_one('h3.middleArticle__art--titles')
    print(f'Title: {title.get_text(strip=True) if title else \"N/A\"}')
    date = items[0].select_one('div.middleArticle__dates')
    print(f'Date: {date.get_text(strip=True) if date else \"N/A\"}')
"
```

---

## 9. 文件结构速查

```
school-watcher/
├── app.py                     # Flask 主入口：路由 + API + 认证 + 安全头 + 调度器 + 部门树 + DAILY NEWS
├── config.yaml                # 学校配置（启动时自动导入 DB，只增不更新）
├── .gitignore                 # Git 排除规则（data/、.env、DB 等敏感文件）
├── .env                       # 环境变量（SECRET_KEY、APP_PASSWORD 等，自动生成，勿删！）
├── setup.bat                  # 一键环境安装（pip install + 初始化）
├── run.bat                    # 一键启动（chcp 65001 + python app.py + 开浏览器）
├── school-notifier.bat        # 🆕 桌面快捷方式指向的启动脚本（ASCII 名，避免编码问题）
├── icon.svg                   # 🆕 SVG 矢量图标源文件（resvg CLI 渲染用）
├── icon.ico                   # 🆕 多分辨率图标文件（16-256px，PIL 原生 ICO 格式）
├── icon_preview.png           # 🆕 512px 图标预览
├── README.md                  # 面向用户的使用文档
├── HANDOFF.md                 # 本文件：面向开发者的交接文档
├── PRIVACY_LOG.md             # 隐私日志（记录所有秘密位置，已在 .gitignore 中排除）
├── scraper/
│   ├── engine.py              # HTTP 爬虫核心：翻页、增量、选择器探测、站点发现入口
│   ├── site_discovery.py      # 智能站点发现：导航层级解析 + 列表页枚举 + 部门树（第6轮重写）
│   ├── dom_analyzer.py        # 🆕 通用DOM分析引擎：重复块检测、文本密度、导航识别、翻页检测、日期提取
│   ├── list_detector.py       # 🆕 通知列表检测器：profile优先 + DOM分析回退
│   ├── content_extractor.py   # 🆕 通用正文提取器：语义标签 → 文本密度 → body回退
│   ├── nav_parser.py          # 🆕 通用导航解析器：链接密度 + 层次分类
│   ├── pagination_detector.py # 🆕 翻页检测器：HTML预判 + HTTP探测
│   ├── discovery_progress.py  # 🆕 发现进度追踪：SSE实时推送
│   ├── selector_store.py      # 🆕 选择器学习存储：域名→选择器知识库
│   ├── sanitizer.py           # HTML 安全清洗：移除危险标签/事件属性/协议
│   ├── change_detector.py     # 变更检测：内容哈希 + URL 指纹 + 日期解析（4种格式）
│   ├── playwright_fetcher.py   # 🆕 Playwright Chromium 抓取：浏览器单例 + JS检测 + 智能回退
│   ├── selenium_scraper.py    # 🆕 Playwright 全浏览器模式爬虫（Selenium→Playwright重写，处理AJAX翻页）
├── ai/
│   └── summarizer.py          # DeepSeek API 摘要：OpenAI 兼容 SDK，批量/单条
├── database/
│   ├── db.py                  # SQLAlchemy 初始化（Flask-SQLAlchemy）
│   └── models.py              # ORM 模型：School / Department / Announcement / ScrapeLog / AppConfig
├── scheduler/
│   └── jobs.py                # APScheduler 定时爬取 + 摘要生成
├── templates/
│   ├── base.html              # 基础布局：导航栏（含退出登录） + Toast + 汉堡菜单
│   ├── login.html             # 登录页：密码输入卡片 + 忘记密码链接
│   ├── recovery.html          # 🆕 密码恢复页：密保验证 → 重置密码（两阶段，无需登录）
│   ├── index.html             # 首页：学校卡片 + 按学校一键已读 + 未读数
│   ├── school.html            # 通知列表：年月→部门→通知 三级分组 + DAILY NEWS + 侧边栏树
│   ├── announcement.html      # 通知详情：AI 摘要 + 正文（sanitizer 清洗后的 HTML）
│   ├── settings.html          # 设置：API Key + 访问密码 + 密保问题 + 爬取间隔 + 日志
│   └── schools.html           # 学校/部门管理：增删改查模态框 + 部门收展 + 动态渲染
├── static/
│   ├── css/style.css          # 移动优先响应式 + 侧边栏 + 父子部门 + DAILY NEWS + nav-logout
│   └── js/app.js              # 前端交互：Toast、汉堡菜单、折叠、快捷键
└── data/
    └── school_watcher.db      # SQLite 数据库（含 API Key 和密码，勿删勿提交！）

D:\Jinta\Tools\ngrok\
├── ngrok.exe                  # ngrok v3.39.9 主程序
└── config/
    └── ngrok.yml              # auth token（已配置）
```

---

## 10. 为新学校添加配置的完整流程

### 方式 A：UI 添加（推荐，支持智能发现）
1. 打开 `/schools/manage` → 点击「添加学校」
2. 输入学校名称和官网 URL → 保存
3. 回到首页 → 点击该学校的「刷新」按钮
4. **自动流程**：创建默认部门 → 智能发现部门结构 → 探测选择器 → 爬取通知
5. 检查结果：左侧部门栏是否完整 → 通知数是否 > 0

### 方式 B：YAML 配置文件添加（精确控制）
1. F12 找到通知列表的 CSS 选择器
2. 确认是否有 AJAX/catalog 端点
3. `curl` 测试每页 HTML，确定模板类型（A/B/C/D）
4. 在 `config.yaml` 添加学校 + 部门配置
5. 重启应用 → YAML 自动导入
6. 手动触发爬取 → 验证

### 方式 C：混合（智能发现 + 手动微调）
1. 方式 A 自动发现部门
2. 在 `/schools/manage` → 部门管理中编辑不准确的部门
3. 手动微调选择器或 list_url

---

## 11. 认证与安全速查（本轮新增）

### 11.1 密码管理
- **访问密码**：`app_config` 表中 key=`app_password`，或环境变量 `APP_PASSWORD`
- **修改密码**：设置页 → 🔒 访问密码 → 输入新密码 → 保存，立即生效
- **忘记密码**：查启动日志（首次启动会打印），或命令行查询：
  ```bash
  python -c "from app import app, AppConfig; app.app_context().push(); print(AppConfig.get('app_password'))"
  ```

### 11.2 白名单路由
以下路径无需认证：
- `/login`、`/logout`
- `/static/*`（CSS/JS 文件）

其他所有页面和 API 均需登录。

### 11.3 `.env` 文件
```bash
# 自动生成，手动编辑可选
SECRET_KEY=<64位随机hex>    # Flask session 签名密钥
APP_PASSWORD=<你的密码>     # 访问密码（优先级高于数据库）
FLASK_HOST=0.0.0.0          # 如需要局域网/ngrok 访问
```
