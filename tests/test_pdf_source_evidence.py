"""Typed PDF proof survives public review and refuses missing or changed bytes."""
from pathlib import Path
import hashlib
import json
import os
import time
import unittest

from backend.database.db import db
from backend.services import source_governance as g
from backend.scraper.acquisition import FetchResult,FetchedHTML
from backend.scraper.acquisition.attachments import pdf_observation,pdf_sample_identity

PAYLOAD=b'%PDF-1.4\nsmall controlled validation fixture\n%%EOF\n'


class PdfSourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        from tests.test_source_governance import SourceGovernanceTests
        self.fixture=SourceGovernanceTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def proposal(self):
        f=self.fixture;pdf=f.dept.list_url+'0610.pdf'
        html=f.html(1).replace('article/0.htm',pdf)
        fetch_html=f.fetcher(html)
        def fetch(url,purpose):
            if purpose=='body':
                attachment=pdf_observation(PAYLOAD,url,{},retain=True)
                return FetchedHTML(FetchResult(url,status=200,attachment=attachment,outcome='usable'))
            return fetch_html(url,purpose)
        evidence=g.capture_source_evidence(f.school.id,g.source_config(f.dept),department_id=f.dept.id,
            seed_html=html,inventory=f.identity,fetcher=fetch)
        return g.propose_source(f.school.id,g.source_config(f.dept),evidence,department_id=f.dept.id),evidence

    def test_complete_pdf_keeps_binary_fingerprint_and_activates_through_public_gate(self):
        proposal,evidence=self.proposal()
        reference=evidence.bundle['articles'][0]
        self.assertEqual(reference['hash'],hashlib.sha256(PAYLOAD).hexdigest())
        self.assertEqual(reference['attachment']['format'],'pdf')
        self.assertEqual(g.read_snapshot(reference),'')
        self.assertTrue(g.validate_proposal(proposal.id)['passed'])
        self.assertEqual(g.activate_proposal(proposal.id).id,self.fixture.dept.id)

    def test_missing_or_changed_pdf_never_becomes_success_after_validation(self):
        for operation in ('missing','changed'):
            with self.subTest(operation=operation):
                proposal,evidence=self.proposal()
                self.assertTrue(g.validate_proposal(proposal.id)['passed'])
                ref=evidence.bundle['articles'][0]
                path=g._evidence_root()/(ref['hash']+'.pdf')
                if operation=='missing':path.unlink()
                else:path.write_bytes(PAYLOAD+b'changed')
                with self.assertRaises(ValueError):g.activate_proposal(proposal.id)
                self.assertEqual(proposal.state,'validated')
                path.write_bytes(PAYLOAD)

    def test_cleanup_protects_referenced_pdf_but_removes_expired_orphan(self):
        proposal,evidence=self.proposal();ref=evidence.bundle['articles'][0]
        protected=g._evidence_root()/(ref['hash']+'.pdf')
        other=pdf_observation(PAYLOAD.replace(b'controlled',b'orphaned'),ref['url'],{},retain=True)
        orphan=g._evidence_root()/(other['sha256']+'.pdf')
        old=time.time()-40*86400
        os.utime(protected,(old,old));os.utime(orphan,(old,old))
        outcome=g.prune_governance_evidence(7,all_cache=True)
        self.assertEqual(outcome['removed'],1)
        self.assertTrue(protected.exists());self.assertFalse(orphan.exists())


class PdfIdentityBoundaryTests(unittest.TestCase):
    def test_numeric_file_key_is_limited_literal_url_identity(self):
        url='https://example.edu.cn/0610.pdf';meta=pdf_observation(PAYLOAD,url,{})
        proof=pdf_sample_identity(meta,'关于研究生入学事项的通知',url,url)
        self.assertTrue(proof['matched']);self.assertTrue(proof['limited_identity'])
        self.assertFalse(pdf_sample_identity(meta,'关于研究生入学事项的通知',url,url+'?other')['matched'])
        self.assertFalse(pdf_sample_identity(dict(meta,verified=False),'关于研究生入学事项的通知',url,url)['matched'])
        wrong=dict(meta,filename='另一份文件.pdf')
        self.assertFalse(pdf_sample_identity(wrong,'关于研究生入学事项的通知',url,url)['matched'])


if __name__=='__main__':unittest.main()
