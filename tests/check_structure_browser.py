"""Inspect the source hierarchy using real cached official evidence and an isolated account database."""
import json
import logging
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import create_app
from backend.database.db import db
from backend.database.models import School, Department, User, Subscription
from backend.services.source_inventory import DEFAULT_PATH, Inventory, site_key
from scripts.verify_source_inventory import verify_file
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    out = ROOT / 'data' / 'ui-check'
    out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='source-structure-ui-') as directory:
        inventory_path = Path(directory) / 'inventory.sqlite3'
        with closing(sqlite3.connect(DEFAULT_PATH.as_uri() + '?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(inventory_path)) as copied:
                original.backup(copied)
        # The UI fixture owns a consistent copy. Recheck its scopes independently
        # of a live crawler that may have just invalidated an older parser review.
        copied_inventory = Inventory(inventory_path)
        reviewed_sites = set()
        for baseline in (ROOT / 'docs' / 'source_baselines').glob('*.json'):
            result = verify_file(copied_inventory, baseline, reparse=True)
            assert result['scope_passed'], (baseline.name, result['status'])
            reviewed_sites.add(site_key(json.loads(baseline.read_text(encoding='utf-8'))['root_url']))
        for key in reviewed_sites:
            assert all(c['scope_passed'] for c in copied_inventory.report(key)['reference_checks']), key
        review_root = 'https://review.example.edu.cn/'
        review_key = copied_inventory.ensure_site('来源复查测试学校', review_root)
        review_urls = {review_root + f'unit-{number:03}/' for number in range(123)}
        for target in review_urls:
            copied_inventory.enqueue(review_key, target, target.rsplit('/', 2)[-2], 'unit', 1, [], 'school_domain')
            copied_inventory.finish(review_key, target, state='failed', health='unreachable')
        external_url = 'https://partner.example.org/'
        copied_inventory.enqueue(review_key, external_url, '外部单位入口', 'unit', 1, [], 'official_backlink')
        copied_inventory.finish(review_key, external_url, state='fetched', final_url=external_url,
                                health='date_unknown', notes_json=json.dumps(['external_ownership_requires_review']))
        review_urls.add(external_url)
        dynamic_url = review_root + '#/notices?department=1'
        copied_inventory.enqueue(review_key, dynamic_url, '动态通知入口', 'channel', 1, [], 'school_domain')
        copied_inventory.finish(review_key, dynamic_url, state='fetched', final_url=review_root,
                                health='dynamic_content', notes_json=json.dumps(['fragment_route_requires_browser']))
        review_urls.add(dynamic_url)
        app = create_app({'TESTING': True, 'SECRET_KEY': 'source-structure-ui',
                          'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(directory) / 'app.db'),
                          'SOURCE_INVENTORY_PATH': inventory_path})
        with app.app_context():
            db.create_all()
            school = School(name='清华大学', url='https://www.tsinghua.edu.cn/')
            cupk = School(name='中国石油大学（北京）克拉玛依校区', url='https://www.cupk.edu.cn/')
            review = School(name='来源复查测试学校', url=review_root)
            ustc = School(name='中国科学技术大学', url='https://www.ustc.edu.cn/')
            whu = School(name='武汉大学', url='https://www.whu.edu.cn/')
            gzhu = School(name='广州大学', url='https://www.gzhu.edu.cn/')
            sdu = School(name='山东大学', url='https://www.sdu.edu.cn/')
            ysu = School(name='燕山大学', url='https://www.ysu.edu.cn/')
            buaa = School(name='北京航空航天大学', url='https://www.buaa.edu.cn/')
            nju = School(name='南京大学', url='https://www.nju.edu.cn/')
            fudan = School(name='复旦大学', url='https://www.fudan.edu.cn/')
            zju = School(name='浙江大学', url='https://www.zju.edu.cn/')
            cup = School(name='中国石油大学（北京）', url='https://www.cup.edu.cn/')
            swjtu = School(name='西南交通大学', url='https://www.swjtu.edu.cn/')
            shutcm = School(name='上海中医药大学', url='https://www.shutcm.edu.cn/')
            csust = School(name='长沙理工大学', url='https://www.csust.edu.cn/')
            muc = School(name='中央民族大学', url='https://www.muc.edu.cn/')
            hubu = School(name='湖北大学', url='https://www.hubu.edu.cn/')
            snnu = School(name='陕西师范大学', url='https://www.snnu.edu.cn/')
            hrbeu = School(name='哈尔滨工程大学', url='https://www.hrbeu.edu.cn/')
            cqmu = School(name='重庆医科大学', url='https://www.cqmu.edu.cn/')
            db.session.add(school)
            db.session.add(cupk)
            db.session.add(review)
            db.session.add_all([ustc, whu, gzhu, sdu, ysu, buaa, nju, fudan, zju, cup, swjtu, shutcm, csust, muc, hubu, snnu, hrbeu, cqmu])
            db.session.commit()
            school_id = school.id
            muc_id, hubu_id = muc.id, hubu.id
            snnu_id, hrbeu_id, cqmu_id = snnu.id, hrbeu.id, cqmu.id
            cup_id = cup.id
            swjtu_id = swjtu.id
            shutcm_id, csust_id = shutcm.id, csust.id
            cupk_id = cupk.id
            review_id = review.id
            ustc_id, whu_id = ustc.id, whu.id
            gzhu_id = gzhu.id
            sdu_id = sdu.id
            ysu_id = ysu.id
            buaa_id = buaa.id
            nju_id = nju.id
            fudan_id = fudan.id
            zju_id = zju.id
            reader = User(username='结构关联检查', password_hash='unused')
            source = Department(school_id=cupk_id, name='石油工程学院', group_name='院系设置',
                                list_url='https://www.cupk.edu.cn/sgxy/tzgg/')
            unknown = Department(school_id=cupk_id, name='关系待核实栏目', list_url='https://www.cupk.edu.cn/unconfirmed/')
            geoscience = Department(school_id=cupk_id, name='院内新闻', group_name='地球科学与工程学院',
                                    list_url='https://www.cupk.edu.cn/syxy/ynxw/')
            db.session.add_all([reader, source, unknown, geoscience])
            db.session.flush()
            db.session.add(Subscription(user_id=reader.id, school_id=cupk_id, department_ids=[source.id]))
            db.session.commit()
            source_id = source.id
            geoscience_id = geoscience.id
            cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id': reader.id, '_csrf_token': 'structure-check'})
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/schools/{school_id}/structure?reference=https://www.tsinghua.edu.cn/yxsz.htm'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge', headless=True)
                page = browser.new_page()
                page.context.add_cookies([{'name': 'session', 'value': cookie,
                                          'url': f'http://127.0.0.1:{server.server_port}'}])
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                for width in (390, 1440):
                    page.set_viewport_size({'width': width, 'height': 950})
                    page.goto(url)
                    assert '尚未达到 90%' in page.locator('.source-sync').inner_text()
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '126 / 126' in page.locator('.source-sync').inner_text()
                    science = page.locator('summary').filter(has=page.get_by_text('* 理学院 *', exact=True)).first
                    science.click()
                    assert '官网标注：非实体学院' in science.inner_text()
                    parent = science.locator('..')
                    assert '数学科学系' in parent.inner_text()
                    assert '天文系' in parent.inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    science.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-{width}.png'))
                    differences = page.locator('#directoryDifferences')
                    differences.locator('summary').click()
                    assert '能源与动力工程系' in differences.inner_text()
                    assert differences.get_by_role('link', name='https://www.depe.tsinghua.edu.cn/', exact=True).count() == 2
                    assert differences.get_by_role('link', name='http://www.te.tsinghua.edu.cn/', exact=True).count() == 1
                    assert '语言教学中心' in differences.inner_text()
                    assert '此目录未列出' in differences.inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    differences.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-variants-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{cupk_id}/structure')
                    assert page.locator('#structureReference').input_value() == 'https://www.cupk.edu.cn/c/2021-06-29/508819.shtml'
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '27 / 27' in page.locator('.source-sync').inner_text()
                    assert '尚未达到 90%' in page.locator('.source-sync').inner_text()
                    page.screenshot(path=str(out / f'structure-baseline-{width}.png'))
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate('''() => [...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > innerWidth + 1).map(e => [e.tagName, e.className, e.getBoundingClientRect().width, e.innerText?.slice(0, 120)]).slice(0, 12)''')
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{cupk_id}/structure?reference=https://www.cupk.edu.cn/c/2017-04-21/485097.shtml')
                    shared = page.locator('summary').filter(has=page.get_by_text('组织人事部 / 党委教师工作部', exact=True)).first
                    shared.click()
                    assert '师资办公室' in shared.locator('..').inner_text()
                    assert '具体挂靠范围待核实' in shared.inner_text()
                    assert '机关党委工会新闻中心' not in page.locator('body').inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    shared.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-shared-row-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{review_id}/structure')
                    page.locator('#sourceIssues > summary').click()
                    seen = set()
                    for number in range(1, 4):
                        issues = page.locator('#sourceIssues')
                        assert issues.evaluate('(element) => element.open')
                        assert f'第 {number} / 3 页' in issues.inner_text()
                        seen.update(issues.locator('.structure-issues a').evaluate_all('(links) => links.map(link => link.href)'))
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        if number < 3:
                            issues.get_by_role('link', name='下一页', exact=True).click()
                    assert seen == review_urls
                    assert '外部站点归属待核实' in issues.inner_text()
                    assert '动态页面，具体内容待核实' in issues.inner_text()
                    issues.locator('nav').scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-review-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/subscriptions/{cupk_id}')
                    source_entry = page.locator(f'.source-entry[data-source-id="{source_id}"]')
                    source_entry.locator('summary').click()
                    assert '院系设置' in source_entry.locator('.source-path').inner_text()
                    assert '石油工程学院' in source_entry.locator('.source-path').inner_text()
                    assert source_entry.locator('input[name="department"]').count() == 1
                    assert source_entry.locator('input[name="department"]').is_checked()
                    assert page.locator('.source-path-pending').count() == 1
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    source_entry.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'source-hierarchy-{width}.png'))
                    geo_entry = page.locator(f'.source-entry[data-source-id="{geoscience_id}"]')
                    geo_entry.locator('summary').click()
                    assert '地球科学与工程学院' in geo_entry.locator('.source-path').inner_text()
                    assert '学院新闻' in geo_entry.locator('.source-path').inner_text()
                    assert '石油工程学院' not in geo_entry.locator('.source-path').inner_text()
                    assert not geo_entry.locator('input[name="department"]').is_checked()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    geo_entry.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'source-geoscience-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{ustc_id}/structure?reference=https://www.ustc.edu.cn/yxjs.htm')
                    page.locator('summary').filter(has=page.get_by_text('学部/学院', exact=True)).first.click()
                    page.locator('summary').filter(has=page.get_by_text('信息与智能学部', exact=True)).first.click()
                    college = page.locator('summary').filter(has=page.get_by_text('信息科学技术学院', exact=True)).first
                    college.click()
                    assert '自动化系' in college.locator('..').inner_text()
                    assert '计算机科学与技术学院' not in college.locator('..').inner_text()
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '97 / 97' in page.locator('.source-sync').inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    college.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-ustc-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{whu_id}/structure?reference=https://www.whu.edu.cn/jgsz/yxsz.htm')
                    science = page.locator('summary').filter(has=page.get_by_text('理学部', exact=True)).first
                    science.click()
                    assert '数学与统计学院' in science.locator('..').inner_text()
                    cross = page.locator('summary').filter(has=page.get_by_text('跨学科类', exact=True)).first
                    cross.click()
                    assert '弘毅学堂' in cross.locator('..').inner_text()
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '50 / 50' in page.locator('.source-sync').inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    cross.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-whu-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{gzhu_id}/structure?reference=https://www.gzhu.edu.cn/')
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '4 / 4' in page.locator('.source-sync').inner_text()
                    assert '栏目名称、入口及列表范围一致' in page.locator('.source-sync').inner_text()
                    assert '尚未达到 90%' in page.locator('.source-sync').inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.locator('.source-sync').scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-gzhu-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{sdu_id}/structure?reference=https://www.sdu.edu.cn/zzjg/xysz.htm')
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '93 / 93' in page.locator('.source-sync').inner_text()
                    for campus in ('千佛山校区', '兴隆山校区'):
                        heading = page.locator('summary').filter(has=page.get_by_text(campus, exact=True)).first
                        heading.click()
                        assert '官网按校区分组，不代表行政隶属' in heading.inner_text()
                        assert '材料科学与工程学院' in heading.locator('..').inner_text()
                    for campus, art_url in [('洪家楼校区', 'http://www.art.sdu.edu.cn/'), ('威海校区', 'https://art.wh.sdu.edu.cn/')]:
                        heading = page.locator('summary').filter(has=page.get_by_text(campus, exact=True)).first
                        heading.click()
                        art = heading.locator('..').locator('.structure-node').filter(has=page.get_by_text('艺术学院', exact=True))
                        assert art.get_by_role('link', name='官网入口', exact=True).get_attribute('href') == art_url
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    heading.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-sdu-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{ysu_id}/structure?reference=http://journal.ysu.edu.cn/')
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '1 / 1' in page.locator('.source-sync').inner_text()
                    recommendation = page.locator('.structure-leaf').filter(has=page.get_by_text('文章推荐', exact=True))
                    assert recommendation.count() == 1
                    assert '本页发布栏目' in recommendation.inner_text()
                    assert recommendation.locator('..').get_by_role('link', name='官网入口', exact=True).get_attribute('href') == 'http://journal.ysu.edu.cn/wztj.htm'
                    footer_link = page.locator('.structure-leaf').filter(has=page.get_by_text('国家新闻出版署', exact=True))
                    assert '页脚链接，栏目归属待核实' in footer_link.inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    recommendation.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-ysu-columns-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{buaa_id}/structure?reference=https://www.buaa.edu.cn/jgsz/jxkyjg02.htm')
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '57 / 57' in page.locator('.source-sync').inner_text()
                    assert '尚未达到 90%' in page.locator('.source-sync').inner_text()
                    group = page.locator('summary').filter(has=page.get_by_text('融合创新示范区', exact=True)).first
                    group.click()
                    assert '官网教学科研分组，不代表行政隶属' in group.inner_text()
                    shared = page.locator('summary').filter(has=page.get_by_text('中法工程师学院 / 国际通用工程学院', exact=True)).first
                    shared.click()
                    assert '官网同一条目并列，不代表上下级' in shared.inner_text()
                    for label in ('中法工程师学院', '国际通用工程学院'):
                        leaf = shared.locator('..').locator('.structure-leaf').filter(has=page.get_by_text(label, exact=True))
                        assert leaf.count() == 1
                        assert '官网同一条目并列' in leaf.inner_text()
                    missing = shared.locator('..').locator('.structure-leaf').filter(has=page.get_by_text('国际通用工程学院', exact=True)).locator('..')
                    assert missing.get_by_role('link', name='官网入口', exact=True).count() == 0
                    aviation = page.locator('summary').filter(has=page.get_by_text('航空航天学科群', exact=True)).first
                    aviation.click()
                    college = page.locator('summary').filter(has=page.get_by_text('航空科学与工程学院', exact=True)).first
                    college.click()
                    hidden = college.locator('..').locator('.structure-leaf').filter(has=page.get_by_text('中国商飞-北航大飞机研究院', exact=True))
                    assert '官网模板标为隐藏，关系待核实' in hidden.inner_text()
                    shenyuan = page.locator('summary').filter(has=page.get_by_text('沈元学院', exact=True)).first
                    shenyuan.click()
                    combined = shenyuan.locator('..').locator('.structure-leaf').filter(has=page.get_by_text('高等理工学院/未来空天技术学院/国家卓越工程师学院/量子科技学院', exact=True))
                    assert '官网同一入口列出多个名称' in combined.inner_text()
                    assert combined.locator('..').get_by_role('link', name='官网入口', exact=True).get_attribute('href') == 'https://hc.buaa.edu.cn/'
                    innovation = page.locator('summary').filter(has=page.get_by_text('国际创新学院', exact=True)).first
                    innovation.click()
                    assert innovation.locator('..').locator('.structure-leaf').count() == 3
                    assert '下级入口' not in shared.inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    shared.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-buaa-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{nju_id}/structure?reference=https://www.nju.edu.cn/xybm.htm')
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '131 / 131' in page.locator('.source-sync').inner_text()
                    assert '尚未达到 90%' in page.locator('.source-sync').inner_text()
                    college_group = page.locator('summary').filter(has=page.get_by_text('学院', exact=True)).first
                    college_group.click()
                    assert '官网机构分类，不代表行政隶属' in college_group.inner_text()
                    business = page.locator('summary').filter(has=page.get_by_text('商学院', exact=True)).first
                    business.click()
                    for label, address in [('经济学院', 'http://njubs.nju.edu.cn/intro.php/a'), ('管理学院', 'http://njubs.nju.edu.cn/intro.php/e')]:
                        leaf = business.locator('..').locator('.structure-leaf').filter(has=page.get_by_text(label, exact=True))
                        assert '目录嵌套' in leaf.inner_text()
                        assert leaf.locator('..').get_by_role('link', name='官网入口', exact=True).get_attribute('href') == address
                    admin = page.locator('summary').filter(has=page.get_by_text('行政部门', exact=True)).first
                    admin.click()
                    undergraduate = page.locator('summary').filter(has=page.get_by_text('本科生院', exact=True)).first
                    undergraduate.click()
                    assert undergraduate.locator('..').locator('.structure-leaf').count() == 7
                    for label in ('学生发展支持中心', '教育教学发展与评估中心'):
                        missing = undergraduate.locator('..').locator('.structure-leaf').filter(has=page.get_by_text(label, exact=True)).locator('..')
                        assert missing.get_by_role('link', name='官网入口', exact=True).count() == 0
                        assert '官网未提供独立入口' in missing.inner_text()
                    direct = page.locator('summary').filter(has=page.get_by_text('直属单位', exact=True)).first
                    direct.click()
                    archives = page.locator('summary').filter(has=page.get_by_text('档案馆 校史研究室', exact=True)).first
                    archives.click()
                    assert archives.locator('..').locator('.structure-leaf').count() == 3
                    assert archives.locator('..').locator(':scope > .structure-node-meta > a').get_attribute('href') == 'http://dawww.nju.edu.cn/'
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    undergraduate.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-nju-{width}.png'))
                    for profile_path, label, expected in [('njdxjjxy', '经济学院', 5), ('njdxglxy', '管理学院', 4)]:
                        page.goto(f'http://127.0.0.1:{server.server_port}/schools/{nju_id}/structure?reference=http://nubs.nju.edu.cn/{profile_path}/list.htm')
                        profile_branch = page.locator('summary').filter(has=page.get_by_text(label, exact=True)).filter(has_text='单位介绍入口，栏目归属待核实').first
                        profile_branch.click()
                        assert '介绍正文待更新' in profile_branch.locator('..').locator(':scope > .structure-node-meta').inner_text()
                        children = profile_branch.locator('..').locator('.structure-leaf')
                        assert children.count() == expected
                        assert all('单位介绍入口，栏目归属待核实' in leaf.inner_text() for leaf in children.all())
                        news = page.locator('.structure-leaf').filter(has=page.get_by_text('新闻动态', exact=True))
                        assert news.count() == 1
                        assert '网站公共导航，发布单位待核实' in news.inner_text()
                        assert '存在循环引用' not in page.locator('.structure-tree').inner_text()
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        profile_branch.scroll_into_view_if_needed()
                        page.screenshot(path=str(out / f'structure-nju-{profile_path}-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{fudan_id}/structure?reference=https://www.fudan.edu.cn/489/list.htm')
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '47 / 47' in page.locator('.source-sync').inner_text()
                    academic = page.locator('summary').filter(has=page.get_by_text('院系专业', exact=True)).filter(has_text='官网机构分类，不代表行政隶属').first
                    academic.click()
                    assert academic.locator('..').locator('.structure-leaf').count() == 46
                    for full_label in ('现代物理研究所/核科学与技术系', '智能机器人与先进制造创新学院'):
                        leaf = academic.locator('..').locator('.structure-leaf').filter(has=page.get_by_text(full_label, exact=True))
                        assert leaf.count() == 1
                        assert '...' not in leaf.inner_text()
                    missing = academic.locator('..').locator('.structure-leaf').filter(has=page.get_by_text('中山临床医学院', exact=True)).locator('..')
                    assert missing.get_by_role('link', name='官网入口', exact=True).count() == 0
                    differences = page.locator('#directoryDifferences')
                    assert '11 项' in differences.locator('summary').inner_text()
                    differences.locator('summary').click()
                    medical = differences.locator(':scope > ul > li').filter(has=page.get_by_text('基础医学院', exact=True))
                    assert set(medical.get_by_role('link').evaluate_all('(links) => links.map(link => link.href)')) == {'http://basicmed.fudan.edu.cn/', 'http://medicine.fudan.edu.cn/'}
                    forensic = differences.locator(':scope > ul > li').filter(has=page.get_by_text('法医学与法庭科学学院', exact=True))
                    assert '页脚院系目录：官网未提供链接' in forensic.inner_text()
                    assert '尚未达到 90%' in page.locator('.source-sync').inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    differences.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-fudan-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{zju_id}/structure?reference=https://www.zju.edu.cn/xywxw/list.htm')
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '53 / 53' in page.locator('.source-sync').inner_text()
                    assert '尚未达到 90%' in page.locator('.source-sync').inner_text()
                    for group_name in ('人文学部', '社会科学学部', '理学部', '工学部', '信息学部', '农业生命环境学部', '医药学部'):
                        group = page.locator('summary').filter(has=page.get_by_text(group_name, exact=True)).filter(has_text='官网教学科研分组').first
                        assert group.count() == 1
                        group.click()
                    for parent_name, expected in [('公共管理学院', ['社会学系']),
                                                  ('计算机科学与技术学院', ['网络空间安全学院']),
                                                  ('医学院', ['基础医学系', '脑科学与脑医学系', '公共卫生学院'])]:
                        parent = page.locator('summary').filter(has=page.get_by_text(parent_name, exact=True)).first
                        parent.click()
                        leaves = parent.locator('..').locator('.structure-leaf')
                        assert leaves.count() == len(expected)
                        for name in expected:
                            leaf = leaves.filter(has=page.get_by_text(name, exact=True))
                            assert '目录嵌套' in leaf.inner_text()
                        if parent_name == '计算机科学与技术学院':
                            assert parent.locator('..').locator(':scope > .structure-node-meta > a').get_attribute('href') == 'http://www.cs.zju.edu.cn/'
                            assert leaves.locator('..').get_by_role('link', name='官网入口', exact=True).get_attribute('href') == 'https://icsr.zju.edu.cn/'
                    assert '存在循环引用' not in page.locator('.structure-tree').inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    parent.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-zju-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{zju_id}/structure?reference=http://www.cmm.zju.edu.cn/55143/list.htm')
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '49 / 49' in page.locator('.source-sync').inner_text()
                    admin = page.locator('summary').filter(has=page.get_by_text('职能部门', exact=True)).filter(has_text='官网机构分类').first
                    admin.click()
                    teaching = admin.locator('..').locator('summary').filter(has=page.get_by_text('教学办公室', exact=True)).first
                    teaching.click()
                    assert teaching.locator('..').locator('.structure-leaf').count() == 3
                    assert '官网附加列示，隶属关系待核实' in teaching.locator('..').inner_text()
                    party = admin.locator('..').locator('summary').filter(has=page.get_by_text('党政办公室', exact=True)).first
                    party.click()
                    assert party.locator('..').locator('.structure-leaf').count() == 2
                    differences = page.locator('#directoryDifferences')
                    assert '19 项' in differences.locator('summary').inner_text()
                    differences.locator('summary').click()
                    undergraduate = differences.locator(':scope > ul > li').filter(has=page.get_by_text('教学办公室 → 本科生教育办公室', exact=True))
                    assert '官网明确写明下设' in undergraduate.inner_text()
                    assert '括号列示，隶属关系待核实' in undergraduate.inner_text()
                    assert '基础医学院' in differences.inner_text() and '基础医学系' in differences.inner_text()
                    assert '尚未达到 90%' in page.locator('.source-sync').inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    differences.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-zju-medical-organization-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{zju_id}/structure?reference=http://www.cmm.zju.edu.cn/55144/list.htm')
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '31 / 31' in page.locator('.source-sync').inner_text()
                    for group_name, count in [('基础医学系', 10), ('脑科学与脑医学系', 1), ('公共卫生学院', 6), ('临床医学院', 6), ('口腔医学院', 1), ('护理系', 1)]:
                        group = page.locator('summary').filter(has=page.get_by_text(group_name, exact=True)).filter(has_text='官网机构分类').first
                        group.click()
                        assert group.locator('..').locator('.structure-leaf').count() == count
                        assert group.locator('..').locator(':scope > .structure-node-meta > a').count() == 0
                    assert '存在循环引用' not in page.locator('.structure-tree').inner_text()
                    assert '{栏目名称}' not in page.locator('.structure-tree').inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    group.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-zju-medical-academic-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{zju_id}/structure?reference=http://www.cmm.zju.edu.cn/')
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '6 / 6' in page.locator('.source-sync').inner_text()
                    for name in ('学院新闻', '科研进展', '人才招聘', '通知公告', '学术讲座', '视听浙医'):
                        assert page.locator('.structure-tree').get_by_text(name, exact=True).count() >= 1
                    assert page.locator('.structure-tree a[href="http://www.cmm.zju.edu.cn/stzy/list.htm"]').count() >= 1
                    assert '尚未达到 90%' in page.locator('.source-sync').inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(out / f'structure-zju-medical-home-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{zju_id}/structure?reference=http://www.cmm.zju.edu.cn/rczp/list.htm')
                    assert page.locator('select[name="reference"]').input_value() == 'http://www.cmm.zju.edu.cn/rczp/list.htm'
                    for name in ('教师', '行政', '博后', '其它'):
                        entry = page.locator('.structure-leaf').filter(has=page.get_by_text(name, exact=True)).filter(has_text='本页发布栏目')
                        assert entry.count() == 1
                        assert '本页发布栏目' in entry.inner_text()
                    assert '{栏目URL}' not in page.locator('.structure-tree').inner_text()
                    assert '派遣至' not in page.locator('.structure-tree').inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.locator('.structure-tree').scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-zju-medical-recruitment-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{zju_id}/structure?reference=http://www.cmm.zju.edu.cn/mtkzy/list.htm')
                    assert page.locator('select[name="reference"]').input_value() == 'http://www.cmm.zju.edu.cn/mtkzy/list.htm'
                    assert page.locator('.structure-tree').get_by_text('媒体看浙医', exact=True).count() >= 1
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{cup_id}/structure?reference=https://www.cup.edu.cn/news/')
                    assert page.locator('#structureReference').input_value() == 'https://www.cup.edu.cn/news/'
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '5 / 5' in page.locator('.source-sync').inner_text()
                    for name in ('要闻', '时讯', '风华石大'):
                        assert page.locator('.structure-tree').get_by_text(name, exact=True).count() >= 1
                    assert page.locator('.structure-tree a[href="https://www.cup.edu.cn/news/fhsd/index.htm"]').count() >= 1
                    assert '尚未达到 90%' in page.locator('.source-sync').inner_text()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(out / f'structure-cup-news-{width}.png'))
                    for suffix, college_name, names in [
                        ('sgxy/bksjy/bkzysz/', '石油工程学院', ['石油工程专业', '油气储运工程专业']),
                        ('jdxy/bkjy/zysz/jxsjzzjqzdh/', '机电工程学院',
                         ['机械设计制造及其自动化', '过程装备与控制工程', '自动化'])]:
                        reference = 'https://www.cupk.edu.cn/' + suffix
                        page.goto(f'http://127.0.0.1:{server.server_port}/schools/{cupk_id}/structure?reference={reference}')
                        assert page.locator('#structureReference').input_value() == reference
                        ownership = page.locator('div.structure-origin')
                        assert college_name in ownership.inner_text()
                        assert '专业介绍不等于独立的通知发布单位' in ownership.inner_text()
                        majors = page.locator('.structure-leaf').filter(has_text='专业目录条目')
                        assert majors.count() == len(names)
                        for name in names:
                            assert majors.filter(has=page.get_by_text(name, exact=True)).count() == 1
                        assert '尚未达到 90%' in page.locator('.source-sync').inner_text()
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        majors.first.scroll_into_view_if_needed()
                        page.screenshot(path=str(out / f'structure-{suffix.split("/")[0]}-majors-{width}.png'))
                    page.goto(f'http://127.0.0.1:{server.server_port}/schools/{swjtu_id}/structure?reference=http://sfl.swjtu.edu.cn/')
                    page.get_by_text('已核对的目录范围', exact=True).click()
                    assert '7 / 7' in page.locator('.source-sync').inner_text()
                    notice_group = page.locator('summary').filter(has=page.get_by_text('通知公告', exact=True)).filter(has_text='官网栏目分组')
                    assert notice_group.count() == 1
                    notice_group.click()
                    notices = notice_group.locator('..')
                    assert notices.locator('.structure-leaf').filter(has_text='本页发布栏目').count() == 7
                    for suffix in ('bksjy', 'yjsjy', 'xsgz'):
                        assert notices.locator(f'a[href="http://sfl.swjtu.edu.cn/tzgg/{suffix}.htm"]').count() == 1
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    notice_group.scroll_into_view_if_needed()
                    page.screenshot(path=str(out / f'structure-swjtu-student-notices-{width}.png'))
                    for suffix, name in [('bksjy', '本科生教育'), ('yjsjy', '研究生教育'), ('xsgz', '学生工作')]:
                        reference = f'http://sfl.swjtu.edu.cn/tzgg/{suffix}.htm'
                        page.goto(f'http://127.0.0.1:{server.server_port}/schools/{swjtu_id}/structure?reference={reference}')
                        assert page.locator('#structureReference').input_value() == reference
                        group = page.locator('summary').filter(has=page.get_by_text('通知公告', exact=True)).filter(has_text='官网栏目分组')
                        group.click()
                        assert group.locator('..').locator('.structure-leaf').filter(has=page.get_by_text(name, exact=True)).count() == 1
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    for roster_school_id, reference, total, first_name in [
                        (shutcm_id, 'https://www.shutcm.edu.cn/ejxy/list.htm', 17, '中医学院'),
                        (csust_id, 'https://www.csust.edu.cn/jgsz/jxy.htm', 26, '交通学院'),
                        (muc_id, 'https://www.muc.edu.cn/zzjg/jxhkydw1.htm', 42, '民族学与社会学学院'),
                        (hubu_id, 'https://www.hubu.edu.cn/zzjg/xbxy.htm', 35, '生命科学学部')]:
                        page.goto(f'http://127.0.0.1:{server.server_port}/schools/{roster_school_id}/structure?reference={reference}')
                        assert page.locator('#structureReference').input_value() == reference
                        page.get_by_text('已核对的目录范围', exact=True).click()
                        assert f'{total} / {total}' in page.locator('.source-sync').inner_text()
                        if roster_school_id in (muc_id, hubu_id):
                            page.locator('#directoryLinkIssues summary').click()
                            link_issues = page.locator('#directoryLinkIssues').inner_text()
                            assert '链接回到当前目录' in link_issues
                            if roster_school_id == muc_id:
                                assert '原链接缺少网址协议' in link_issues
                                assert '官网标注：非独立科研平台' in page.locator('.structure-tree').inner_text()
                        if roster_school_id == hubu_id:
                            page.locator('.structure-tree summary').filter(has=page.get_by_text('学部设置', exact=True)).click()
                        first_unit = page.locator('.structure-leaf').filter(has=page.get_by_text(first_name, exact=True)).filter(has_text='目录列示')
                        assert first_unit.count() == 1
                        assert '存在循环引用' not in page.locator('.structure-tree').inner_text()
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        first_unit.scroll_into_view_if_needed()
                        page.screenshot(path=str(out / f'structure-academic-{roster_school_id}-{width}.png'))
                    for programme_school_id, reference, total, programme_count, parent_name, first_name in [
                        (snnu_id, 'http://www.snnu.edu.cn/jyjx/bkzy.htm', 90, 69, '教育学部', '教育技术学'),
                        (hrbeu_id, 'http://ugs.hrbeu.edu.cn/2819/list.htm', 59, 59, '', '船舶与海洋工程'),
                        (cqmu_id, 'https://bzkzs.cqmu.edu.cn/xxgk/xyzy.htm', 59, 43, '第一临床学院+人工智能医学学院', '临床医学(AI医学创新班）')]:
                        page.goto(f'http://127.0.0.1:{server.server_port}/schools/{programme_school_id}/structure?reference={reference}')
                        assert page.locator('#structureReference').input_value() == reference
                        page.get_by_text('已核对的目录范围', exact=True).click()
                        assert f'{total} / {total}' in page.locator('.source-sync').inner_text()
                        tree = page.locator('.structure-tree').first
                        assert tree.locator('.structure-leaf').filter(has_text='专业目录条目').count() == programme_count
                        if parent_name:
                            tree.locator('summary').filter(has=page.get_by_text(parent_name, exact=True)).click()
                        if programme_school_id == snnu_id:
                            assert '原目录标注 2024年11月' in page.locator('.directory-note').all_inner_texts().__str__()
                            assert '专业代码：040104' in tree.inner_text()
                        if programme_school_id == cqmu_id:
                            assert '官网联合培养分组，不代表独立学院' in tree.inner_text()
                            assert tree.locator('.structure-leaf').filter(has=page.get_by_text(first_name, exact=True)).count() == 3
                        first_programme = tree.locator('.structure-leaf').filter(has=page.get_by_text(first_name, exact=True)).first
                        first_programme.scroll_into_view_if_needed()
                        assert '存在循环引用' not in tree.inner_text()
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        page.screenshot(path=str(out / f'structure-programmes-{programme_school_id}-{width}.png'))
                browser.close()
                assert not errors, errors
                print(json.dumps({'widths': [390, 1440], 'browser_errors': errors,
                                  'evidence': 'Cached official directories and columns: Tsinghua, CUPK, USTC, Wuhan, Guangzhou, Shandong, Yanshan, Beihang, Nanjing, Fudan and Zhejiang University'}, ensure_ascii=True))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            with app.app_context():
                db.session.remove()
                db.engine.dispose()


if __name__ == '__main__':
    check()
