# 第三方组件与内容

第三方代码和素材保留原始版权与许可证，不因项目主许可证而变更授权。本文件说明直接随源码分发的主要资产；安装的依赖和容器系统包还带有各自声明。

根目录 [LICENSE](LICENSE) 的非商业限制适用于按该许可提供的项目代码与文档，不替换或收紧这里列出的第三方许可证，也不授予高校内容的使用权。

## 随源码分发的资产

| 资产 | 上游及版本 | 许可与本地声明 |
| --- | --- | --- |
| `frontend/static/fonts/inter/` | [Inter](https://github.com/rsms/inter)，4.001；字体文件中的版本为 `git-66647c0bb` | SIL OFL 1.1；[原始许可](frontend/static/fonts/inter/OFL.txt) |
| `frontend/static/fonts/noto-sans-sc/` | [Google Fonts / Noto Sans SC](https://github.com/google/fonts/tree/main/ofl/notosanssc)，2.004-H2 | SIL OFL 1.1；[原始许可](frontend/static/fonts/noto-sans-sc/OFL.txt) |
| 模板中的部分 SVG 图标，如主页、指南针、搜索、日月主题切换 | [Feather Icons](https://github.com/feathericons/feather/tree/v4.29.2)，与 4.29.2 对应图形一致；嵌入时调整尺寸、类名或描边 | MIT；Copyright (c) 2013–2023 Cole Bemis；[完整声明](licenses/Feather-MIT.txt) |
| `deploy/seccomp_profile.json` | [Playwright v1.61.0 Docker 配置](https://github.com/microsoft/playwright/blob/v1.61.0/utils/docker/seccomp_profile.json)，JSON 内容未经修改 | Apache-2.0；[完整许可](licenses/Apache-2.0.txt)、[上游 NOTICE](licenses/Playwright-NOTICE.txt) |

字体以 WOFF2 分片提供。Noto Sans SC 的补充分片由上游字体生成，保留 OFL 授权；上游声明的保留字体名为 `Source`。来源地址、版本、校验值和生成方式见 [字体说明](frontend/static/fonts/README.md) 与 [清单](frontend/static/fonts/manifest.json)。字体许可不等同于项目代码许可。

Playwright 的 seccomp 配置基于 Docker 默认配置，并增加允许创建用户命名空间的规则，参见 [Playwright 部署说明](https://playwright.dev/python/docs/docker)。Docker 上游版权声明为 Copyright 2012–2017 Docker, Inc.，来源见 [Moby NOTICE](https://github.com/moby/moby/blob/v20.10.0/NOTICE)。该配置随附 Apache-2.0 许可。

## 安装依赖与容器

桌面安装包还包括 Python 运行时、pywebview、Playwright 与其 Chromium 浏览器。打包时按实际收录的 Python 模块保留依赖发行包中的许可证和版权文件，放在程序资源目录 `third-party-licenses/`；Python 的声明见 `licenses/Python-3.14.3.txt`。Chromium 的原始文件、声明和内置 credits 随浏览器目录一同保留。这些第三方组件继续遵循各自原有许可证。

Python 依赖及固定版本见根目录 `requirements.txt` 和 [requirements/](requirements/README.md)。Playwright 的浏览器二进制由安装步骤另行下载；Docker 构建还会安装 PostgreSQL 客户端、Nginx、noVNC、TigerVNC、Xvfb、Squid 和字体等系统包。它们不统一改授项目主许可证。

正文清洗使用 [nh3](https://github.com/messense/nh3)（MIT；Copyright (c) 2021-present Messense Lv），其 Python 发行包包含基于 Ammonia 的 HTML5 清洗实现。桌面打包流程将实际安装发行包的原始许可证一并收入 `third-party-licenses/`。

重新分发打包程序或容器时，应保留实际安装版本的许可与版权文件；不能仅凭本清单推定完整镜像只受一种许可证约束。修改依赖或引入新的内嵌资源时须同步更新声明。

## 高校目录与网页样本

学校名称、官网地址、栏目与专业等目录事实用于来源识别，相关名称与标识不表示学校对本项目的背书。程序运行时取得的通知、图片和附件仍属于各自权利人，不能根据代码许可推定可任意再分发。

`tests/fixtures/*.html` 中有用于解析回归的真实官网页面或片段，目前没有统一的再分发许可证明。它们不在项目原创代码授权声明的范围内；公开发行前需要逐项确认授权，或替换为独立编写的最小测试样本。测试用途本身不等于取得内容的开源授权。
# Windows installer translation

Simplified Chinese messages by Zhenghan Yang (Kira), vendored from
https://github.com/kira-96/Inno-Setup-Chinese-Simplified-Translation (MIT).
License: `licenses/inno-chinese-translation.txt`.
Translation SHA-256: `bf0751fa176569c6faa2f6e17ed2734617bef325d5cc06eae030fdd0258ee778`.
