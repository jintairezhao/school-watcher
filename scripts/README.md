# 启动与维护工具

命令示例从项目根目录运行，使用项目的虚拟环境。日常使用只需要安装和启动入口。

## 日常启动

| 入口 | 用途 |
| --- | --- |
| `setup.bat` | Windows 创建环境并安装桌面依赖；迁移由应用启动时执行 |
| `run.bat` | Windows 启动桌面实例 |
| `launch_desktop.py` | 与 `app.py` 相同，调用桌面程序入口 |
| `run_worker.py` | 独立采集与任务进程 |
| `run_browser.py` | 独立浏览器服务 |
| `initialize_runtime.py` | 历史数据初始化工具，不作为网站启动入口 |
| `create-shortcut.ps1` | 按需创建 Windows 桌面快捷方式 |
| `school-notifier.bat` | 兼容已有 Windows 启动入口 |

## 按用途查找工具

| 目录 | 工具与作用 |
| --- | --- |
| `maintenance/` | 数据库迁移、完整备份恢复、目录构建与旧库切换 |
| `sources/` | 来源发现、证据核查、栏目关系修复与离线重新解析 |
| `checks/` | 抓取诊断与显式容量检查 |
| `assets/` | 自托管字体的校验与恢复 |

常见操作：

```sh
python scripts/maintenance/migrate_safely.py
python scripts/maintenance/backup_runtime.py
python scripts/sources/verify_source_inventory.py --help
python scripts/checks/acceptance_load.py --help
python scripts/assets/vendor_fonts.py --verify
```

迁移、恢复和来源修复可能写入数据；先阅读 [部署与维护](../deploy/README.md) 及具体工具的参数说明。真实官网检查会产生网络请求，容量检查应使用隔离环境。运行记录和输出保存在本机数据目录，不提交至仓库。

升级自旧目录布局时，维护命令已移入上述子目录；网站、worker、浏览器和 Windows 日常启动入口保持在本目录。自动化脚本中引用的旧维护路径需一起更新。
