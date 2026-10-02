# 运行、升级与维护

从 0.2.0 开始，源码与安装版统一为本机桌面应用，没有注册、登录、用户管理或多人网站部署入口。界面只通过桌面启动凭据访问，后台进程由桌面程序统一启停。

## 安装版

从 [GitHub Releases](https://github.com/jintairezhao/school-watcher/releases) 下载适合系统的安装包。Windows、macOS 的安装、更新、组件准备与数据位置见 [桌面版说明](DESKTOP.md)。

## 源码运行

使用 Python 3.14.3，在项目根目录创建虚拟环境：

```sh
python -m venv .venv
```

Windows：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements/browser.txt -r requirements/desktop.txt
.venv\Scripts\python.exe app.py
```

macOS：

```sh
.venv/bin/python -m pip install -r requirements.txt -r requirements/browser.txt -r requirements/desktop.txt
.venv/bin/python app.py
```

Windows 也可依次双击 `scripts/setup.bat` 和 `scripts/run.bat`。所有入口均启动 `desktop/entry.py`，直接进入通知界面和系统管理，无需创建账户或提升管理员。数据库迁移、浏览器准备和后台采集由应用完成。Flask/Gunicorn 不再作为独立网站启动入口。

默认数据位置与安装版一致。使用特定数据目录可运行 `python app.py --data-dir "目录的绝对路径"`。不要让旧版与新版同时操作同一数据库。

## 升级与旧目录迁移

退出应用后备份完整数据目录，再安装新版本或更新源码依赖。应用启动前会执行数据库迁移；已有的来源 ID、通知、订阅、已读和收藏记录保留。

旧源码版曾把数据放在项目 `data/` 下。可以通过 `--data-dir` 指向该目录继续使用，或使用应用的导出/导入及文件位置功能迁移。不要直接覆盖新旧目录中的数据库和密钥。

旧多用户数据库仅保留原有记录用于兼容，采用已保存的桌面所有者；没有该标记时复用原管理员或最早的本地记录。不会把不同历史用户的收藏、已读状态强行合并。

## 完整搬迁与备份恢复

关闭应用后复制整个数据目录，包括数据库、本机密钥、加密配置及相关目录设置。仅导出通知不能代替完整备份。保留原目录副本，恢复后检查订阅、收藏、已读及 AI 服务设置。

应用内增量导入不会因为备份缺少某条通知而删除现有通知。[存储管理](DATA_MANAGEMENT.md) 说明缓存清理、备份保留与文件位置；[AI 配置](AI_CONFIGURATION.md) 说明模型配置和密钥迁移。

## 开发验证与发布

```sh
python -m pip install -r requirements/test.txt
python -m unittest discover -s tests -p "test_*.py"
python tests/smoke/check_migration.py
python tests/smoke/check_minimal_runtime.py
```

测试应使用隔离数据。历史 PostgreSQL 数据迁移保留开发验证能力，但这不构成多人服务器发行支持。打包与 Release 流程见 [桌面版说明](DESKTOP.md#发布新版本)。
