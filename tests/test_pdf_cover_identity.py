"""A retained PDF cover supplies evidence that filenames and metadata cannot."""
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from flask import Flask
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
from backend.scraper.acquisition.attachments import pdf_observation,pdf_sample_identity
from backend.scraper.acquisition.pdf_identity import cover_title

def document(lines,metadata_title='',encrypted=False):
    writer=PdfWriter();page=writer.add_blank_page(width=600,height=800)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject()
    escape=lambda s:s.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')
    stream.set_data(('BT /F1 18 Tf 60 740 Td '+''.join('('+escape(line)+') Tj 0 -24 Td 'for line in lines)+'ET').encode('ascii'))
    page[NameObject('/Contents')]=writer._add_object(stream)
    if metadata_title:writer.add_metadata({'/Title':metadata_title})
    if encrypted:writer.encrypt('not-a-public-document')
    result=BytesIO();writer.write(result);return result.getvalue()

class PdfCoverIdentityTests(unittest.TestCase):
    def test_split_cover_title_has_a_replayable_page_locator(self):
        proof=cover_title(document(['Research platform','User guide','(Finance)']),'Research platform User guide (Finance)')
        self.assertTrue(proof['matched']);self.assertEqual(proof['locator'],'pdf:page:1:nonempty-lines:1-3')

    def test_body_mentions_and_metadata_cannot_replace_the_cover(self):
        for payload in(document(['Unrelated report','This report mentions','Research platform User guide']),document(['Different cover'],'Research platform User guide')):
            self.assertFalse(cover_title(payload,'Research platform User guide')['matched'])

    def test_blank_encrypted_and_malformed_files_remain_unconfirmed(self):
        for payload in(document([]),document(['Research platform User guide'],encrypted=True),b'%PDF-1.7\nbroken\n%%EOF'):
            self.assertFalse(cover_title(payload,'Research platform User guide')['matched'])

    def test_gate_rechecks_bytes_and_ignores_injected_cover_metadata(self):
        with tempfile.TemporaryDirectory()as folder:
            app=Flask(__name__);app.config['SOURCE_GOVERNANCE_EVIDENCE_PATH']=folder
            with app.app_context():
                url='https://example.edu.cn/guidea.pdf';title='Research platform User guide'
                payload=document(['Research platform','User guide'])
                attachment=pdf_observation(payload,url,{},retain=True)
                proof=pdf_sample_identity(attachment,title,url,url)
                self.assertTrue(proof['matched']);self.assertFalse(proof['limited_identity'])
                self.assertEqual(proof['body_basis'],'verified_pdf_cover_title')
                path=Path(folder)/(attachment['sha256']+'.pdf')
                path.write_bytes(payload+b'changed')
                self.assertFalse(pdf_sample_identity(dict(attachment,cover_identity={'matched':True}),title,url,url)['matched'])
                path.unlink()
                self.assertFalse(pdf_sample_identity(attachment,title,url,url)['matched'])

if __name__=='__main__':unittest.main()
