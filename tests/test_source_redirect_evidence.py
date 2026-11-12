"""A witnessed source redirect survives validation, receipts and publication."""
import unittest

from backend.database.db import db
from backend.services import source_governance as governance
from backend.services.source_path_receipts import _material, load_source_receipts


class SourceRedirectEvidenceTests(unittest.TestCase):
    def test_school_root_without_trailing_slash_reuses_the_real_catalogue_home(self):
        from tests.test_source_governance import SourceGovernanceTests
        from backend.scraper.discovery.inventory_crawler import inspect_page
        from backend.services.source_inventory import site_key
        from backend.services.source_workflow import publisher_proven
        fixture=SourceGovernanceTests();fixture.setUp()
        try:
            root=fixture.school.url
            key=site_key(root)
            with fixture.identity.connect() as connection:
                connection.execute("UPDATE pages SET kind='root',label=? WHERE site_key=? AND url=?",
                    (fixture.school.name,key,root))
            site=fixture.identity.report(key)['site']
            inspect_page(fixture.identity,site,fixture.identity.get_page(key,root),fetcher=lambda url:dict(
                url=url,status=200,html='<title>示例大学</title><nav><a href="/departments/">院系设置</a></nav>'))
            inspect_page(fixture.identity,site,fixture.identity.get_page(key,root+'departments/'),fetcher=lambda url:dict(
                url=url,status=200,html='<main><h4><a href="https://college.example.edu.cn/">示例学院</a></h4></main>'))
            fixture.school.url=root.rstrip('/')
            db.session.commit()
            base=fixture.fetcher('<title>示例学院通知公告</title>'+fixture.html())
            def fetch(url,purpose):
                self.assertNotEqual(url.rstrip('/'),root.rstrip('/'),'School home is already retained')
                return base(url,purpose)
            config=governance.source_config(fixture.dept)
            evidence=governance.capture_source_evidence(fixture.school.id,config,department_id=fixture.dept.id,
                inventory=fixture.identity,fetcher=fetch)
            self.assertTrue(publisher_proven(evidence.bundle,config))
        finally:
            fixture.tearDown()

    def test_actual_capture_activation_keeps_requested_identity_and_publisher(self):
        from tests.test_source_governance import SourceGovernanceTests
        from backend.scraper.acquisition import FetchResult, FetchedHTML
        from backend.services.runtime_catalog import RuntimeCatalog
        from backend.services.source_inventory import site_key
        from backend.services.structure_snapshot import read_structure
        fixture = SourceGovernanceTests()
        fixture.setUp()
        try:
            from backend.scraper.discovery.inventory_crawler import inspect_page
            key = site_key(fixture.school.url)
            with fixture.identity.connect() as connection:
                connection.execute("UPDATE pages SET kind='root',label=? WHERE site_key=? AND url=?",
                                   (fixture.school.name, key, fixture.school.url))
            site = fixture.identity.report(key)['site']
            inspect_page(fixture.identity, site, fixture.identity.get_page(key, fixture.school.url), fetcher=lambda url: dict(
                url=url, status=200, html='<title>示例大学</title><nav><a href="/departments/">院系设置</a></nav>'))
            directory = fixture.identity.get_page(key, fixture.school.url + 'departments/')
            inspect_page(fixture.identity, site, directory, fetcher=lambda url: dict(
                url=url, status=200, html='<main><h4><a href="https://college.example.edu.cn/">示例学院</a></h4></main>'))
            requested = 'http://college.example.edu.cn/notices/'
            final = 'https://college.example.edu.cn/notices/'
            fixture.dept.list_url = requested
            db.session.commit()
            original_id = fixture.dept.id
            config = governance.source_config(fixture.dept)
            base = fixture.fetcher('<title>示例学院通知公告</title>' + fixture.html())
            calls = []
            def fetch(url, purpose):
                calls.append((url, purpose))
                response = final if purpose in ('list', 'independent_list') else url
                return FetchedHTML(FetchResult(response, status=200, html=base(url, purpose), outcome='usable'))
            evidence = governance.capture_source_evidence(fixture.school.id, config,
                department_id=original_id, inventory=fixture.identity, fetcher=fetch,
                seed_html=FetchedHTML(FetchResult(final, status=200,
                    html=base(final, 'list'), outcome='usable')))
            proposal = governance.propose_source(fixture.school.id, config, evidence, department_id=original_id)
            self.assertTrue(governance.validate_proposal(proposal.id)['passed'])
            source = governance.activate_proposal(proposal.id)
            self.assertEqual(source.id, original_id)
            self.assertEqual(source.list_url, requested)
            receipts = load_source_receipts(dict(name=fixture.school.name, root_url=fixture.school.url))
            self.assertEqual([r['source_id'] for r in receipts], [original_id])
            self.assertEqual(receipts[0]['url'], requested)
            self.assertEqual(evidence.bundle['list']['requested_url'], requested)
            self.assertEqual(evidence.bundle['list']['url'], final)
            self.assertEqual(evidence.bundle['independent']['requested_url'], requested)
            catalog = RuntimeCatalog(fixture.app.config['SOURCE_CATALOG_PATH']).pin(site_key(fixture.school.url))
            page = catalog.get_page(site_key(fixture.school.url), requested)
            self.assertEqual(page['final_url'], final)
            capture = read_structure(fixture.school)
            self.assertTrue(any(r['type'] == 'source_ownership' and r['target'] == 'source:' + str(original_id)
                                for r in capture['facts']['relations']))
            self.assertEqual([purpose for _, purpose in calls].count('list'), 1)
            self.assertEqual([purpose for _, purpose in calls].count('independent_list'), 1)
        finally:
            fixture.tearDown()

    def test_protocol_similarity_or_failed_response_does_not_prove_redirect(self):
        requested = 'http://college.example.edu.cn/notices/'
        reference = dict(url='https://college.example.edu.cn/notices/', hash='a' * 64,
                         outcome='usable', status=200)
        region = dict(container='#notices', heading='#notices h2')
        self.assertIsNone(_material(reference, region, requested))
        self.assertIsNone(_material(dict(reference, requested_url=requested + 'other/'), region, requested))
        self.assertEqual(_material(dict(reference, requested_url=requested), region, requested), 'a' * 64)
        for status in (None, 0, 302, 403, 500, True):
            with self.subTest(status=status):
                self.assertIsNone(_material(dict(reference, requested_url=requested, status=status), region, requested))


if __name__ == '__main__':
    unittest.main()
