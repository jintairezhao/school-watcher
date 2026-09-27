# 依赖分组

以下命令均在项目根目录、已创建的虚拟环境中运行。各文件引用同目录中的公共清单和版本约束，无需逐个安装。

| 场景 | 安装命令 | 包含内容 |
| --- | --- | --- |
| 基础运行 | `python -m pip install -r requirements.txt` | 网站、SQLite、HTTP 抓取、解析与凭据加密 |
| 本机动态页面抓取 | `python -m pip install -r requirements.txt -r requirements/browser.txt` | 基础功能及 Playwright 浏览器服务 |
| PostgreSQL 服务器 | `python -m pip install -r requirements/server.txt` | 基础、浏览器及 PostgreSQL 驱动 |
| 开发与测试 | `python -m pip install -r requirements/test.txt` | 服务器依赖及测试工具 |

安装浏览器依赖后，再运行 `python -m playwright install chromium` 下载匹配的浏览器。Windows 的 `scripts/setup.bat` 已包含这一步。

- `base.txt` 是基础依赖的实际清单；根目录 `requirements.txt` 只引用它。
- `constraints.txt` 统一限定依赖版本，被基础和浏览器清单引用；它本身不会安装全部依赖。
- AI 使用基础依赖中的 `requests` 调用服务，无需独立的 AI SDK 清单。

版本更新应同时检查约束文件、Python 版本、浏览器二进制和 Docker 配置，并运行相应测试。不要只在某一份清单中单独放宽版本。
