"""A roster-listed union work website is not an arbitrary topic alias."""
from copy import deepcopy
from types import SimpleNamespace
import hashlib
import json
import unittest

from backend.scraper.discovery.structure import extract_structure
from backend.services.school_structure import _publisher_material_assertion, _publisher_assertion
from backend.services.source_relationships import SourceRelationships


class UnionWorkWebsiteBindingTests(unittest.TestCase):
    root = 'https://school.example/'
    directory = root + 'org/'
    home = root + 'union/'
    target = home + 'notices/'

    def context(self, label='工会工作', title='示例大学工会工作', table=True):
        row = ('<table><tr><th>单位名称</th><th>挂靠单位</th><th>备注</th></tr>'
               '<tr><td><a href="/party/">党群工作部</a></td><td><p>工会</p></td>'
               f'<td><a href="/union/">{label}</a></td></tr></table>')
        if not table:
            row = '<nav><a href="/union/">工会工作</a></nav>'
        materials = {
            self.directory: '<html><head><title>示例大学</title></head><body><main>' + row + '</main></body></html>',
            self.home: f'<html><head><title>{title}</title></head><body><nav>'
                '<a href="/union/notices/">通知公告</a></nav></body></html>',
            self.target: f'<html><head><title>{title}</title></head><body><main>通知公告</main></body></html>',
        }
        pages, records = [], []
        for url, html in materials.items():
            kind = 'directory' if url == self.directory else 'unit' if url == self.home else 'channel'
            parsed = extract_structure(html, url, self.root, kind, '组织机构' if kind == 'directory' else '')
            digest = hashlib.sha256(html.encode()).hexdigest()
            pages.append(dict(url=url, final_url=url, title=parsed['title'], kind=kind,
                state='fetched', content_hash=digest, checked_at='2026-10-30T00:00:00+00:00',
                notes_json=json.dumps(parsed['notes']), feed_json=''))
            for node in parsed['nodes']:
                records.append(dict(node_key=node['key'], parent_key=node['parent'],
                    **{k: node[k] for k in ('name', 'kind', 'url', 'locator', 'relation')},
                    reference_url=url, content_hash=digest, observed_at='2026-10-30T00:00:00+00:00'))
        report = dict(site=dict(site_key='test-school', name='示例大学', root_url=self.root), pages=pages)
        return SourceRelationships(report, records, directory_navigation=True), materials

    def witness(self, relationships, materials, path):
        return _publisher_material_assertion(path, SimpleNamespace(list_url=self.target),
            lambda url: materials.get(url, ''), relationships)

    def test_named_union_keeps_its_affiliation_and_uses_the_same_row_work_website(self):
        relations, materials = self.context()
        paths = relations.paths_for(self.target)
        self.assertEqual({p['unit_name'] for p in paths}, {'工会'})
        self.assertTrue(any(self.witness(relations, materials, p) for p in paths))
        self.assertTrue(all(p['nodes'][-1]['relation'] == 'attached_unit' for p in paths))
        self.assertTrue(all(p['nodes'][-1]['name'] == '工会' and not p['nodes'][-1]['url'] for p in paths))
        self.assertTrue(all(p['website_url'] == self.home for p in paths))
        self.assertFalse(_publisher_assertion(paths[0], SimpleNamespace(list_url=self.target),
            paths[0]['nodes'][-1], None, lambda url: materials.get(url, ''), relations),
            'A website identity cannot replace a current source receipt')

    def test_topic_links_and_body_mentions_cannot_supply_work_website_identity(self):
        for label, title in [('工会活动', '示例大学工会工作'), ('工会工作', '教职工活动新闻')]:
            with self.subTest(label=label, title=title):
                relations, _ = self.context(label=label, title=title)
                self.assertFalse(relations.paths_for(self.target))

    def test_ordinary_navigation_cannot_replace_the_explicit_roster_row(self):
        relations, _ = self.context(table=False)
        self.assertFalse(relations.paths_for(self.target))

    def test_writer_rejects_tampered_work_label_or_binding_form(self):
        relations, materials = self.context()
        for field, value in [('entry_label', '工会活动'), ('identity_form', None)]:
            path = deepcopy(relations.paths_for(self.target)[0])
            path['website_binding'][field] = value
            self.assertIsNone(self.witness(relations, materials, path))

    def test_writer_rejects_changed_directory_original(self):
        relations, materials = self.context()
        materials[self.directory] = materials[self.directory].replace('工会工作', '工会活动')
        self.assertFalse(any(self.witness(relations, materials, p) for p in relations.paths_for(self.target)))


if __name__ == '__main__':
    unittest.main()
