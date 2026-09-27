# 参与贡献

欢迎提交问题、改进建议、官网适配和代码。交流可使用中文或英文。

本项目采用 [School Watcher Noncommercial License 1.0](LICENSE)，仅授权非商业用途，包括基于本项目制作的修改版本。请先确认该许可适合你的使用和贡献场景。

## 报告问题

先搜索已有 [Issues](https://github.com/jintairezhao/school-watcher/issues)。提交时提供：

- 使用的提交或版本、操作系统、Python 版本及部署方式。
- 最少复现步骤、预期结果与实际结果。
- 与问题有关的错误信息；官网适配问题附公开页面 URL、栏目名称和发生时间。

请勿上传数据库、数据备份、`.env`、API 密钥、Cookie、浏览器会话、学生名单或未经脱敏的日志。安全漏洞按 [安全政策](SECURITY.md) 私下报告。

## 开发环境

目录分工见 [README](README.md#开发与验证)，维护命令见 [脚本说明](scripts/README.md)，测试入口见 [测试说明](tests/README.md)。

使用独立克隆及测试数据，不在正在运行的实例上开发或执行迁移。安装 Python 3.14.3 后，在项目根目录执行：

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux: source .venv/bin/activate
python -m pip install -r requirements/test.txt
python -m playwright install chromium
python -m unittest discover -s tests -p "test_*.py"
```

本地测试使用隔离数据库；需要 PostgreSQL 或真实浏览器的测试在环境不可用时会跳过。通过基础测试不等于完成服务器或容量验收。数据库与浏览器部署选项见 [部署说明](deploy/README.md) 和 [浏览器服务说明](backend/browser_service/README.md)。

前端使用 Jinja、原生 JavaScript 和 CSS，无需 Node 构建。字体随项目提供，修改字体时需保留许可并运行 `python scripts/assets/vendor_fonts.py --verify`。

## 提交改动

1. 从 `main` 建立工作分支，一次集中解决一个问题；较大的架构调整先在 Issue 中说明。
2. 修复问题时补充能复现该行为的最小测试，并运行相关测试；涉及迁移时验证旧数据保留。
3. PR 说明用户可见的变化、验证方式和限制。界面改动附使用虚构数据的截图。
4. 更新受影响的使用说明；不要提交个人计划、执行记录、临时抓取网页、数据库或凭据。

统一抓取入口位于 `backend/scraper/acquisition/`；学校差异通过适配配置或独立适配器表达。不要在业务流程中按学校名称不断叠加分支，也不要增加独立重试循环。

测试样本优先使用自己编写的最小 HTML/JSON。复现问题时只保留必要的结构特征；不要复制完整官网、附件、第三方脚本或个人信息。引入第三方资产需要注明来源、版本和许可证，并更新 [第三方声明](THIRD_PARTY_NOTICES.md)。

新增代码需要具有可授权的来源。除事先明确约定并经维护者接受的第三方材料外，你提交给本项目的原创贡献按 [项目许可证](LICENSE) 提供，版权仍由相应权利人持有；不要提交你无权按该许可提供的代码。第三方代码继续遵守原许可，不把它改标成项目原创。
