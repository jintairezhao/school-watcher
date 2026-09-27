Windows 和 macOS 桌面版：独立应用窗口，内置本机服务与 Chromium，无需安装 Python。

- Windows x64：下载 `windows-x64-setup.exe` 安装；也提供便携 ZIP。
- Apple 芯片 Mac：下载 `macos-arm64.dmg`。
- Intel Mac：下载 `macos-x64.dmg`。macOS 构建目标为 macOS 15 及以上。
- 应用菜单提供“检查更新”，从正式 GitHub Release 下载匹配的安装包，校验 SHA-256 后由用户确认安装。
- 用户数据独立存储，更新和卸载不会删除账号、订阅及阅读记录。退出应用后停止本机采集。

初次启动请注册本机账户，第一个账户是本机管理员。AI 服务仍需自行配置。

本版本暂未配置 Windows 商业代码签名和 Apple Developer ID 公证，系统可能要求确认开发者来源。请核对仓库和校验文件。macOS 安装方式为打开 DMG，将应用拖入 Applications。

仓库公开后，普通用户才能无需授权地检查和下载更新；私有仓库阶段仅用于开发验证。安装包不含开发者的本机数据、凭据或宣传片材料。
