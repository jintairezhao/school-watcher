# 公开配置

这里存放随程序发布的默认来源配置，不存放 API 密钥、密码、会话或用户数据。

| 文件 | 用途 |
| --- | --- |
| [schools.yaml](schools.yaml) | 学校与初始栏目种子；初始化时增量导入，不覆盖数据库中已有配置 |
| [cms_profiles.yaml](cms_profiles.yaml) | 建站系统的选择器、日期与分页等公共解析规则 |
| [source_profiles.json](source_profiles.json) | 按 URL 和用途匹配的抓取策略、就绪条件及经核实的公开接口适配 |

日常订阅、管理员设置和已发现来源保存在数据库中。修改种子文件不等于修改已经运行的数据库记录。

可通过 `WATCHER_SOURCE_PROFILES` 指定实例自己的来源策略文件；学校种子和 CMS 默认配置随本目录加载。配置升级后重启网站、采集与浏览器进程，避免旧进程继续使用旧配置。

本机环境配置见根目录 [.env.example](../.env.example)，服务器环境配置见 [server.env.example](../deploy/server.env.example)。目录校验数据位于 `backend/resources/source_baselines/`，属于运行和维护工具需要的资源。
