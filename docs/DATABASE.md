# 数据库架构说明

本项目使用 **SQLite** 单文件数据库，通过 **Flask-SQLAlchemy** 进行 ORM 映射，并引入 **Flask-Migrate（Alembic）** 管理 schema 迁移。

- 数据库文件：`data/school_watcher.db`
- 连接串来源：`backend/config.py` 的 `get_database_uri()`（可用环境变量 `DATABASE_URL` 覆盖，默认指向 `data/school_watcher.db`）
- ORM 模型：`backend/database/models.py`
- 迁移目录：`migrations/`（初始基线 `0fc208134b8a`）

---

## 数据表总览

| 表名 | 用途 | 关键关系 |
|------|------|----------|
| `schools` | 学校 | 1 → N `departments`、1 → N `announcements` |
| `departments` | 部门/栏目（含爬取选择器配置） | 1 → N `announcements` |
| `announcements` | 通知/公告（核心业务数据） | 归属 `schools` + `departments` |
| `scrape_logs` | 爬取任务日志 | 归属 `schools`（可空） |
| `app_config` | 应用配置键值对 | 无 |

---

## 1. `schools` — 学校

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | Integer PK | 自增主键 |
| `name` | String(200) | 学校名称 |
| `url` | String(500) | 学校官网 URL |
| `enabled` | Boolean | 是否启用（关闭则定时任务跳过） |
| `config` | Text | 爬取配置 JSON（备用扩展字段） |
| `created_at` | DateTime | 创建时间 |

关系：`departments`（级联删除）、`announcements`（级联删除）。

---

## 2. `departments` — 部门 / 栏目

每个部门对应学校官网上的一个通知栏目，保存该栏目的抓取选择器。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | Integer PK | 自增主键 |
| `school_id` | Integer FK | 归属学校（`schools.id`） |
| `name` | String(200) | 部门名称 |
| `list_url` | String(1000) | 通知列表页 URL |
| `list_selector` | String(500) | 列表项 CSS 选择器 |
| `title_selector` | String(500) | 标题 CSS 选择器 |
| `link_selector` | String(500) | 链接 CSS 选择器 |
| `date_selector` | String(500) | 日期 CSS 选择器 |
| `content_selector` | String(500) | 正文内容 CSS 选择器 |
| `group_name` | String(200) | 导航分组名（如「院系设置」「科学研究」） |
| `last_scraped_at` | DateTime | 上次完整抓取完成时间（用于增量抓取） |

关系：`announcements`（级联删除）。

---

## 3. `announcements` — 通知 / 公告

核心业务表，保存抓取到的每条通知及其 AI 摘要。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | Integer PK | 自增主键 |
| `school_id` | Integer FK | 归属学校 |
| `department_id` | Integer FK | 归属部门 |
| `title` | String(500) | 标题 |
| `url` | String(2000) | 原文链接 |
| `content_html` | Text | 正文 HTML |
| `content_text` | Text | 正文纯文本 |
| `summary` | Text | AI 摘要（DeepSeek 生成） |
| `published_at` | DateTime | 发布时间 |
| `content_hash` | String(64) | 内容哈希（用于变更检测去重） |
| `is_read` | Boolean | 是否已读 |
| `is_updated` | Boolean | 是否被更新过 |
| `created_at` | DateTime | 入库时间 |

索引：

- `idx_announcement_hash`（`content_hash`）— 去重/变更检测
- `idx_announcement_url`（`url`）
- `idx_announcement_school_dept`（`school_id`, `department_id`）— 按学校/部门查询
- `idx_announcement_published`（`published_at`）— 按时间排序

---

## 4. `scrape_logs` — 爬取日志

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | Integer PK | 自增主键 |
| `school_id` | Integer FK | 归属学校（可空） |
| `started_at` | DateTime | 开始时间 |
| `finished_at` | DateTime | 结束时间 |
| `new_count` | Integer | 新增通知数 |
| `total_count` | Integer | 检查总数 |
| `status` | String(20) | `running` / `success` / `failed` |
| `error_message` | Text | 错误信息 |

---

## 5. `app_config` — 应用配置键值对

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | Integer PK | 自增主键 |
| `key` | String(100) UNIQUE | 配置键 |
| `value` | Text | 配置值 |

由 `AppConfig.get(key, default)` / `AppConfig.set(key, value)` 静态方法读写，存储密码、密保、API Key、抓取间隔等。

---

## 迁移（Flask-Migrate）工作流

> 日常开发中，**不要**手动改 `.db` 文件或调用 `db.create_all()`——schema 变更一律走迁移。

```bash
# 修改 models.py 后，自动生成迁移脚本
python -m flask --app app db migrate -m "描述这次变更"

# 应用迁移到本地数据库
python -m flask --app app db upgrade

# 查看当前版本
python -m flask --app app db current

# 回退一步 / 回退到指定版本
python -m flask --app app db downgrade
```

说明：

- 初始基线 `0fc208134b8a` 对应现有 5 张表，已 `stamp` 到当前数据库（只写入 `alembic_version` 版本号，未改动既有数据）。
- 全新环境安装时，`setup.bat` 会执行 `flask --app app db upgrade` 自动建表。
- 生成迁移脚本时如需在「空库」上对比（避免对已有数据误判为无变更），可临时指定环境变量：
  `DATABASE_URL=sqlite:///<临时空库路径> python -m flask --app app db migrate -m "..."`。
