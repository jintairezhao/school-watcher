# 中国石油大学（北京）克拉玛依校区官网调研与整改计划书

> 调研日期：2026-08-23
> 调研对象：https://www.cupk.edu.cn （中国石油大学（北京）克拉玛依校区）
> 对照项目：school-watcher（学校通知扒取工具）
> 结论：官网已完成**院系重组**（4 学院 → 9 学院），项目部门配置存在**名称过时 + 缺失 6 个学院 + 数据源不统一**三类问题，需整改。

---

## 一、调研结论摘要

1. **院系大重组**：官网原来的「石油学院 / 工学院 / 文理学院 / 工商管理学院·马克思主义学院」4 个学院，已拆分重组为 **9 个学院**（详见下表）。这是本次整改的**核心事实**。
2. **项目选择器机制仍然有效**：项目的 4 种 ZCMS 模板（`c-notice` / `middleNotice` / `middleArticle-art` / `middleArticle-list`）**完全覆盖**官网当前所有列表页（新旧学院的 `tzgg` 页都沿用 `middleArticle` 统一模板）。**无需新增选择器 profile**。
3. **问题集中在「配置数据」而非「代码逻辑」**：整改本质是更新部门名称、补全缺失学院、统一列表 URL 数据源，不涉及爬虫引擎代码改动。
4. **旧学院的 catalog 端点仍有效但语义漂移**：`catalog/15315` 原本是「石油学院」，现在 title 已变成「地球科学与工程学院」；catalog ID 的归属随重组改变，且新拆出的学院**没有** catalog 端点（只有 `tzgg` 路径）。

---

## 二、官网现状调研

### 2.1 院系重组对照表（核心）

从官网「院系设置」页（`/c/2021-06-29/508819.shtml`）爬取到当前 9 个学院，与项目 `config.yaml` 现状对照：

| 项目现状（config.yaml） | 官网现状（2026-08） | 子站 | 通知列表 URL | 列表模板 |
|---|---|---|---|---|
| 石油学院（catalog/15315） | **地球科学与工程学院** | `syxy` | `/syxy/tzgg/` | middleArticle-list |
| ❌ 缺失 | **石油工程学院** | `sgxy` | `/sgxy/tzgg/` | middleArticle-list |
| 工学院（catalog/15250） | **化工与环境学院** | `gxy` | `/gxy/tzgg/` | middleArticle-art |
| ❌ 缺失 | **机电工程学院** | `jdxy` | `/jdxy/tzgg/` | middleArticle-art |
| ❌ 缺失 | **新能源与材料学院** | `xcxy` | `/xcxy/tzgg/` | middleArticle-list |
| ❌ 缺失 | **数理与人工智能学院** | `szxy` | `/szxy/tzgg/` | middleArticle-art |
| 文理学院（catalog/15413） | **人文学院** | `wlxy` | `/wlxy/tzgg/` | middleArticle-art |
| 工商管理学院/马克思主义学院（catalog/17291） | **工商管理学院** | `gsxy` | `/gsxy/tzgg/` | middleArticle-art |
| （同上，合并于一个部门） | **马克思主义学院** | `smxy` | `/smxy/tzgg/` | middleArticle-art |

**拆分关系**：
- 石油学院 → 地球科学与工程学院（保留地质相关）+ 石油工程学院（石油工程/储运）
- 工学院 → 化工与环境学院 + 机电工程学院 + 新能源与材料学院 + 数理与人工智能学院（一拆四）
- 文理学院 → 人文学院（改名，未拆分）
- 工商管理学院/马克思主义学院 → 拆分为工商管理学院 + 马克思主义学院（独立）

### 2.2 其他部门（未变，无需整改）

以下部门配置经逐项验证**仍有效**（详见 2.3），名称与官网一致：

- 校区通知公告（首页 `li.c-notice__half-li`）✅ 12 项
- 教务部 + 6 子分类（`jwb/tzgg` 等，`middleNotice`）✅ 各 8~10 项
- 学生工作与安全保卫部（`catalog/15887`，`middleArticle-art`）✅
- 研究生部 + 4 子分类（`yjsb/tzgg` 等，`middleNotice`）✅ 各 9~10 项
- 创新创业学院（`catalog/17669`，`middleArticle-list`）✅

### 2.3 选择器有效性验证

用项目真实的 `curl_cffi`（`impersonate='chrome'`）+ BeautifulSoup，对 `config.yaml` 全部 15 个部门逐一抓取 `list_url` 并验证 4 个选择器，**全部命中**（list/date 均 > 0）：

| 部门 | list | date | 状态 |
|---|---|---|---|
| 校区通知公告 | 12 | 12 | ✅ |
| 教务部（+6 子分类） | 8~10 | 8~10 | ✅ |
| 学生工作与安全保卫部 | 6 | 6 | ✅ |
| 研究生部（+4 子分类） | 9~10 | 9~10 | ✅ |
| 创新创业学院 | 10 | 10 | ✅ |
| 工商管理学院/马克思主义学院 | 8 | 8 | ✅ |
| 石油学院 / 工学院 / 文理学院 | 6~8 | 6~8 | ✅ |

