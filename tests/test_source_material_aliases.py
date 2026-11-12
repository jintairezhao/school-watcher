"""CMS entry aliases require matching original pages, never copied metadata."""
from copy import deepcopy
import hashlib
import json
import unittest

from backend.scraper.discovery.publication_lists import publication_lists
from backend.services.source_relationships import SourceRelationships,material_source_addresses,observe_source_page_aliases
from backend.services.structure_facts import build_structure_facts,CONFIG_FIELDS,_digest

ROOT='https://school.example.edu.cn/'
CANONICAL=ROOT+'college/notices/'
CMS=ROOT+'zcms/catalog/42/pc/index_1.shtml'

def page_html(alias=False):
    rows=''.join(f'<li class="middleArticle--articleList"><a class="middleArticle__articleList--article" '
        f'href="/college/c/2026-10-30/{100000+i}.shtml">关于本学期校园事务安排的通知{i}</a>'
        '<span class="middleArticle__articleList--date">2026-10-30</span></li>'for i in range(3))
    pager=("<script>function page(PageIndex){return '"+ROOT+
        "zcms/catalog/42/pc/index_{PageIndex}.shtml'.replace('{PageIndex}',PageIndex);}</script>")if alias else''
    return ('<title>示例大学示例学院</title><div class="c-main__right">'
        '<div class="middleArticle__position"><span class="middleArticle__position--label">'
        f'<a href="{ROOT}">首页</a><a href="{CANONICAL}">通知公告</a></span></div>'
        f'<div class="middleArticle__art"><ul class="middleArticle__articleList">{rows}</ul></div>{pager}</div>')


