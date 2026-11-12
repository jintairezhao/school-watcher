"""Evidence capture must keep the observed addresses its own paths rely upon."""
from copy import deepcopy
import json
import unittest

from backend.services.source_governance import _identity_observations
from backend.services.source_inventory import site_key
from backend.services.source_relationships import SourceRelationships

ROOT='https://www.example.edu.cn/'
REQUEST='http://www.example.edu.cn/office'
RESPONSE=REQUEST+'/'
HTTPS='https://www.example.edu.cn/office/'


class IdentityAliasCaptureTests(unittest.TestCase):
    def report(self):
        def page(url,final,marker,feed=False):
            listing=dict(name='通知公告',heading_locator='h2',container_locator='ul',
                samples=[dict(url=final+'a.htm'),dict(url=final+'b.htm')])
            return dict(url=url,final_url=final,title='测试大学办公室',label='办公室',kind='unit',state='fetched',
                health='healthy',content_hash=marker*64,checked_at='2026-11-03T09:00:00+00:00',
                feed_json=json.dumps(dict(lists=[listing])) if feed else None,notes_json='[]')
        return dict(site=dict(site_key=site_key(ROOT),name='测试大学',root_url=ROOT),pages=[
            page(ROOT,ROOT,'a'),page(REQUEST,RESPONSE,'b',True),page(HTTPS,HTTPS,'c',True),
            page(ROOT+'unrelated/',ROOT+'unrelated/','d')])

    def paths(self):
        return [dict(nodes=[dict(kind='unit',url=REQUEST)],references=[dict(url=ROOT),dict(url=HTTPS)])]

    def test_observed_protocol_and_redirect_witnesses_survive_capture(self):
        report=self.report();live=SourceRelationships(report,[])
        self.assertIn(HTTPS,live._addresses(REQUEST))
        captured=_identity_observations(report,self.paths())
        self.assertEqual({p['url'] for p in captured['identity_pages']},{ROOT,REQUEST,HTTPS})
        restored=SourceRelationships(dict(site=captured['identity_site'],pages=captured['identity_pages']),[])
        self.assertEqual(restored._addresses(REQUEST),live._addresses(REQUEST))
        original=next(p for p in report['pages'] if p['url']==REQUEST)
        retained=next(p for p in captured['identity_pages'] if p['url']==REQUEST)
        self.assertEqual((retained['content_hash'],retained['checked_at'],retained['final_url']),
                         (original['content_hash'],original['checked_at'],RESPONSE))

    def test_capture_does_not_invent_an_unobserved_protocol_page(self):
        report=self.report();report['pages']=[p for p in report['pages'] if p['url']!=HTTPS]
        paths=deepcopy(self.paths());paths[0]['references']=[dict(url=ROOT),dict(url=REQUEST)]
        captured=_identity_observations(report,paths)
        self.assertEqual({p['url'] for p in captured['identity_pages']},{ROOT,REQUEST})
        restored=SourceRelationships(dict(site=captured['identity_site'],pages=captured['identity_pages']),[])
        self.assertNotIn(HTTPS,restored._addresses(REQUEST))

    def test_newer_failed_observation_is_preserved_without_restoring_an_old_alias(self):
        report=self.report();failed=next(p for p in report['pages'] if p['url']==REQUEST)
        failed.update(state='failed',health='unreachable',final_url=RESPONSE,
                      checked_at='2026-11-03T10:00:00+00:00')
        captured=_identity_observations(report,self.paths())
        retained=next(p for p in captured['identity_pages'] if p['url']==REQUEST)
        self.assertEqual((retained['state'],retained['checked_at']),('failed',failed['checked_at']))
        restored=SourceRelationships(dict(site=captured['identity_site'],pages=captured['identity_pages']),[])
        self.assertNotIn(HTTPS,restored._addresses(REQUEST))


if __name__=='__main__':unittest.main()