> 注：这三个院系部门「仍有效」是因为 catalog 端点仍返回 middleArticle 列表，但**名称已过时**（见 2.1），且**漏了拆出的新学院**。

### 2.4 数据源对照（catalog 端点 vs tzgg 路径）

旧学院同时存在两条数据管道，实测**数据完全一致**：

| 学院 | tzgg 路径项数 | catalog 端点项数 | 结论 |
|---|---|---|---|
| syxy 地球科学与工程学院 | 6 | 6（catalog/15315） | 一致 |
| gxy 化工与环境学院 | 6 | 6（catalog/15250） | 一致 |
| wlxy 人文学院 | 8 | 8（catalog/15413） | 一致 |

`tzgg` 路径是 catalog 端点的「列表页」视图，二者同源。**新拆出的学院只有 `tzgg` 路径**（无 catalog 端点）。因此整改应**统一改用 `/{子站}/tzgg/` 路径**。

### 2.5 子站首页的「定制模板」（无需处理）

新学院的**子站首页**（`/syxy/`、`/sgxy/` 等）用了 3 种定制 CSS 模板（`c-shiyouxueyuan-new-module`、`c-wenli-notice-module`、`c-home-main__body-main-content`），把「新闻」和「通知」分成两个区块。但这些是**门户首页**，不是通知归档页——项目的目标是抓「通知公告」，应抓 `/{子站}/tzgg/` 归档页（统一 middleArticle 模板），**不需要**为这些定制模板新增 profile。

---

## 三、项目现有设计评估

### 3.1 合理之处（无需改动）

1. **选择器 profile 体系**：`SELECTOR_PROFILES` 的 4 种 ZCMS 模板恰好覆盖官网全部列表页类型，前瞻性良好。
2. **站点发现机制**：`site_discovery.py` 能识别「院系设置」列表页并枚举学院（本次调研即通过它找到权威院系入口），新学院也能被自动发现。
3. **数据模型**：`Department` 的 `group_name`（导航分组）+ 6 选择器 + `last_scraped_at`（增量抓取）设计合理，支持「部门树归类」展示。
4. **选择器自愈**：`selector_store` 保存元素签名，能应对模板微调。
5. **`_probe_selectors` 自动探测**：空选择器时首次抓取会自动探测匹配 profile 并永久保存，新部门可先用空选择器让引擎自测。

### 3.2 问题清单

| # | 问题 | 严重度 | 影响 |
|---|---|---|---|
| P1 | **院系名称过时**：DB 中「石油学院/工学院/文理学院/工商管理学院与马克思主义学院」已是旧名 | 高 | 用户看到过时的院系名，与官网对不上 |
| P2 | **缺失 6 个学院**：石油工程、机电、新能源材料、数理人工智能、工商管理（独立）、马克思主义（独立） | 高 | 大量学院通知完全抓不到 |
| P3 | **数据源不统一**：旧学院用 catalog 端点（15315 等），新学院只能用 tzgg 路径 | 中 | 配置混乱，catalog ID 语义已漂移，维护困难 |
| P4 | catalog 端点标题与列表含旧名残留（如「关于公布《石油学院2026年…》」） | 低 | 属官网过渡期自身现象，无法靠项目解决 |

---

## 四、整改方案

### 4.1 目标

将学院相关部门从「4 个旧学院 + catalog 端点」改为「9 个新学院 + 统一 tzgg 路径」，其余部门（校区通知、教务部、研究生部、学生工作、创新创业）保持不变。

### 4.2 部门配置更新（9 个学院完整配置）

以下为建议的最终配置（`list_url` 统一为 `tzgg` 路径，选择器沿用既有 middleArticle 两种变体，`content_selector` 统一 `div.c-main__right`）：

**middleArticle-list 变体**（用于 sgxy、xcxy 及原石油学院、创新创业）：
```
list_selector:  li.middleArticle--articleList
title_selector: a.middleArticle__articleList--article
link_selector:  a.middleArticle__articleList--article
date_selector:  span.middleArticle__articleList--date
```

**middleArticle-art 变体**（用于 gxy、jdxy、szxy、wlxy、gsxy、smxy 及原工/文理/学生工作/工商管理）：
```
list_selector:  div.middleArticle__art--art
title_selector: h3.middleArticle__art--titles
link_selector:  a.middleArticle__art--link
date_selector:  div.middleArticle__dates
```

