"""Public teaching columns retain local headings and only explicit complete dates."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bs4 import BeautifulSoup
from backend.scraper.discovery.publication_lists import publication_lists, heading_evidence
from backend.scraper.date_elements import infer_publication_date_selector, publication_date_text


class TeachingModuleEvidenceTests(unittest.TestCase):
    def fixture(self, short):
        return (Path(__file__).parent / 'fixtures' / (short + '_teaching_modules.html')).read_text(encoding='utf-8')

    def test_cupk_module_head_and_date_after_title_span(self):
        feeds = publication_lists(self.fixture('cupk'), 'https://www.cupk.edu.cn/jwb/')
        self.assertEqual({f['name']: (f['item_count'], f['dated_item_count']) for f in feeds},
                         {'通知公告': (7, 7), '新闻动态': (7, 7)})
        notice = next(f for f in feeds if f['name'] == '通知公告')
        self.assertEqual(notice['column_url'], 'https://www.cupk.edu.cn/jwb/tzgg/')
        self.assertEqual(notice['latest_publication'], '2026-09-17')
        self.assertIn('全国大学英语四、六级考试报名通知', notice['samples'][0]['title'])

    def test_opaque_classes_use_only_the_single_adjacent_title_with_own_more_link(self):
        feeds = publication_lists(self.fixture('bfsu'), 'https://jwc.bfsu.edu.cn/')
        self.assertEqual({f['name'] for f in feeds}, {'教务信息', '通知公告'})
        self.assertEqual({f['column_url'] for f in feeds}, {'https://jwc.bfsu.edu.cn/index/jwxx2.htm',
                                                          'https://jwc.bfsu.edu.cn/index/tzgg.htm'})
        self.assertTrue(all(f['dated_item_count'] == 0 and f['latest_publication'] is None for f in feeds))

    def test_icon_font_and_more_plus_are_not_part_of_column_title(self):
        feeds = publication_lists(self.fixture('cqmu'), 'https://jwc.cqmu.edu.cn/')
        notice = next(f for f in feeds if f['name'] == '教务通知')
        self.assertEqual(notice['column_url'], 'https://jwc.cqmu.edu.cn/xwtz/jwtz.htm')
        self.assertEqual(notice['dated_item_count'], 6)
        self.assertTrue(all(f['publication_dates_complete'] for f in feeds))

    def test_broken_heading_does_not_borrow_neighbor_widget(self):
        html = '<div><div><ul><li><a href="/info/1001/1234.htm">关于课程选课的通知</a></li></ul></div>'
        html += '<section><h2>另一栏目</h2><ul><li><a href="/info/1001/1235.htm">别的消息通知</a></li></ul></section></div>'
        soup = BeautifulSoup(html, 'lxml')
        result = heading_evidence(soup.select_one('li'), 'https://example.edu.cn/')
        self.assertEqual(result['name'], '')
        self.assertEqual(result['column_url'], '')

    def test_two_title_links_remain_ambiguous(self):
        soup = BeautifulSoup('<section><div class="head"><a href="a.htm">学生通知</a><a href="b.htm">教师通知</a></div>'
                             '<div><ul><li><a href="/info/1/1234.htm">关于选课的公告</a></li></ul></div></section>', 'lxml')
        result = heading_evidence(soup.li, 'https://example.edu.cn/')
        self.assertTrue(result['heading_ambiguous'])
        self.assertEqual(result['column_url'], '')

    def test_month_day_before_explicit_year_is_read_without_guessing(self):
        soup = BeautifulSoup('<div class="time"><p class="d">09-20</p><p>2026</p></div>', 'lxml')
        self.assertEqual(publication_date_text(soup.div), '2026-09-20')
        soup.p.decompose()
        self.assertEqual(publication_date_text(soup.div), '2026')

    def test_prose_dates_and_competing_event_dates_are_not_publication_dates(self):
        for body in ('<p class="summary">活动开始于<span class="date">2026-09-12</span></p>',
                     '<div class="start-date">2026-09-12</div><div class="end-date">2026-09-14</div>',
                     '<span>09-12</span>'):
            soup = BeautifulSoup('<ul>' + ''.join('<li><a href="/info/1/' + str(i) + '.htm">课程选课工作通知</a>' + body + '</li>' for i in range(3)) + '</ul>', 'lxml')
            self.assertEqual(infer_publication_date_selector(soup.select('li')), '')

    def test_inferred_selector_is_reusable_by_actual_list_scraper(self):
        from unittest.mock import patch
        from backend import create_app
        from backend.database.db import db
        from backend.database.models import School, Department, Announcement
        from backend.scraper.engine import _process_announcement_item
        html = self.fixture('cupk')
        notice = next(f for f in publication_lists(html, 'https://www.cupk.edu.cn/jwb/') if f['name'] == '通知公告')
        app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:'})
        with app.app_context():
            db.create_all()
            school = School(name='测试学校', url='https://www.cupk.edu.cn/')
            db.session.add(school); db.session.flush()
            dept = Department(school_id=school.id, name=notice['name'], list_url='https://www.cupk.edu.cn/jwb/',
                **{k: notice.get(k, '') for k in ('list_selector', 'title_selector', 'link_selector', 'date_selector')})
            db.session.add(dept); db.session.flush()
            with patch('backend.scraper.engine._fetch_html', side_effect=AssertionError('List ingestion must not fetch article bodies')):
                for item in BeautifulSoup(html, 'lxml').select(notice['list_selector']):
                    self.assertTrue(_process_announcement_item(item, dept, school.url))
            db.session.commit()
            articles = Announcement.query.all()
            self.assertEqual(len(articles), 7)
            self.assertTrue(all(a.published_at for a in articles))
            self.assertIn('全国大学英语四、六级考试报名通知', articles[0].title)
            self.assertTrue(all(not a.content_html for a in articles))
            db.session.remove(); db.drop_all()



if __name__ == '__main__':
    unittest.main()
