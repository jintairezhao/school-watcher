# 桌面版与更新

桌面版复用现有网页与后端，使用系统网页引擎呈现在独立窗口中。关闭应用后停止网站、采集 worker、应用创建的采集浏览器和组件下载；不会关闭用户自己打开的浏览器。

## 下载和安装

[GitHub Releases](https://github.com/jintairezhao/school-watcher/releases) 提供 Windows x64 安装程序及便携 ZIP，以及 Apple 芯片、Intel Mac 两种 DMG。macOS 构建目标为 macOS 15 及以上。Windows 使用 WebView2，缺少该组件时安装程序会运行微软官方引导程序，首次安装需要联网。

首次启动直接进入通知页面，无需注册或登录，可直接打开系统管理。应用菜单可检查更新、查看采集组件、打开数据文件夹或退出。桌面窗口使用每次启动的本机访问凭据，保留写操作保护；服务器部署仍使用原有多用户账号系统。

安装包不再内置完整 Chromium 和独立 headless shell。优先检测可用的 Edge／Chrome，使用独立临时配置，不读取用户浏览器历史、密码或登录状态。没有可用浏览器时，从 Playwright 官方下载源安装匹配版本的完整 Chromium（`--no-shell`）到用户数据目录；同时支持无界面采集和人工访问验证。只有 Safari 的 Mac 也需要下载。首次准备需要联网，下载失败可从页面提示或菜单重新检测，已有通知与设置继续可用。

全新安装不预选学校，也不包含开发者的通知、订阅和凭据。在学校目录订阅后，使用 AI 辅助识别部门与栏目；未配置时从订阅页进入 AI 服务设置，保存目录识别用途后继续。订阅页显示当前步骤、检查页数与重试入口。升级隐藏未使用的旧版预设，保留已订阅或已有通知的学校与数据。

在系统管理 → 抓取任务 → 抓取设置中选择“起始发布年月”，默认本年一月。历史通知每批抓取几页后自动续抓；达到所选日期边界或官网末页才完成，不再把三页当作全部结果。修改年月不删除已有通知。Windows 右上角“…”菜单可检查更新、管理组件和打开数据文件夹。

| 系统 | 数据位置 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\SchoolWatcher` |
| macOS | `~/Library/Application Support/School Watcher` |

安装目录只放程序资源，数据、密钥、浏览器会话和下载的更新留在上述用户目录。更新与卸载不会删除它。数据库升级前沿用现有自动备份逻辑。源码运行时原有 `data/` 不会被桌面版自动挪动；迁移历史数据请使用已有导出/导入或完整备份恢复流程。

## 检查更新

菜单 → 检查更新 → 下载更新 → 安装更新。只查询正式版本，不降级，不静默安装。按当前应用架构选择安装包，下载大小及 SHA-256 均通过后才允许打开安装程序。macOS 仍需在 DMG 窗口中拖动替换应用。

更新源是本仓库的 `/releases/latest`。私有仓库阶段，普通安装者无法查询更新；仓库公开后该功能即可面向公众使用。应用内不嵌入 GitHub 访问令牌。网络不可用、请求限流或校验失败不会影响已安装版本。

## 开发与打包

Windows 和 macOS 必须在相应系统构建。Python 3.14.3：

```sh
python -m pip install -r requirements/build.txt
python desktop/entry.py
```

运行 `python desktop/build.py`。构建不需要下载或收录浏览器。Windows 安装包需要 Inno Setup 6 和微软 WebView2 官方引导程序的本机路径 `WATCHER_WEBVIEW_BOOTSTRAPPER`，使用 `python desktop/build.py --installer`。CI 单独下载 Chromium 用于验收，但不放入安装包。

生成内容只在 `.local/desktop-build`、`.local/desktop-dist`、`.local/desktop-release`。资源收录使用明确目录清单，不收录仓库根目录、`.env`、`data/`、`.local/` 或宣传片。完成后检查最终文件树，拒绝私有数据库和媒体制作文件。

## 发布新版本

1. 修改 `desktop/__init__.py` 的 `VERSION`（例如 `0.2.0`）与 `desktop/RELEASE_NOTES.md`。
2. 将变更合入 `main`。Desktop builds 会完成三个目标的构建、浏览器的有界面／无界面模式和原生窗口启动验收。
3. 所有目标通过后，工作流为尚未发布的版本创建对应标签和 Release，上传三个安装包、便携 ZIP 和统一的 `SHA256SUMS.txt`。已有版本不会被覆盖；也支持主动推送 `v0.2.0` 这样的版本标签触发发布。

不要单独覆盖某个已发布安装包而遗漏校验文件。检查更新依赖文件命名规则，请通过构建脚本生成。

正式广泛分发前建议配置代码签名：Windows 安装包签名与 Apple Developer ID 签名、公证是独立发布步骤。当前工作流没有私钥和公证凭据；未签名构建的系统提示需如实告知下载者。不要通过全局关闭系统安全检查解决安装提示。
