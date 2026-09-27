# 全平台视觉与字体统一验收记录

日期：2026-09-26。完成统一开源字体、通知双视图、共用正文排版及学校／账户／管理区重设计。登录双栏构图保留。本机网站已重启并加载新版本。

## 交付内容

- 全站Inter + Noto Sans SC自托管；字体入口、控件继承、字号角色和来源文字归一。
- 桌面并排／专注阅读，窄屏列表／正文分别显示；来源菜单、查询、收藏和归档保留。
- 首页与独立通知页共用正文组件；字体归一仅影响展示副本，历史正文不重写。
- 学校共用页头和导航，个人中心按订阅／安全分区，管理子页完整同壳导航。
- 清理重复字体栈、手机根字号缩小、旧阅读规则、旧账户布局及管理统计冲突样式。
- 兼容旧view=saved/archived；专注阅读view=focus结合mailbox保留邮箱分类，无数据库迁移。

## 自动与浏览器验证

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 相关后端回归 | 100项通过 | 以下9个测试模块 |
| 阅读视图 | 8组通过 | data/ui-check/reading-views/report.json |
| 搜索 | 8组交互＋1组视觉通过 | data/ui-check/inbox-search/report.json |
| 来源菜单 | 11组通过 | tests/check_inbox_source_menu_browser.py及其报告 |
| 学校／账户／管理 | 7组通过，9页跨3个宽度 | data/ui-check/supporting-redesign/report.json |
| 字体／共用阅读器 | 6组通过，四宽度两主题 | data/ui-check/visual-system/report.json |
| 字体完整性 | 146项哈希及本地CSS通过 | scripts/vendor_fonts.py --verify |
| JavaScript与差异检查 | 通过 | node语法检查、git diff --check |

100项测试模块：test_inbox_search_suggestions、test_ux_redesign、test_fragment_routes、test_shared_summaries、test_article_provenance、test_storage_management、test_browser_runtime_routes、test_directory_options、test_source_governance。运行中出现已有摘要测试的SQLAlchemy identity-map警告；未出现测试失败。

阅读验证包括：切视图和对应前后退不发内容请求；保留文章、列表／正文滚动及固定偏好；收藏／归档深链接刷新兼容；Esc先关来源；局部更新保留草稿；迟到请求不能还原过时视图。搜索与目录多选回归保持通过。

实际字体用Edge CDP检查，不只读取CSS声明。登录、正文、目录、账户和管理标题及控件均使用自托管字体；外部网络阻断时可加载，阻断WOFF2时文字与控件仍可见。可见文章标题实测桌面28px、手机24px，正文17px。测量器排除了隐藏的邮箱语义标题，并等待响应式布局完成。

字体142个WOFF2，共10,453,428字节，按需请求。Noto并集30,890码点与所选官方同版原始TTF主cmap完全相等；新增34片补足网页分片缺少的17,255项。官方原件与镜像SHA一致，首末补片重建哈希一致。龘、鱻、喆、𬌗实际使用自托管字体；𠮷不在上游字集中，保留系统回退。17MB原始TTF没有放入运行时静态目录。

## 视觉证据

截图使用隔离验收数据库，校名和通知内容不代表真实官网抓取结果。图片保存在本机data/ui-check下，由测试脚本可重新生成。

- 改造前参考：data/ui-check/inbox-search/1440-light-matches.png、data/ui-check/apple-desktop-account.png。与新图不是完全相同数据状态的像素对照。
- 并排／手机：visual-system/1440-light-reader.png、1200-light-reader.png、1024-dark-reader.png、390-dark-reader.png。
- 专注阅读：visual-system/1440-focus.png；最终展示优先使用此完整截图。
- 固定来源：reading-views/1200-light-reading.png、1200-dark-reading.png，以该目录实际文件名和报告为准。
- 登录：visual-system/1440-light-login.png、1440-dark-login.png。
- 账户和管理：visual-system/1440-account.png、1440-security.png、1440-admin.png；其他页面在supporting-redesign目录。

独立视觉审查最终结论：ship。唯一发现的深色登录按钮对比不足已修复并复核resolved；白字配#0071e3为4.70:1，悬停#0066cc为5.57:1。见visual-system/login-contrast.json。其余已检查截图未发现阻止交付的布局或层级问题。

## 复跑方法

从项目根目录、已安装测试依赖与浏览器的环境运行：

```text
python -m unittest discover -s tests -p test_inbox_search_suggestions.py
python -m unittest discover -s tests -p test_ux_redesign.py
python -m unittest discover -s tests -p test_fragment_routes.py
python -m unittest discover -s tests -p test_shared_summaries.py
python -m unittest discover -s tests -p test_article_provenance.py
python -m unittest discover -s tests -p test_storage_management.py
python -m unittest discover -s tests -p test_browser_runtime_routes.py
python -m unittest discover -s tests -p test_directory_options.py
python -m unittest discover -s tests -p test_source_governance.py
python tests/check_inbox_views_browser.py
python tests/check_inbox_search_browser.py
python tests/check_inbox_source_menu_browser.py
python tests/check_supporting_surfaces_browser.py
python tests/check_visual_system_browser.py
python scripts/vendor_fonts.py --verify
```

浏览器检查使用临时数据库和本机临时端口，不用于清理或更改真实学校数据。当前本机使用Playwright的msedge通道；字体恢复／重建所需fonttools与brotli只属于维护工具，详见字体README，不是应用运行依赖。

## 实测边界

- 实测环境为Windows Edge。1440、1200、1024、390为浏览器响应式视口，不代表各类手机实机已验收。
- 200%采用720×480 CSS视口模拟1440×960显示的布局空间；不写成浏览器真实缩放完整验收。
- 支持页面矩阵主要为1440／1024浅色和390深色，通知正文有四宽度×两主题；不宣称所有页面都跑遍所有组合。
- 未进行macOS、Safari、Linux实机检查；原始字体字集外的字符仍依赖系统回退。
- 本轮不评估官网抓取成功率、数据库迁移或服务器容量，不改变已有通知保存策略。
- 真实本机GET首页、专注地址、存储管理和访问验证均返回200，字体与阅读脚本已接入。