class MaterialSourceAliasTests(unittest.TestCase):
    def setUp(self):
        self.materials={CANONICAL:page_html(),CMS:page_html(True)}
        self.pages=[]
        for url,html in self.materials.items():
            self.pages.append(dict(url=url,state='fetched',content_hash=hashlib.sha256(html.encode()).hexdigest(),
                checked_at='2026-10-30T03:00:00+00:00',title='示例大学示例学院',
                feed_json=json.dumps(dict(lists=publication_lists(html,url)),ensure_ascii=False)))
        self.site=dict(site_key='school',root_url=ROOT,name='示例大学')
        self.relations=SourceRelationships(dict(site=self.site,pages=self.pages),[])
        self.assertIn(CANONICAL,self.relations._addresses(CMS))

    def resolve(self,materials=None,relations=None):
        return material_source_addresses(relations or self.relations,CMS,(materials or self.materials).get)

    def test_both_original_lists_replay_the_existing_cms_alias(self):
        self.assertEqual(self.resolve(),{CMS,CANONICAL})

    def test_missing_changed_or_unobserved_original_does_not_use_saved_alias(self):
        for change in('missing','changed','unobserved'):
            with self.subTest(change=change):
                materials=dict(self.materials);relations=self.relations
                if change=='missing':materials.pop(CMS)
                elif change=='changed':materials[CMS]+='changed'
                else:
                    pages=deepcopy(self.pages);next(p for p in pages if p['url']==CMS)['checked_at']=None
                    relations=SourceRelationships(dict(site=self.site,pages=pages),[])
                self.assertEqual(self.resolve(materials,relations),{CMS})

    def binding_inputs(self):
        config=dict.fromkeys(CONFIG_FIELDS,'')
        config.update(name='通知公告',list_url=CMS,list_selector='li.middleArticle--articleList',group_name='示例学院')
        source=dict(config,id=76,school_id=1,kind='column')
        receipt=dict(source_id=76,url=CMS,config_hash=_digest(config),receipt_id='proposal:76')
        return source,receipt

    def observe_bindings(self,source=None,receipt=None,materials=None,pages=None):
        original_source,original_receipt=self.binding_inputs()
        source=original_source if source is None else source
        receipt=original_receipt if receipt is None else receipt
        indexed={p['url']:p for p in(pages or self.pages)}
        return observe_source_page_aliases(site=dict(self.site,id=1),departments=[source],
            receipts=[]if receipt is False else[receipt],
            metadata=lambda urls:{url:indexed[url]for url in urls if url in indexed},
            material=(self.materials if materials is None else materials).get)

    def test_shared_facts_use_the_proven_alias_without_a_click_or_source_merge(self):
        source,receipt=self.binding_inputs();aliases=self.observe_bindings()
        self.assertEqual(len(aliases),1)
        other=dict(source,id=13,list_url=CANONICAL)
        facts=build_structure_facts(site=dict(self.site,id=1),pages=self.pages,structure_nodes=[],navigation_edges=[],
            departments=[source,other],directory_entries=[],receipts=[receipt],source_page_aliases=aliases)
        bindings=[e for e in facts['relations']if e.get('observed_relation')=='validated_listing_alias']
        self.assertEqual(len(bindings),1)
        self.assertEqual(bindings[0]['type'],'source_on_page')
        self.assertEqual(bindings[0]['target'],'source:76')
        pages={n['id']:n['url']for n in facts['nodes']if n['entity_type']=='page'}
        self.assertEqual(pages[bindings[0]['source']],CANONICAL)
        self.assertEqual({p['reference_url']for p in bindings[0]['evidence']},{CMS,CANONICAL})
        self.assertEqual({n['id']for n in facts['nodes']if n['entity_type']=='source'},{'source:13','source:76'})
        self.assertFalse(any(e['type']=='navigation_link'for e in facts['relations']))

    def test_no_current_receipt_or_changed_config_cannot_project_the_alias(self):
        source,receipt=self.binding_inputs()
        self.assertFalse(self.observe_bindings(receipt=False))
        self.assertFalse(self.observe_bindings(source=dict(source,list_selector='another li')))
        self.assertFalse(self.observe_bindings(receipt=dict(receipt,config_hash='wrong')))

    def protocol_bindings(self,change=None):
        http=CANONICAL.replace('https:','http:',1)
        source,_=self.binding_inputs()
        sources=[dict(source,id=21,list_url=http),dict(source,id=1440,list_url=CANONICAL)]
        pages=[];materials={}
        for item in sources:
            url=item['list_url'];html=page_html().replace('href="'+CANONICAL+'"','href="/college/notices/"')
            page=dict(url=url,state='fetched',title='示例大学示例学院',checked_at='2026-10-30T03:00:00+00:00',
                content_hash=hashlib.sha256(html.encode()).hexdigest(),
                feed_json=json.dumps(dict(lists=publication_lists(html,url)),ensure_ascii=False))
            pages.append(page);materials[url]=html
        if change=='missing':materials.pop(http)
        elif change=='unobserved':pages[0]['checked_at']=None
        elif change=='wrong_original':materials[http]+='changed'
        elif change=='different_path':
            old=sources[0]['list_url'];new=old+'other/'
            sources[0]['list_url']=new;pages[0]['url']=new;materials[new]=materials.pop(old)
        receipts=[dict(source_id=s['id'],url=s['list_url'],config_hash=_digest({f:s.get(f)or''for f in CONFIG_FIELDS}),
                       receipt_id='native:'+str(s['id']))for s in sources]
        indexed={p['url']:p for p in pages}
        return observe_source_page_aliases(site=dict(self.site,id=1),departments=sources,receipts=receipts,
            metadata=lambda urls:{u:indexed[u]for u in urls if u in indexed},material=materials.get)

    def test_independently_recorded_protocol_lists_share_proof_without_merging_source_ids(self):
        bindings=self.protocol_bindings()
        self.assertEqual({(b['source_id'],b['alias_url'])for b in bindings},
            {(21,CANONICAL),(1440,CANONICAL.replace('https:','http:',1))})
        self.assertTrue(all({r['url']for r in b['references']}=={CANONICAL,CANONICAL.replace('https:','http:',1)}
            for b in bindings))

    def test_protocol_spelling_never_replaces_two_observed_originals(self):
        for change in('missing','unobserved','wrong_original','different_path'):
            with self.subTest(change=change):self.assertFalse(self.protocol_bindings(change))

    def test_binding_rechecks_originals_instead_of_reusing_saved_alias_metadata(self):
        for change in('missing','changed','no_pager','unobserved'):
            with self.subTest(change=change):
                materials=dict(self.materials);pages=deepcopy(self.pages)
                if change=='missing':materials.pop(CMS)
                elif change=='changed':materials[CMS]+='changed'
                elif change=='unobserved':next(p for p in pages if p['url']==CMS)['checked_at']=None
                else:
                    html=materials[CMS];materials[CMS]=html[:html.index('<script>')]+html[html.index('</script>')+9:]
                    next(p for p in pages if p['url']==CMS)['content_hash']=hashlib.sha256(materials[CMS].encode()).hexdigest()
                self.assertFalse(self.observe_bindings(materials=materials,pages=pages))

    def test_fact_projection_rejects_a_stale_alias_reference_or_wrong_receipt(self):
        source,receipt=self.binding_inputs();alias=self.observe_bindings()[0]
        for change in('wrong_receipt','stale_reference','missing_alias_locator'):
            with self.subTest(change=change):
                changed=deepcopy(alias)
                if change=='wrong_receipt':changed['receipt_id']='proposal:another'
                elif change=='stale_reference':changed['references'][0]['content_hash']='a'*64
                else:next(r for r in changed['references']if r['url']==CANONICAL)['locators']=[]
                facts=build_structure_facts(site=dict(self.site,id=1),pages=self.pages,structure_nodes=[],navigation_edges=[],
                    departments=[source],directory_entries=[],receipts=[receipt],source_page_aliases=[changed])
                self.assertFalse(any(e.get('observed_relation')=='validated_listing_alias'for e in facts['relations']))

    def test_same_name_or_cross_link_cannot_replace_actual_pager_and_shared_entries(self):
        for change in('no_pager','other_entries','other_brand'):
            with self.subTest(change=change):
                materials=dict(self.materials);html=materials[CMS]
                if change=='no_pager':html=html[:html.index('<script>')]+html[html.index('</script>')+9:]
                elif change=='other_entries':html=html.replace('/college/c/','/another/c/')
                else:html=html.replace('示例大学示例学院','示例大学另一学院')
                materials[CMS]=html
                # Even metadata asserting the old alias with a current hash
                # cannot bypass re-execution of the actual retained DOM.
                pages=deepcopy(self.pages);next(p for p in pages if p['url']==CMS)['content_hash']=hashlib.sha256(html.encode()).hexdigest()
                relations=SourceRelationships(dict(site=self.site,pages=pages),[])
                self.assertIn(CANONICAL,relations._addresses(CMS))
                self.assertEqual(self.resolve(materials,relations),{CMS})
