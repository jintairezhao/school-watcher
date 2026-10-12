"""Observed date fields must not consume the year belonging to a notice title."""
import unittest

from backend.scraper.detectors.title_quality import clean_title


class NoticeTitlePrefixTests(unittest.TestCase):
    def test_real_zju_joined_date_and_title_keep_title_year(self):
        expected = '2026年本科生招生专业目录'
        # Literal texts from the retained real a element: governance separates
        # descendants, while daily collection concatenates their text nodes.
        for observed in ('06-15 2026 2026年本科生招生专业目录',
                         '06-1520262026年本科生招生专业目录'):
            with self.subTest(observed=observed):
                self.assertEqual(clean_title(observed), expected)

    def test_iso_date_cannot_eat_the_next_title_year(self):
        self.assertEqual(clean_title('2026-06-15 2026年招生安排'), '2026年招生安排')

    def test_year_and_academic_year_range_are_part_of_titles(self):
        for title in ('2026年工作要点', '2026-2027学年奖学金申请通知',
                      '2026/2027年度工作安排'):
            with self.subTest(title=title):
                self.assertEqual(clean_title(title), title)

    def test_month_day_prefix_cannot_consume_academic_year_range(self):
        for title in ('2026-2027学年奖学金申请通知', '2026/2027年度工作安排',
                      '2026—2027学年课程安排', '2026年工作要点',
                      '2026届毕业生就业安排', '2026级新生课程安排'):
            for prefix in ('06-15 ', '06-15'):
                with self.subTest(title=title, prefix=prefix):
                    self.assertEqual(clean_title(prefix + title), title)

    def test_invalid_calendar_prefix_is_not_removed(self):
        title = '2026-02-30实验编号使用说明'
        self.assertEqual(clean_title(title), title)

    def test_date_noise_and_view_count_still_clean(self):
        self.assertEqual(clean_title('2026/6/15 | 关于课程安排的通知 浏览数：12'),
                         '关于课程安排的通知')
        self.assertEqual(clean_title('06-15 关于课程安排的通知'), '关于课程安排的通知')


if __name__ == '__main__':
    unittest.main()
