# 提取配置建议
每个候选返回一个提案，仅使用输入 observed_urls 或证据明确出现的 URL，不猜测 API、分页地址或校名路径。
config 只填写契约允许的 CSS 选择规则和实际地址。只允许静态声明式字段，不生成 Python、JavaScript、shell、脚本下载、函数或任意浏览器操作。
定位目标列表区域，不能为了抓到更多标题改用相邻新闻。链接和标题相对于列表项；日期缺失留空。内容页面未提供证据时正文选择器留空。
outcome=needs_adapter 的可读正文仍是材料，不是成功样本。结合 acquisition_results 中的读取结果，依据正文 DOM 补全 content_selector；程序会重新执行检查。不得把这个内部识别问题交给用户。selector_check 的 matched=0 表示规则在已有页面中无匹配，应立即改正；连续两次提交同一无效规则会停止本轮探索。
用 scope_evidence 声明 container_selector 和 heading_selector：容器应唯一包含目标栏目标题及全部列表项，不能选择整个 html/body 或混入其他栏目。标题规则必须唯一指向官网真实栏目名称，config.name 使用该标题文字。程序将在首读、独立复读及分页上实际执行这些规则；通用模板未认识该结构不是拒绝 AI 判断的理由。出现 column_identity_or_scope_unconfirmed 时补全此声明，不要原样重交。
动态页面需先取得渲染后的目标区域。挑战页/登录页/空壳只能 review，不能拿它们修复正常选择器。
分页只有实际观察到下页地址时填写 pagination_url。该 URL 不是完整分页模板或自动遍历授权。标题、原文身份、范围和翻页终止由程序复测。
配置需要目标名称、列表 URL、列表规则和证据；缺少时先用 explore + actions 补充材料，耗尽支持的路径或预算后才返回 review。提出 CSS 不代表验证通过。
验证出现 publisher_requires_review 时，应读取学校官网中观察到的正式机构目录和部门主页，构成证据链，并填写 publisher_evidence 的 directory_evidence_id、homepage_evidence_id、name；不能只在 reason 中声称“已确认”。同一配置未通过时不能原样重交；根据验证错误读取所缺页面或修正具体字段。
