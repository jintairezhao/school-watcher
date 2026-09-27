# 测试入口

| 位置 | 检查内容 | 运行方式 |
| --- | --- | --- |
| `test_*.py` | 抓取、来源、任务、权限、页面与数据一致性的自动回归 | `python -m unittest discover -s tests -p "test_*.py"` |
| `smoke/` | 旧库迁移、可选依赖缺失时的基础运行 | 单独运行对应检查脚本 |
| `browser/` | 真实浏览器中的页面操作、响应式布局与交互 | 安装 Playwright 及浏览器后，按需运行对应脚本 |
| `fixtures/` | 解析测试样本 | 由测试读取，通常不单独运行 |

在独立开发目录、虚拟环境中执行：

```sh
python -m pip install -r requirements/test.txt
python -m playwright install chromium
python -m unittest discover -s tests -p "test_*.py"
python tests/smoke/check_migration.py
python tests/smoke/check_minimal_runtime.py
```

浏览器检查示例：

```sh
python tests/browser/check_inbox_filter_memory_browser.py
```

部分界面检查使用本机 Edge 或允许以 `PLAYWRIGHT_CHANNEL` 选择浏览器；运行前查看对应脚本。浏览器截图及报告写入 `data/ui-check/`，不发布到源码仓库。

公开的界面回归使用独立测试数据，不要求开发者个人数据库中存在特定学校、栏目或通知编号。依赖本机真实数据的人工验收脚本和历史截图仅保存在被忽略的本机目录中。

PostgreSQL 用例需要隔离测试数据库；环境缺失时跳过不代表验收通过。CI 在 [Runtime acceptance](../.github/workflows/runtime.yml) 中配置了 Windows 与 Linux/PostgreSQL 检查。

新增回归通常放入 `test_*.py`；需要真实界面时放入 `browser/`。样本优先使用自行编写的最小 HTML/JSON，现有官网样本的发布限制见 [第三方声明](../THIRD_PARTY_NOTICES.md)。
