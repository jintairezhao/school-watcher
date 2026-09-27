"""Evidence picker boundaries using isolated saved HTML, never a live school."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_shared_summaries as summary_fixture
from bs4 import BeautifulSoup
from backend.database.db import db
from backend.database.models import BackgroundTask
from backend.services import source_governance as governance
from backend.services.source_picker import preview, propose_picks, _document

HTML = '''<html><head><script>parent.pwned=true</script><meta http-equiv="refresh" content="0;url=https://attacker.invalid"></head>
<body onload="fetch('https://attacker.invalid')"><img src="https://attacker.invalid/a"><form action="https://attacker.invalid"><input name="x"></form>
<ul id="notices"><li><a href="/info/1">第一条申请通知</a><span>2026-10-01</span></li>
<li><a href="/info/2" onclick="alert(1)">第二条申请通知</a><span>2026-10-02</span></li></ul></body></html>'''


class SourcePickerTests(unittest.TestCase):
    setUp = summary_fixture.SharedSummaryTests.setUp
    tearDown = summary_fixture.SharedSummaryTests.tearDown
    client = summary_fixture.SharedSummaryTests.client

    def proposal(self):
        self.app.config['SOURCE_GOVERNANCE_EVIDENCE_PATH'] = str(Path(self.temp.name) / 'evidence')
        proposal = governance.propose_source(self.ann.school_id, {'name': '学院申请通知',
            'list_url': 'https://example.edu.cn/notices', 'list_selector': 'ul li', 'title_selector': 'a',
            'link_selector': 'a'})
        reference = governance._snapshot(HTML, 'https://example.edu.cn/notices')
        proposal.evidence_json = json.dumps({'list': reference})
        db.session.commit()
        return proposal

    def test_preview_is_inert_and_every_pick_id_is_server_generated(self):
        proposal = self.proposal()
        data = preview(proposal.id)
        soup = BeautifulSoup(data['html'], 'lxml')
        self.assertFalse(soup.select('script,meta,img,form,iframe,style,link'))
        for node in soup.find_all(True):
            self.assertFalse(set(node.attrs) - {'data-pick-id', 'disabled'})
        self.assertNotIn('https://attacker.invalid', data['html'])
        self.assertTrue(soup.select('[data-pick-id]'))

    def test_cross_row_picks_and_changed_evidence_are_rejected(self):
        proposal = self.proposal()
        _, nodes, reference = _document(proposal)
        rows = [key for key, value in nodes.items() if value.name == 'li']
        titles = [key for key, value in nodes.items() if value.name == 'a']
        with patch('backend.services.source_picker.queue_source_review') as queue:
            with self.assertRaisesRegex(ValueError, '同一条'):
                propose_picks(proposal.id, {'evidence_hash': reference['hash'],
                    'picks': {'row': rows[0], 'title': titles[1]}}, self.admin.id)
            with self.assertRaisesRegex(ValueError, '已更新'):
                propose_picks(proposal.id, {'evidence_hash': 'old',
                    'picks': {'row': rows[0], 'title': titles[0]}}, self.admin.id)
            queue.assert_not_called()

    def test_valid_picks_become_proposal_and_never_activate_directly(self):
        proposal = self.proposal()
        soup, nodes, reference = _document(proposal)
        row = next(key for key, value in nodes.items() if value.name == 'li')
        title = next(key for key, value in nodes.items() if value.name == 'a')
        with patch('backend.services.source_picker.queue_source_review', return_value={'state': 'proposed'}) as queue:
            result = propose_picks(proposal.id, {'evidence_hash': reference['hash'],
                'picks': {'row': row, 'title': title}}, self.admin.id)
        config = queue.call_args.args[1]
        self.assertEqual(len(soup.select(config['list_selector'])), 2)
        self.assertEqual(config['date_selector'], '')
        self.assertEqual(result['state'], 'proposed')
        self.assertEqual(BackgroundTask.query.count(), 0)

    def test_picker_requires_admin_and_csrf(self):
        proposal = self.proposal()
        url = f'/api/admin/source-proposals/{proposal.id}/picker'
        self.assertEqual(self.client(self.reader).get(url).status_code, 403)
        self.assertEqual(self.client(self.admin).post(url, json={}).status_code, 403)
        self.assertEqual(self.client(self.admin).get(url).status_code, 200)


if __name__ == '__main__':
    unittest.main()