| # | 新部门名 | list_url | 变体 | 对应旧部门处理 |
|---|---|---|---|---|
| 1 | 地球科学与工程学院 | `https://www.cupk.edu.cn/syxy/tzgg/` | list | 由「石油学院」改名 |
| 2 | 石油工程学院 | `https://www.cupk.edu.cn/sgxy/tzgg/` | list | 新增 |
| 3 | 化工与环境学院 | `https://www.cupk.edu.cn/gxy/tzgg/` | art | 由「工学院」改名 |
| 4 | 机电工程学院 | `https://www.cupk.edu.cn/jdxy/tzgg/` | art | 新增 |
| 5 | 新能源与材料学院 | `https://www.cupk.edu.cn/xcxy/tzgg/` | list | 新增 |
| 6 | 数理与人工智能学院 | `https://www.cupk.edu.cn/szxy/tzgg/` | art | 新增 |
| 7 | 人文学院 | `https://www.cupk.edu.cn/wlxy/tzgg/` | art | 由「文理学院」改名 |
| 8 | 工商管理学院 | `https://www.cupk.edu.cn/gsxy/tzgg/` | art | 由「工商管理学院/马克思主义学院」拆分 |
| 9 | 马克思主义学院 | `https://www.cupk.edu.cn/smxy/tzgg/` | art | 由「工商管理学院/马克思主义学院」拆分 |

### 4.3 实施步骤

> ⚠️ 注意：`config.yaml` 导入逻辑是**「只增不更新」**（见 `backend/core/config.py` 的 `load_config_yaml`）。修改已有部门名称/选择器**不会**通过重启生效，必须通过管理界面或 API 完成。

**步骤 1 — 通过 API 更新/新增部门（推荐，避免动 DB）**

- 改名 3 个：石油学院→地球科学与工程学院、工学院→化工与环境学院、文理学院→人文学院（`PUT /api/departments/<id>`）
- 拆分 1 个：工商管理学院/马克思主义学院 → 拆成 2 个（改名旧部门为「工商管理学院」+ 新增「马克思主义学院」）
- 新增 5 个：石油工程学院、机电工程学院、新能源与材料学院、数理与人工智能学院（`POST /api/departments` 或走「🔍 自动发现」）

**步骤 2 — 同步 config.yaml 种子（可选，便于重装）**

将 9 个学院的最终配置写入 `config.yaml`（作为新装机的种子），删除旧的 4 个学院条目。旧条目的历史通知数据保留在 DB 中，不受 config.yaml 影响。

**步骤 3 — 数据迁移策略（关键决策）**

历史通知归属问题需用户确认（见下「待确认」）：改名学院（地球科学/化工/人文）是「原地改名」还是「新建+重抓」？
- **方案 A（原地改名）**：保留原部门 ID，仅改 name + list_url，历史通知继续归属该部门（推荐，省时）
- **方案 B（新建重抓）**：新建 9 个部门，删除旧 4 个，全量重抓（数据最干净，但耗时且丢历史）

**步骤 4 — 验证**

1. 对每个新部门执行「测试选择器」（管理界面按钮），确认 list/title/link/date 命中
2. 触发一次抓取，确认能抓到各学院最新通知
3. 抽查正文内容（`content_selector: div.c-main__right` 是否覆盖新学院详情页）

### 4.4 待用户确认的决策点

1. **历史数据归属**：改名学院用「原地改名」（保留历史）还是「新建重抓」（丢历史）？（推荐原地改名）
2. **是否保留旧 catalog 端点**：`catalog/15315` 等仍有效，但语义已漂移。建议废弃，统一 tzgg 路径。
3. **工商管理/马院拆分**：旧部门「工商管理学院/马克思主义学院」的历史通知如何归属（拆分后归到哪个新部门）？

---

## 五、风险与注意事项

1. **官网仍处于过渡期**：首页导航仍写「石油学院/工学院/文理学院」旧名，子站 title 已是新名，catalog 端点通知标题里新旧名混用（如「关于公布《石油学院2026年…》」）。这是官网自身未同步完，项目只需跟随**子站 title 的新名**即可。
2. **翻页格式**：tzgg 列表页翻页沿用 `index_N.shtml` 格式（`_get_page_url` 的 `catalog` 风格会处理），但需实测确认每个学院的翻页是否一致。
3. **正文选择器**：统一 `div.c-main__right` 在旧学院有效，新学院详情页需抽查确认（引擎有 `content_extractor` 自动回退，风险较低）。
4. **只增不更新的陷阱**：改 `config.yaml` 不会更新已有部门，务必走 API/界面，避免「以为改了实际没生效」。
5. **增量抓取**：`last_scraped_at` 基于部门记录，若用「新建重抓」方案，新部门会全量抓取（首次较慢）。

---

## 附：调研数据来源

- 首页：`https://www.cupk.edu.cn/`（84131 字节）
- 院系设置页：`https://www.cupk.edu.cn/c/2021-06-29/508819.shtml`（权威院系列表）
- 9 个学院子站首页 + `tzgg` 列表页：`/syxy/` `/sgxy/` `/gxy/` `/jdxy/` `/xcxy/` `/szxy/` `/wlxy/` `/gsxy/` `/smxy/`
- 教务部/研究生部列表页：`/jwb/tzgg/` `/yjsb/tzgg/`
- catalog 端点：`/zcms/catalog/{15315,15250,15413,17669,17291,18144,15887}/pc/index_1.shtml`
