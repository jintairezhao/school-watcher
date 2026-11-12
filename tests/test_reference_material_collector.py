"""Boundaries of reference collection; synthetic cases do not measure crawl quality."""
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.checks.collect_reference_materials import Collector, inspect_material, validate_plan


SCHOOL = {'id': 'one', 'name': '示例学校', 'root_url': 'https://one.edu.cn/', 'max_documents': 3}
ENTRY = {'url': SCHOOL['root_url'], 'label': SCHOOL['name'], 'kind': 'root', 'path': []}
FIXTURES = Path(__file__).parent/'fixtures/acquisition'


def analyze(html, school=None, entry=None):
    school = school or SCHOOL
    entry = entry or ENTRY
    return inspect_material(html, entry['url'], school, entry, hashlib.sha256(html.encode()).hexdigest(), '2026-10-23T00:00:00Z')


def plan(schools=None, documents=6, attempts=8):
    return {'schema': 1, 'schools': schools or [SCHOOL], 'max_documents': documents,
        'max_http_attempts': attempts, 'reuse_audit': 'Fixture only, no live requests'}


class ReferenceCollectorTests(unittest.TestCase):
    def test_foreign_college_with_desired_name_is_not_followed(self):
        result = analyze('<nav><a href="https://two.edu.cn/college">马克思主义学院</a>'
                         '<a href="/org">机构设置</a></nav>')
        self.assertEqual([row['url'] for row in result['candidates']], ['https://one.edu.cn/org'])
        self.assertTrue(any(row['decision'] == 'official_external_link' for row in result['links']))

    def test_repeated_navigation_retains_both_proofs_but_fetches_page_once(self):
        html = '<nav><a href="/org">院系设置</a></nav><footer><a href="/org">院系设置</a></footer>'
        calls = []
        def fetch(url):
            calls.append(url)
            return 200, {}, html.encode() if url.endswith('/') else b'<nav><a href="/">home</a></nav>'
        with tempfile.TemporaryDirectory() as parent:
            collector = Collector(plan(), Path(parent)/'run', fetch)
            collector.run()
            proofs = [row for row in collector.links if row['url'] == 'https://one.edu.cn/org']
            self.assertEqual(len(proofs), 2)
            self.assertEqual(len({row['locator'] for row in proofs}), 2)
            self.assertEqual(calls.count('https://one.edu.cn/org'), 1)

    def test_article_looking_real_directory_in_navigation_is_retained(self):
        url = 'https://one.edu.cn/2026/1023/c123a456/page.htm'
        result = analyze(f'<nav><a href="{url}">院系设置</a></nav>')
        self.assertIn(url, [row['url'] for row in result['candidates']])

    def test_actual_upc_material_does_not_queue_proven_articles(self):
        index = json.loads((FIXTURES/'reference-materials-20261023.json').read_text('utf-8'))
        row = next(r for r in index if r['file'] == 'upc-marxism-root-20261023.html.gz')
        raw = gzip.decompress((FIXTURES/row['file']).read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), row['raw_sha256'])
        school = dict(SCHOOL, root_url='https://www.upc.edu.cn/')
        entry = dict(ENTRY, url=row['url'], label='马克思主义学院', kind='unit')
        result = inspect_material(raw.decode('utf-8'), row['url'], school, entry, row['raw_sha256'], row['observed_at'])
        article = 'http://szjyb.upc.edu.cn/2026/1022/c22394a504021/page.htm'
        self.assertNotIn(article, [r['url'] for r in result['candidates']])
        self.assertTrue(result['publication_lists'])
        self.assertTrue(any(article in column['article_urls'] for column in result['publication_lists']))

    def test_access_challenge_is_unknown_and_not_empty(self):
        raw = gzip.decompress((FIXTURES/'ucass-wzws-home-20261023.html.gz').read_bytes())
        result = analyze(raw.decode('utf-8'), dict(SCHOOL, root_url='https://www.ucass.edu.cn/'),
            dict(ENTRY, url='https://www.ucass.edu.cn/'))
        self.assertEqual(result['error_code'], 'access_challenge')
        self.assertEqual(result['semantic_value'], 'unknown')
        self.assertEqual(result['candidates'], [])

    def test_browser_transport_is_preserved_for_an_unchanged_challenge(self):
        raw = gzip.decompress((FIXTURES/'ucass-wzws-home-20261023.html.gz').read_bytes())
        result = inspect_material(raw.decode('utf-8'), 'https://www.ucass.edu.cn/', SCHOOL, ENTRY,
            hashlib.sha256(raw).hexdigest(), '2026-10-23T00:00:00Z', transport='browser')
        self.assertEqual(result['outcome'], 'needs_manual')
        self.assertEqual(result['error_code'], 'access_challenge')
        self.assertEqual(result['candidates'], [])

    def test_round_robin_keeps_schools_separate_and_quota_bounded(self):
        other = dict(SCHOOL, id='two', name='第二校', root_url='https://two.edu.cn/')
        calls = []
        def fetch(url):
            calls.append(url)
            return 200, {}, b'<nav><a href="/org">Departments</a><a href="/admissions">Admissions</a></nav>'
        with tempfile.TemporaryDirectory() as parent:
            collector = Collector(plan([SCHOOL, other], documents=6), Path(parent)/'run', fetch)
            result = collector.run()
            self.assertEqual([r['school_id'] for r in collector.decisions], ['one', 'two']*3)
            self.assertEqual(result['documents_by_school'], {'one': 3, 'two': 3})
            for row in collector.observations:
                expected = 'one.edu.cn' if row['school_id'] == 'one' else 'two.edu.cn'
                self.assertIn(expected, row['requested_url'])
            self.assertEqual(len(calls), 6)

    def test_redirects_consume_budget_and_failure_is_unknown(self):
        def fetch(url):
            return 302, {'Location': '/next' if url.endswith('/') else '/third'}, b'redirect'
        with tempfile.TemporaryDirectory() as parent:
            collector = Collector(plan(attempts=2), Path(parent)/'run', fetch)
            result = collector.run()
            self.assertEqual(result['http_attempts'], 2)
            self.assertEqual(result['documents'], 1)
            self.assertEqual(collector.observations[0]['failure'], 'http_budget_exhausted')
            self.assertEqual(collector.observations[0]['semantic_value'], 'unknown')
            self.assertEqual(len(list(collector.output.glob('*.html.gz'))), 1)

    def test_redirect_to_other_school_is_never_sent(self):
        calls = []
        def fetch(url):
            calls.append(url)
            return 302, {'Location': 'https://two.edu.cn/'}, b''
        with tempfile.TemporaryDirectory() as parent:
            collector = Collector(plan(), Path(parent)/'run', fetch)
            collector.run()
            self.assertEqual(calls, [SCHOOL['root_url']])
            self.assertEqual(collector.observations[0]['failure'], 'redirect_outside_school_scope')

    def test_cannot_load_annotations_or_overwrite_old_run(self):
        with self.assertRaises(ValueError):
            validate_plan(dict(plan(), must_find_targets=['hidden target']))
        with tempfile.TemporaryDirectory() as parent:
            with self.assertRaises(FileExistsError):
                Collector(plan(), parent)


if __name__ == '__main__':
    unittest.main()
