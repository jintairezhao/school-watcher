# 📚 项目文档导航

最新实施：[公共目录与实例 AI 实施记录](ONBOARDING_AI_IMPLEMENTATION.md)。

界面验收：[全平台视觉与字体统一验收记录](acceptance/VISUAL_SYSTEM_2026-09-26.md)（2026-09-26）。

> 本文件是 `docs/` 目录的索引，用于快速定位每一份文档的用途。

## 本轮最新实施方案

[高校公共目录、项目 Skill 与通知摘要实施方案](UNIVERSITY_ONBOARDING_AI_PLAN.md)（2026-09-24，待实施）：整合全名单接入、两个项目 Skill、实例级 API 配置及公共摘要共享。线上由管理员配置 API，本地由部署者配置；不设计线上账号自带 API。

## 文档目录树

```
school-watcher/
├── README.md               ← 项目入口：简介、快速开始、API、FAQ
├── docs/
│   ├── README.md           ← 本文件：文档导航索引
│   ├── DATABASE.md         ← 架构：数据库表结构 + 迁移工作流
│   ├── HANDOFF.md          ← 交接：项目唯一真相源（规则/状态/踩坑/历史）
│   └── PRIVACY_LOG.md      ← 运维·安全：隐私/秘密位置清单（敏感，勿外传）
│       PRIVACY_LOG.docx    ← 同上（Word 版，内容一致）
└── migrations/README       ← Flask-Migrate 自动生成（占位，可忽略）
```

## 文档分类

### 📖 入门
| 文档 | 说明 |
|------|------|
| [../README.md](../README.md) | 项目简介、功能特点、技术栈、快速开始、配置说明、API 列表、FAQ |

### 🏗️ 架构
| 文档 | 说明 |
|------|------|
| [DATABASE.md](DATABASE.md) | 数据库 5 张表结构、字段说明、索引、Flask-Migrate 迁移工作流 |

### 🤝 交接
| 文档 | 说明 |
|------|------|
| [HANDOFF.md](HANDOFF.md) | **项目唯一真相源**。含硬性规则、项目概述、当前状态、下一步计划、23 轮历史变更、踩坑记录、速查命令 |

### 🔐 运维·安全
| 文档 | 说明 |
|------|------|
| [PRIVACY_LOG.md](PRIVACY_LOG.md) | 隐私/秘密文件位置清单、安装日志。**含敏感信息位置，勿上传 GitHub / 截图外发**（已在 `.gitignore` 中排除） |

## 阅读顺序建议

- **新会话 / 新协作者**：先读 [../README.md](../README.md) 了解全貌 → 再读 [HANDOFF.md](HANDOFF.md)（项目唯一真相源）
- **改数据库结构**：读 [DATABASE.md](DATABASE.md)
- **处理隐私 / 迁移环境**：读 [PRIVACY_LOG.md](PRIVACY_LOG.md)
