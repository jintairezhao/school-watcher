# 依赖分组

以下命令均在项目根目录、已创建的虚拟环境中运行。各文件引用同目录中的公共清单和版本约束，无需逐个安装。

| 场景 | 安装命令 | 包含内容 |
| --- | --- | --- |
| 基础组件 | `python -m pip install -r requirements.txt` | SQLite、HTTP 抓取、解析与凭据加密 |
| 本机动态页面抓取 | `python -m pip install -r requirements.txt -r requirements/browser.txt` | 基础功能及 Playwright 浏览器服务 |
| 桌面源码运行 | `python -m pip install -r requirements.txt -r requirements/browser.txt -r requirements/desktop.txt` | 与安装版相同的原生窗口及本机进程管理 |
| 历史 PostgreSQL 数据维护 | `python -m pip install -r requirements/server.txt` | 基础、浏览器及旧数据库驱动；保留文件名兼容历史工具 |
| 开发与测试 | `python -m pip install -r requirements/test.txt` | 数据库兼容依赖及测试工具 |

桌面启动时优先检测 Edge／Chrome，缺少时自动准备采集组件。独立浏览器测试可用 `python -m playwright install chromium` 下载测试用浏览器。

- `base.txt` 是基础依赖的实际清单；根目录 `requirements.txt` 只引用它。
- `constraints.txt` 统一限定依赖版本，被基础和浏览器清单引用；它本身不会安装全部依赖。
- AI 使用基础依赖中的 `requests` 调用服务，无需独立的 AI SDK 清单。

版本更新应同时检查约束文件、Python 版本、浏览器二进制和 Docker 配置，并运行相应测试。不要只在某一份清单中单独放宽版本。
