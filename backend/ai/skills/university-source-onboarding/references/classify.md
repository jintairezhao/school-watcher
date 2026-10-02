# 机构与栏目分类
逐一处理输入 candidate_id，每个恰好一个结果。propose 仅表示可提交程序验证；review 表示需补充证据；exclude 只表示不是本次机构/栏目候选，不意味着删除。
归属以官方机构名录和目标页面身份为依据。同域、同名、友情链接、文章提及不能单独证明归属；官网证据支持的跨域学院可以接入。不同校区不能因域名相同合并。
栏目发布者由目标栏目所属机构决定。学院导航链接到研究生院招生页面，形成导航关系，不能把研究生院挂到学院名下。转载文章原作者也不自动成为栏目发布者。
字段约束：kind=channel 时 parent_entity_id 必须为 null，发布机构只填 publisher_entity_id，使用输入 entities 中已有 ID 或本批已提出的 unit 候选编号。未取得发布主体证据时返回 decision=review、publisher_entity_id=null，并列出 needed_evidence；不得返回 propose 却缺少发布者，也不能把部门名称直接当作 ID。部门隶属才使用 parent_entity_id。
名称保留官方措辞。Read、更多、了解、查看详情是动作文字；找不到区域标题返回 null/review。单篇文章、专业介绍、人员名录和登录入口不是通知栏目。
同页多个区域逐一识别，不混用证据。同一 URL 的不同标签页或接口参数可能是不同栏目。材料无法区分区域时 review。
主题与发布者分开。column_scope 需栏目名称/说明证据，sample_only 只代表文章样本。一篇夏令营通知不能将综合通知栏重命名；学生优先不能排除行政科研栏目。
name、parent、publisher、identity 的 evidence 数组分别引用支持该字段的输入 evidence_id。不得创造实体 ID。引用本批 unit 候选需该候选也为 propose；否则 review。关系不得循环。
coverage_gaps 只列本次发现的待查问题，空数组也不证明全校完整。每个 review 指出具体缺失材料。
示例：学院网站快捷链接到研究生院招考栏，发布者应为研究生院；未有目标站点身份时应 review，而非根据入口猜测。
review 时可附 actions 请求读取 observed_urls 内的实际页面。即使链接没有进入本批 candidates，也可按 observed_links 选择下一步；kind_hint 仅供参考。程序会排队读取并在后续页面判断中提供 previous_exploration。优先最能解除当前疑问的目录、部门主页或栏目页面，每批最多 3 个读取操作。
