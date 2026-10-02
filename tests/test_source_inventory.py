"""Regression checks for completeness accounting and explicit source relationships."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.source_inventory import Inventory, canonical_url
from backend.scraper.discovery.structure import extract_structure, health_from_evidence
from backend.scraper.discovery.inventory_crawler import crawl_site

ROOT_URL = 'https://www.example.edu.cn/'


class StructureTests(unittest.TestCase):
    def test_tsinghua_non_entity_colleges_require_the_official_legend(self):
        html = '<div><h4><a>* 理学院 *</a></h4><ul><li><a href="/math/">数学科学系</a></li></ul></div>'
        url = 'https://www.tsinghua.edu.cn/yxsz.htm'
        without = extract_structure(html, url, 'https://www.tsinghua.edu.cn/')
        self.assertEqual(next(n for n in without['nodes'] if n['name'] == '* 理学院 *')['relation'], 'directory_entry')
        with_legend = extract_structure(html + '<p>注：* 为非实体学院</p>', url, 'https://www.tsinghua.edu.cn/')
        college = next(n for n in with_legend['nodes'] if n['name'] == '* 理学院 *')
        self.assertEqual(college['relation'], 'non_entity_directory_entry')
        self.assertEqual(next(n for n in with_legend['nodes'] if n['name'] == '数学科学系')['parent'], college['key'])

    def test_official_directory_views_preserve_missing_units_and_different_links(self):
        import json
        html = ('<div class="yxszCon"><div class="setL"><div><h4><a href="/new/">工程学院</a></h4>'
                '<ul><li><a>机械工程系</a></li></ul></div><div><h4><a>语言教学中心</a></h4></div></div></div>'
                '<div class="yxszConMobile"><div class="MCon"><h4><a href="/new/">工程学院</a></h4>'
                '<ul><li><a>机械工程系</a></li></ul></div></div>'
                '<div class="orgaCon"><dl><dd><h3><a href="/old/">工程学院</a></h3>'
                '<p><a>机械工程系</a></p></dd></dl></div>')
        found = extract_structure(html, 'https://www.tsinghua.edu.cn/yxsz.htm', 'https://www.tsinghua.edu.cn/')
        rows = json.loads(next(n.split(':', 1)[1] for n in found['notes'] if n.startswith('directory_variant_differences:')))
        self.assertEqual({r['name'] for r in rows}, {'工程学院', '语言教学中心'})
        college = next(r for r in rows if r['name'] == '工程学院')
        self.assertEqual({u for v in college['versions'] for u in v['urls']},
                         {'https://www.tsinghua.edu.cn/new/', 'https://www.tsinghua.edu.cn/old/'})
        missing = next(r for r in rows if r['name'] == '语言教学中心')
        self.assertEqual([v['present'] for v in missing['versions']], [True, False, False])
        self.assertIn('机械工程系', {n['name'] for n in found['nodes']})

    def test_a_news_topic_link_to_a_unit_home_is_not_a_confirmed_navigation_column(self):
        html = '<main><a href="/another-college/">2025年自治区重点实验室学术委员会议</a></main><nav><a href="/research/">科学研究</a></nav>'
        found = extract_structure(html, ROOT_URL, ROOT_URL, 'unit', '工程学院')
        story = next(n for n in found['nodes'] if n['url'] == ROOT_URL + 'another-college/')
        self.assertEqual(story['relation'], 'linked_navigation_unverified')
        self.assertEqual(next(n for n in found['nodes'] if n['url'] == ROOT_URL + 'research/')['relation'], 'navigation_entry')

    def test_cms_article_addresses_do_not_become_units_or_columns(self):
        urls = ['/info.jsp?urltype=news.NewsContentUrl&wbtreeid=1&wbnewsid=1234',
                '/news-show-13668.html', '/2026/0616/c10533a200982/page.htm',
                'https://mp.weixin.qq.com/s/exampleArticle']
        html = ''.join('<a href="' + url + '">关于开展学术活动的通知 工程学院</a>' for url in urls)
        found = extract_structure(html, ROOT_URL, ROOT_URL)
        self.assertEqual(len(found['links']), 4)
        self.assertTrue(all(l['decision'] == 'article_reference' for l in found['links']))
        self.assertEqual(len(found['nodes']), 1)
        found = extract_structure('<a href="' + urls[0] + '">工程学院</a>', ROOT_URL, ROOT_URL, 'directory')
        self.assertEqual(found['links'][0]['decision'], 'follow')

    def test_no_arbitrary_unit_limit(self):
        html = '<main><ul>' + ''.join(f'<li><a href="/college/{i}/">第{i}学院</a></li>' for i in range(125)) + '</ul></main>'
        found = extract_structure(html, ROOT_URL + 'units', ROOT_URL, 'directory', '院系设置')
        units = [x for x in found['links'] if x['kind'] == 'unit']
        self.assertEqual(len(units), 125)

    def test_public_services_are_real_sources(self):
        names = ['图书馆', '人才招聘', '招标采购', '本科生招生', '研究生招生', '信息公开', '后勤保障部', '就业信息']
        html = ''.join(f'<a href="/site{i}/">{name}</a>' for i, name in enumerate(names))
        found = extract_structure(html, ROOT_URL, ROOT_URL, page_label='某大学')
        self.assertEqual({l['label'] for l in found['links']}, set(names))
        self.assertTrue(all(l['decision'] == 'follow' for l in found['links']))

    def test_news_titles_mentioning_units_or_directories_are_not_structural_nodes(self):
        html = ('<main><a href="/info/1/10001.htm">学术讲座活动圆满举行 机电工程学院</a>'
                '<a href="/info/1/10002.htm">某研究院博士后招聘启事：该院为校内实体科研机构</a></main>')
        found = extract_structure(html, ROOT_URL, ROOT_URL, page_label='某大学')
        self.assertEqual(len(found['links']), 2)
        self.assertTrue(all(l['decision'] == 'article_reference' for l in found['links']))
        self.assertEqual([n['name'] for n in found['nodes']], ['某大学'])

    def test_document_shaped_official_directories_and_unit_introductions_are_retained(self):
        found = extract_structure('<a href="/info/1/10001.htm">院系设置</a>', ROOT_URL, ROOT_URL)
        self.assertEqual(found['links'][0]['decision'], 'follow')
        html = '<nav><div><a>组织机构</a><ul><li><a href="/info/1/10002.htm">油气储运工程系</a></li></ul></div></nav>'
        found = extract_structure(html, ROOT_URL + 'college/', ROOT_URL, 'unit', '石油工程学院')
        self.assertEqual(next(l for l in found['links'] if l['label'] == '油气储运工程系')['decision'], 'follow')
        found = extract_structure('<a href="/info/1/10003.htm">物理学院</a>', ROOT_URL + 'units/', ROOT_URL, 'directory', '院系设置')
        self.assertEqual(found['links'][0]['decision'], 'follow')

    def test_nested_units_have_explicit_parent_identity(self):
        html = '<ul><li><a href="/eng/">工程学院</a><ul><li><a href="/eng/lab/">智能实验室</a></li></ul></li></ul>'
        found = extract_structure(html, ROOT_URL + 'units', ROOT_URL, 'directory', '院系设置')
        parent = next(n for n in found['nodes'] if n['name'] == '工程学院' and n['kind'] == 'unit')
        child = next(n for n in found['nodes'] if n['name'] == '智能实验室')
        self.assertEqual(child['parent'], parent['key'])

    def test_hyphens_do_not_create_fictional_parents(self):
        found = extract_structure('<a href="/lab/">材料-能源研究中心</a>', ROOT_URL, ROOT_URL, page_label='某大学')
        self.assertIn('材料-能源研究中心', [n['name'] for n in found['nodes']])
        self.assertNotIn('材料', [n['name'] for n in found['nodes']])

    def test_table_columns_define_parent_child_and_exclude_majors(self):
        html = '<table><tr><th>学 院</th><th>系</th><th>本科生专业</th></tr><tr><td><a href="/eng/">工程学院</a></td><td><a href="/eng/me/">机械系</a><br>自动化系</td><td>机械工程</td></tr></table>'
        found = extract_structure(html, ROOT_URL + 'units', ROOT_URL, 'directory', '院系设置')
        parent = next(n for n in found['nodes'] if n['name'] == '工程学院')
        children = [n for n in found['nodes'] if n['relation'] == 'sub_unit']
        self.assertEqual({c['name'] for c in children}, {'机械系', '自动化系'})
        self.assertTrue(all(c['parent'] == parent['key'] for c in children))
        self.assertNotIn('机械工程', [n['name'] for n in found['nodes']])
        self.assertNotIn('学院', [n['name'] for n in found['nodes']])

    def test_responsive_copies_merge_unlinked_children_but_keep_evidence(self):
        block = '<h4><a href="/architecture/">建筑学院</a></h4><ul><li><a>建筑系</a></li><li><a>城市规划系</a></li></ul>'
        html = '<main>' + ''.join('<section>' + block + '</section>' for _ in range(3)) + '</main>'
        found = extract_structure(html, ROOT_URL + 'units', ROOT_URL, 'directory', '院系设置')
        children = [n for n in found['nodes'] if n['relation'] == 'nested_directory_entry']
        self.assertEqual(len({n['key'] for n in children}), 2)
        self.assertEqual(len({n['locator'] for n in children}), 6)

    def test_same_unlinked_name_in_different_units_stays_separate(self):
        html = ''.join('<h4><a>' + name + '</a></h4><ul><li><a>教学中心</a></li></ul>'
                       for name in ('建筑学院', '工程学院'))
        found = extract_structure(html, ROOT_URL + 'units', ROOT_URL, 'directory', '院系设置')
        children = [n for n in found['nodes'] if n['relation'] == 'nested_directory_entry']
        self.assertEqual(len({n['key'] for n in children}), 2)

    def test_mobile_copy_without_url_can_match_one_explicit_sibling_identity(self):
        html = '<section><h4><a href="/eng/">工程学院</a></h4><ul><li><a href="/civil/">土木工程系</a></li></ul></section><section><h4><a href="/eng/">工程学院</a></h4><p><a>土木工程系</a></p></section>'
        found = extract_structure(html, ROOT_URL + 'units', ROOT_URL, 'directory', '院系设置')
        copies = [n for n in found['nodes'] if n['name'] == '土木工程系']
        self.assertEqual(len({n['key'] for n in copies}), 1)
        self.assertEqual({n['url'] for n in copies}, {'', ROOT_URL + 'civil/'})

    def test_split_chinese_name_and_pku_directory_label(self):
        html = '<a href="/departments/">学部与院系</a><a href="/marx/">马 <span>克思主义系</span></a>'
        found = extract_structure(html, ROOT_URL, ROOT_URL)
        self.assertEqual(found['links'][0]['kind'], 'directory')
        self.assertEqual(found['links'][1]['label'], '马克思主义系')

    def test_pku_visual_heading_adapter_preserves_group_vs_unit(self):
        html = '<div class="text"><div class="fz30"><a href="https://fs.pku.edu.cn/">理学部</a></div><div class="p links"><a href="https://math.pku.edu.cn/">数学科学学院</a></div></div><div class="text"><div class="fz30"><a>跨学科类</a></div><div class="p links"><a href="https://yuanpei.pku.edu.cn/">元培学院</a></div></div>'
        found = extract_structure(html, 'https://www.pku.edu.cn/department.html', 'https://www.pku.edu.cn/', 'unit', '学部与院系')
        parents = {n['name']: n for n in found['nodes'] if n['name'] in ('理学部', '跨学科类')}
        math = next(n for n in found['nodes'] if n['name'] == '数学科学学院')
        yuanpei = next(n for n in found['nodes'] if n['name'] == '元培学院')
        self.assertEqual(math['parent'], parents['理学部']['key'])
        self.assertEqual(yuanpei['parent'], parents['跨学科类']['key'])
        self.assertEqual(parents['跨学科类']['kind'], 'group')

    def test_whu_wrapped_heading_keeps_each_faculty_and_cross_discipline_group(self):
        html = ('<section><div class="top flex"><h4>理学部</h4></div><div class="bottom"><ul class="list21 flex">'
                '<li><a href="https://maths.whu.edu.cn/">数学与统计学院</a></li></ul></div></section>'
                '<section><div class="top flex"><h4>跨学科类</h4></div><ul class="list21 flex">'
                '<li><a href="https://hyxt.whu.edu.cn/">弘毅学堂</a></li></ul></section>')
        found = extract_structure(html, 'https://www.whu.edu.cn/jgsz/yxsz.htm', 'https://www.whu.edu.cn/', 'directory', '院系设置')
        by_name = {n['name']: n for n in found['nodes']}
        self.assertEqual(by_name['数学与统计学院']['parent'], by_name['理学部']['key'])
        self.assertEqual(by_name['弘毅学堂']['parent'], by_name['跨学科类']['key'])
        self.assertEqual(by_name['跨学科类']['kind'], 'group')

    def test_ustc_row_styles_preserve_three_levels_shared_urls_and_unlinked_units(self):
        def row(name, css='line01', bold=True, url=None):
            anchor = '<a' + (' href="' + url + '"' if url else '') + ' style="font-weight:' + ('bold' if bold else 'normal') + '">' + name + '</a>'
            return '<tr><td class="' + css + '"' + (' colspan="2"' if css == 'line01' else '') + '>' + anchor + '</td></tr>'
        html = '<div class="wp_articlecontent"><table>' + ''.join([
            row('学部/学院'), row('信息与智能学部', url='https://iid.ustc.edu.cn/'),
            row('信息科学技术学院', 'line02', True, 'https://sist.ustc.edu.cn/'),
            row('自动化系', 'line02', False, 'https://sist.ustc.edu.cn/'),
            row('计算机科学与技术学院', 'line02', True, 'https://cs.ustc.edu.cn/'),
            row('公共教学部'), row('艺术教学中心', 'line02', False, 'https://arts.ustc.edu.cn/'),
            row('国家级科研平台'), row('国家高性能计算中心（合肥）')]) + '</table></div>'
        found = extract_structure(html, 'https://www.ustc.edu.cn/yxjs.htm', 'https://www.ustc.edu.cn/', 'directory', '院系介绍')
        by_name = {n['name']: n for n in found['nodes']}
        self.assertEqual(by_name['信息科学技术学院']['parent'], by_name['信息与智能学部']['key'])
        self.assertEqual(by_name['自动化系']['parent'], by_name['信息科学技术学院']['key'])
        self.assertNotEqual(by_name['自动化系']['key'], by_name['信息科学技术学院']['key'])
        self.assertEqual(by_name['计算机科学与技术学院']['parent'], by_name['信息与智能学部']['key'])
        self.assertEqual(by_name['艺术教学中心']['parent'], by_name['公共教学部']['key'])
        self.assertEqual(by_name['公共教学部']['url'], '')
        self.assertEqual(by_name['国家高性能计算中心（合肥）']['parent'], by_name['国家级科研平台']['key'])
        self.assertEqual(by_name['国家级科研平台']['kind'], 'group')

    def test_directory_footer_links_do_not_assert_organizational_membership(self):
        found = extract_structure('<footer><a href="/partner/">合作学院</a></footer>', ROOT_URL + 'units', ROOT_URL, 'directory', '院系设置')
        unit = next(n for n in found['nodes'] if n['name'] == '合作学院')
        self.assertEqual(unit['relation'], 'navigation_entry')

    def test_attached_units_are_distinct_from_sub_departments(self):
        html = '<table><tr><th>单位名称</th><th>挂靠单位</th><th>备注</th></tr><tr><td><a href="/office/">综合办公室</a></td><td>督查办公室<br>校友工作办公室</td><td>无</td></tr></table>'
        found = extract_structure(html, ROOT_URL + 'org', ROOT_URL, 'directory', '组织机构')
        children = [n for n in found['nodes'] if n['relation'] == 'attached_unit']
        self.assertEqual({c['name'] for c in children}, {'督查办公室', '校友工作办公室'})

    def test_table_unit_cells_use_paragraphs_not_concatenated_or_partial_names(self):
        html = '<table><tr><th>单位名称</th><th>挂靠单位</th></tr><tr><td>党群工作部</td><td><p>机关<span>党委</span></p><p>工会</p><p>新闻中心</p></td></tr></table>'
        found = extract_structure(html, ROOT_URL + 'org', ROOT_URL, 'directory', '组织机构')
        names = [n['name'] for n in found['nodes'] if n['relation'] != 'page_identity']
        self.assertCountEqual(names, ['党群工作部', '机关党委', '工会', '新闻中心'])
        self.assertNotIn('机关党委工会新闻中心', [l['label'] for l in found['links']])

    def test_shared_table_row_does_not_choose_one_of_multiple_units_as_parent(self):
        html = '<table><tr><th>单位名称</th><th>挂靠单位</th></tr><tr><td><p><a href="/hr/">组织人事部</a></p><p><a href="/hr/">党委教师工作部</a></p></td><td>党校办公室<br>师资办公室</td></tr></table>'
        found = extract_structure(html, ROOT_URL + 'org', ROOT_URL, 'directory', '组织机构')
        group = next(n for n in found['nodes'] if n['relation'] == 'shared_table_row')
        self.assertEqual(group['kind'], 'group')
        units = [n for n in found['nodes'] if n['relation'] == 'same_table_row']
        self.assertEqual({n['name'] for n in units}, {'组织人事部', '党委教师工作部'})
        attached = [n for n in found['nodes'] if n['relation'] == 'attached_unit']
        self.assertTrue(all(n['parent'] == group['key'] for n in units + attached))

    def test_table_remark_links_keep_the_row_context_without_asserting_subordination(self):
        html = '<table><tr><th>单位名称</th><th>挂靠单位</th><th>备注</th></tr><tr><td rowspan="2"><a href="/hr/">组织人事部</a></td><td>党校办公室</td><td><a href="/jobs/">人才招聘</a></td></tr><tr><td>师资办公室</td><td><a href="/hire/">招聘说明</a></td></tr></table>'
        found = extract_structure(html, ROOT_URL + 'org', ROOT_URL, 'directory', '组织机构')
        unit = next(n for n in found['nodes'] if n['name'] == '组织人事部')
        refs = [n for n in found['nodes'] if n['relation'] == 'table_reference']
        self.assertEqual({n['name'] for n in refs}, {'人才招聘', '招聘说明'})
        self.assertTrue(all(n['parent'] == unit['key'] for n in refs))
        self.assertEqual(len([n for n in found['nodes'] if n['relation'] == 'attached_unit']), 2)

    def test_unlinked_heading_parent_preserves_real_directory_hierarchy(self):
        html = '<div><h4><a>* 理学院 * </a></h4><ul><li><a href="/math/">数学科学系</a></li><li><a href="/phys/">物理系</a></li></ul></div>'
        found = extract_structure(html, ROOT_URL + 'units', ROOT_URL, 'directory', '院系设置')
        parent = next(n for n in found['nodes'] if n['name'] == '* 理学院 *')
        children = [n for n in found['nodes'] if n['name'] in ('数学科学系', '物理系')]
        self.assertTrue(all(c['parent'] == parent['key'] for c in children))
        self.assertEqual(parent['url'], '')

    def test_same_url_retains_multiple_original_labels(self):
        html = '<a href="/notice/">教学通知</a><a href="/notice/">学生公告</a>'
        found = extract_structure(html, ROOT_URL, ROOT_URL, page_label='某大学')
        self.assertEqual(len(found['links']), 2)

    def test_unlinked_units_remain_visible(self):
        found = extract_structure('<main><ul><li>未来技术学院</li></ul></main>', ROOT_URL + 'units', ROOT_URL, 'directory', '院系设置')
        self.assertEqual(found['links'][0]['decision'], 'missing_link')
        self.assertEqual(found['links'][0]['label'], '未来技术学院')

    def test_more_button_uses_its_actual_heading(self):
        found = extract_structure('<section><h2>通知公告</h2><a href="/notice/">更多</a></section>', ROOT_URL, ROOT_URL)
        self.assertEqual(found['links'][0]['label'], '通知公告')
        self.assertEqual(found['links'][0]['raw_label'], '更多')

    def test_external_official_link_is_retained(self):
        found = extract_structure('<a href="https://join-example.edu.cn/">本科生招生</a>', ROOT_URL, ROOT_URL)
        self.assertEqual(found['links'][0]['decision'], 'official_external_link')

    def test_stale_is_not_retired_and_dates_can_be_unknown(self):
        self.assertEqual(health_from_evidence('<p>通知公告</p>', '2020-01-01'), 'stale')
        self.assertEqual(health_from_evidence('<p>通知公告</p>'), 'date_unknown')
        self.assertEqual(health_from_evidence('<p>本站停止更新，新站已经上线。</p>'), 'retirement_notice')

    def test_url_identity_preserves_functional_parameters(self):
        self.assertEqual(canonical_url('https://EXAMPLE.edu.cn/List?a=2&b=1&utm_source=x#nav'),
                         'https://example.edu.cn/List?a=2&b=1')
        self.assertNotEqual(canonical_url(ROOT_URL + '?channel=1'), canonical_url(ROOT_URL + '?channel=2'))

    def test_http_https_unit_identity_keeps_both_page_urls(self):
        from backend.scraper.discovery.structure import node_key
        a = 'http://med.example.edu.cn/'
        b = 'https://med.example.edu.cn/'
        self.assertEqual(node_key('unit', '医学院', a), node_key('unit', '医学院', b))
        self.assertNotEqual(canonical_url(a), canonical_url(b))


class FrontierTests(unittest.TestCase):
    def test_reparse_replaces_current_links_and_archives_old_paths_even_with_same_html(self):
        import gzip
        import json
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'inventory.db')
            key = store.ensure_site('某大学', ROOT_URL)
            link = {'url': ROOT_URL + 'research/', 'label': '教育教学', 'kind': 'channel',
                    'locator': '#research a', 'decision': 'follow'}
            store.record_edges(key, ROOT_URL, [link], 'unchanged-html')
            correct = dict(link, label='科学研究')
            store.record_edges(key, ROOT_URL, [correct], 'unchanged-html')
            with store.connect() as c:
                self.assertEqual([r[0] for r in c.execute('SELECT label FROM edges')], ['科学研究'])
                archived = json.loads(gzip.decompress(c.execute('SELECT records_gzip FROM edge_history').fetchone()[0]))
                self.assertEqual(archived[0]['label'], '教育教学')
            store.record_edges(key, ROOT_URL, [correct], 'unchanged-html')
            with store.connect() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM edge_history').fetchone()[0], 1)
            store.record_edges(key, ROOT_URL, [], 'unchanged-html')
            self.assertEqual(store.report(key)['edge_count'], 0)

    def test_old_news_frontier_is_retained_as_reference_and_can_be_revived_by_a_real_roster(self):
        from backend.scraper.discovery.inventory_crawler import inspect_page
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'inventory.db')
            key = store.ensure_site('某大学', ROOT_URL)
            article = ROOT_URL + 'info/1/10001.htm'
            store.enqueue(key, article, '新闻活动 机电工程学院', 'unit', 1, [], 'school_domain')
            report = store.report(key)
            root = next(p for p in report['pages'] if p['url'] == ROOT_URL)
            inspect_page(store, report['site'], root, fetcher=lambda _: {
                'html': '<a href="' + article + '">新闻活动 机电工程学院</a>', 'url': ROOT_URL, 'status': 200})
            self.assertIsNone(store.claim(key))
            page = next(p for p in store.report(key)['pages'] if p['url'] == article)
            self.assertEqual(page['state'], 'reference_only')
            self.assertEqual(page['attempts'], 0)
            directory = ROOT_URL + 'units/'
            store.enqueue(key, directory, '院系设置', 'directory', 1, [], 'school_domain')
            page = store.claim(key)
            inspect_page(store, report['site'], page, fetcher=lambda _: {
                'html': '<a href="' + article + '">机电工程学院</a>', 'url': directory, 'status': 200})
            self.assertEqual(store.claim(key)['url'], article)

    def test_stale_news_evidence_cannot_remove_a_pending_unit(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'inventory.db')
            key = store.ensure_site('某大学', ROOT_URL)
            target = ROOT_URL + 'info/1/10001.htm'
            store.finish(key, ROOT_URL, html='<p>新版官网</p>', state='fetched')
            store.enqueue(key, target, '机电工程学院', 'unit', 1, [], 'school_domain')
            store.record_edges(key, ROOT_URL, [{'url': target, 'label': '机电工程学院', 'kind': 'unit',
                               'decision': 'article_reference', 'locator': 'a'}], hashlib.sha256(b'old').hexdigest())
            self.assertEqual(store.claim(key)['url'], target)

    def test_reclassification_keeps_snapshots_and_requires_every_current_reference_to_agree(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'inventory.db')
            key = store.ensure_site('某大学', ROOT_URL)
            target, directory = ROOT_URL + 'info/1/10001.htm', ROOT_URL + 'units/'
            store.enqueue(key, directory, '院系设置', 'directory', 1, [], 'school_domain')
            store.enqueue(key, target, '机械学院', 'unit', 2, [], 'school_domain')
            for url in (ROOT_URL, directory):
                store.finish(key, url, html='<p>Current reference</p>', state='fetched')
            store.finish(key, target, html='<article>原始公告快照</article>', state='fetched')
            edge = {'url': target, 'label': '机械学院', 'kind': 'unit', 'locator': 'a'}
            digest = hashlib.sha256(b'<p>Current reference</p>').hexdigest()
            store.record_edges(key, ROOT_URL, [dict(edge, decision='article_reference')], digest)
            store.record_edges(key, directory, [dict(edge, decision='follow')], digest)
            self.assertEqual(store.reconcile_article_references(key), 0)
            store.finish(key, directory, html='<p>目录已经改版</p>', state='fetched')
            self.assertEqual(store.reconcile_article_references(key), 1)
            self.assertEqual(store.snapshot(key, target), '<article>原始公告快照</article>')
            self.assertEqual(next(p for p in store.report(key)['pages'] if p['url'] == target)['state'], 'reference_only')

    def test_deep_units_do_not_starve_known_top_level_publication_channels(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'inventory.db')
            key = store.ensure_site('某大学', ROOT_URL)
            store.claim(key)
            store.enqueue(key, ROOT_URL + 'deep-unit/', '实验室', 'unit', 8, [], 'school_domain')
            store.enqueue(key, ROOT_URL + 'notices/', '通知公告', 'channel', 1, [], 'school_domain')
            self.assertEqual(store.claim(key)['url'], ROOT_URL + 'notices/')

    def test_sdu_campus_wrapper_keeps_placeholder_and_project_without_guessing_parent(self):
        url = 'https://www.sdu.edu.cn/zzjg/xysz.htm'
        html = ('<div class="nygljg"><div class="wp"><dl><a href="https://www.wh.sdu.edu.cn/">'
                '<dt>威海校区 威海市文化西路180号 邮编：264209</dt></a><dd><ul>'
                '<li><h4><a href="#">出版学院</a></h4></li><li><h4><a href="https://www.gsp.sdu.edu.cn/">'
                '中加合作办学项目</a></h4></li></ul></dd></dl></div></div>')
        parsed = extract_structure(html, url, 'https://www.sdu.edu.cn/')
        campus = next(n for n in parsed['nodes'] if n['name'] == '威海校区')
        college = next(n for n in parsed['nodes'] if n['name'] == '出版学院')
        project = next(n for n in parsed['nodes'] if n['name'] == '中加合作办学项目')
        self.assertEqual(campus['kind'], 'group')
        self.assertEqual(campus['relation'], 'campus_group')
        self.assertEqual(campus['url'], 'https://www.wh.sdu.edu.cn/')
        self.assertEqual(college['url'], '')
        self.assertEqual(college['parent'], campus['key'])
        self.assertEqual(project['kind'], 'group')
        self.assertEqual(project['parent'], campus['key'])
        self.assertEqual(next(l['kind'] for l in parsed['links'] if l['label'] == '威海校区'), 'unit')
        self.assertEqual(next(l['kind'] for l in parsed['links'] if l['label'] == '中加合作办学项目'), 'navigation')
        self.assertFalse(any(n['relation'] == 'sub_unit' for n in parsed['nodes']))

    def test_live_progress_does_not_reload_full_page_and_publication_evidence(self):
        from unittest.mock import patch
        from backend.scraper.discovery.inventory_crawler import crawl_site
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'inventory.db')
            key = store.ensure_site('某大学', ROOT_URL)
            seen = []
            def fetch(url):
                html = ('<a href="/college/">工程学院</a><a href="/notices/">通知公告</a>'
                        if url == ROOT_URL else '<p>官网页面</p>')
                return {'url': url, 'status': 200, 'html': html}
            with patch.object(store, 'report', wraps=store.report) as full_report:
                result = crawl_site(store, key, workers=1, max_pages=3, fetcher=fetch,
                                    progress=lambda count, snapshot: seen.append((count, snapshot)))
                self.assertEqual(full_report.call_count, 1)
            self.assertEqual([count for count, _ in seen], [1, 2, 3])
            self.assertEqual([snapshot['states']['fetched'] for _, snapshot in seen], [1, 2, 3])
            self.assertTrue(all({'site', 'states'} <= set(snapshot) and 'pages' not in snapshot for _, snapshot in seen))
            self.assertEqual(seen[-1][1]['states'], result['states'])
            self.assertEqual(len(result['pages']), 3)
            self.assertFalse(result['accepted'])

    def test_cached_reparse_preserves_web_observation_time(self):
        from scripts.sources.reparse_sources import reparse_site
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'inventory.db')
            key = store.ensure_site('某大学', ROOT_URL)
            store.finish(key, ROOT_URL, html='<a href="/college/">工程学院</a>', state='fetched',
                         final_url=ROOT_URL, status_code=200, fetched_at='2026-09-01T00:00:00+00:00',
                         notes_json='["public_dns_ipv4:1.1.1.1"]')
            reparse_site(store, key)
            page = next(p for p in store.report(key)['pages'] if p['url'] == ROOT_URL)
            self.assertEqual(page['checked_at'], '2026-09-01T00:00:00+00:00')
            self.assertIn('public_dns_ipv4:1.1.1.1', page['notes_json'])
            self.assertIn('工程学院', {n['name'] for n in store.structure(key)})
            repeated = reparse_site(store, key)
            self.assertEqual(repeated['reparsed'], 0)
            self.assertEqual(repeated['already_current'], 1)
            with store.connect() as c:
                c.execute("UPDATE pages SET parser_revision='previous-version' WHERE url=?", (ROOT_URL,))
            self.assertEqual(reparse_site(store, key)['reparsed'], 1)

    def test_root_first_reparse_keeps_unit_pages_for_the_next_pass(self):
        from scripts.sources.reparse_sources import reparse_site
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'inventory.db')
            key = store.ensure_site('某大学', ROOT_URL)
            unit = ROOT_URL + 'college/'
            store.enqueue(key, unit, '工程学院', 'unit', 1, [], 'school_domain')
            for url in (ROOT_URL, unit):
                store.finish(key, url, html='<p>官网页面</p>', state='fetched')
            self.assertEqual(reparse_site(store, key, roots_only=True)['reparsed'], 1)
            remaining = next(p for p in store.report(key)['pages'] if p['url'] == unit)
            self.assertIsNone(remaining['parser_revision'])
            self.assertEqual(reparse_site(store, key)['reparsed'], 1)

    def test_undated_current_columns_prevent_a_page_wide_staleness_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'inventory.db')
            key = store.ensure_site('某大学', ROOT_URL)
            html = ''.join('<section><h2>' + title + '</h2><ul>' + ''.join(
                '<li><a href="/info/1/' + str(start + i) + '.htm">学校学术工作安排通知' + str(i) + '</a>' + date + '</li>'
                for i in range(3)) + '</ul></section>' for title, start, date in (
                    ('教学通知', 10000, '<time>2020-03-01</time>'), ('学校新闻', 20000, '')))
            result = crawl_site(store, key, max_pages=1, fetcher=lambda _: {'html': html, 'url': ROOT_URL, 'status': 200})
            root = next(p for p in result['pages'] if p['url'] == ROOT_URL)
            self.assertEqual(root['health'], 'date_unknown')
            self.assertIn('publication_dates_incomplete', root['notes_json'])
            self.assertEqual(result['match_threshold'], 90)

    def test_budget_pause_resume_and_missing_baseline_never_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'inventory.db')
            key = store.ensure_site('某大学', ROOT_URL)
            pages = {
                ROOT_URL: '<a href="/units/">院系设置</a>',
                ROOT_URL + 'units/': '<a href="/math/">数学学院</a><a href="/physics/">物理学院</a>',
                ROOT_URL + 'math/': '<h1>数学学院</h1>',
                ROOT_URL + 'physics/': '<h1>物理学院</h1>',
            }
            def fake(url):
                return {'html': pages[url], 'url': url, 'status': 200}
            first = crawl_site(store, key, max_pages=1, fetcher=fake)
            self.assertEqual(first['states']['pending'], 1)
            self.assertFalse(first['accepted'])
            second = crawl_site(store, key, max_pages=10, fetcher=fake)
            self.assertEqual(second['states'], {'fetched': 4})
            self.assertEqual(second['site']['state'], 'needs_review')
            self.assertIsNone(second['metrics']['units']['percent'])
            self.assertFalse(second['accepted'])
            self.assertIn('数学学院', {n['name'] for n in store.structure(key)})
            self.assertIn('院系设置', store.snapshot(key, ROOT_URL))

    def test_failed_unit_kept_for_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'inventory.db')
            key = store.ensure_site('某大学', ROOT_URL)
            def unavailable(url):
                return {'html': '', 'url': url, 'status': 503, 'error': 'HTTP 503'}
            result = crawl_site(store, key, fetcher=unavailable)
            self.assertEqual(result['states'], {'failed': 1})
            self.assertEqual(result['pages'][0]['health'], 'unreachable')
            self.assertFalse(result['accepted'])
            store.retry(key)
            self.assertEqual(store.report(key)['states'], {'pending': 1})


if __name__ == '__main__':
    unittest.main()
