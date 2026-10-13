"""One reader hierarchy for stored memberships and observed website paths."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.services.inbox import source_hierarchy
from backend.services.source_placements import placements_from_paths

ROOT = 'https://example.edu.cn/'

def department(ident, name, url, kind='column', group='院系设置'):
    return SimpleNamespace(id=ident, school_id=1, name=name, list_url=url,
        kind=kind, group_name=group, list_selector='li' if kind == 'column' else '',
        school=SimpleNamespace(url=ROOT, name='示例大学'))

class SourcePresentationConsistencyTests(unittest.TestCase):
    def test_observed_path_and_stored_directory_share_one_college(self):
        unit = department(10, '环境学院', ROOT+'environment/', 'unit')
        old = department(11, '环境学院', ROOT+'environment/notices/')
        new = department(12, '教学通知', ROOT+'environment/teaching/')
        path = {'group':'院系设置', 'nodes':[{'key':'observed-unit', 'name':'环境学院',
            'url':ROOT+'environment/'}], 'label':'教学通知'}
        entries = [SimpleNamespace(parent_id=10, department_id=11, position=0)]
        with patch('backend.services.source_placements.official_source_placements', return_value={12:[path]}):
            tree = source_hierarchy([unit,old,new], directory_entries=entries)
        colleges = tree['院系设置']
        self.assertEqual(len(colleges), 1)
        self.assertEqual({c['department'].id for c in colleges[0]['columns']}, {11,12})
        self.assertEqual(colleges[0]['department_id'], 10)

    def test_actual_column_and_local_navigation_replace_department_placeholder(self):
        unit = {'kind':'unit', 'name':'教务部', 'url':ROOT+'education/', 'relation':'directory_entry'}
        group = {'kind':'group','name':'组织机构','relation':'page_identity'}
        path = {'basis':'official_website_entry','nodes':[group,unit],
            'entry_names':['通知公告'], 'entry_nodes':[{'kind':'group','name':'教学运行'}]}
        item = department(11,'教务部-教学运行',ROOT+'education/teaching/notices/',group='组织机构')
        result = placements_from_paths(item, ROOT, [path], {'组织机构'})
        self.assertEqual(result[0]['label'], '教学运行 / 通知公告')
        item.name = '教务部'
        path['entry_nodes'] = []
        self.assertEqual(placements_from_paths(item,ROOT,[path],{'组织机构'})[0]['label'],'通知公告')

    def test_equal_names_at_distinct_official_entrances_are_not_combined(self):
        a = department(10,'研究生院',ROOT+'campus-a/', 'unit')
        b = department(20,'研究生院',ROOT+'campus-b/', 'unit')
        columns = [department(11,'通知公告',ROOT+'campus-a/notices/'),
                   department(21,'通知公告',ROOT+'campus-b/notices/')]
        entries = [SimpleNamespace(parent_id=10,department_id=11,position=0),
                   SimpleNamespace(parent_id=20,department_id=21,position=0)]
        with patch('backend.services.source_placements.official_source_placements',return_value={}):
            tree=source_hierarchy([a,b,*columns],directory_entries=entries)
        self.assertEqual(len(tree['院系设置']),2)
        self.assertEqual({u['department_id'] for u in tree['院系设置']},{10,20})

    def test_observed_publisher_reuses_directory_group_and_qualified_identity(self):
        unit=department(10,'环境学院',ROOT+'environment/','unit')
        column=department(11,'教学通知',ROOT+'environment/teaching/',group='错误的旧分组')
        publisher={'publisher_key':'site-observed','publisher_label':'示例大学环境学院',
            'column_label':'教学通知','units':[{'key':'site-observed','name':'示例大学环境学院',
                                              'url':ROOT+'environment/'}]}
        with patch('backend.services.source_placements.official_source_placements',return_value={}), \
             patch('backend.services.source_channels.channels_for',return_value={11:publisher}):
            tree=source_hierarchy([unit,column])
        self.assertEqual(set(tree),{'院系设置'})
        self.assertEqual(tree['院系设置'][0]['name'],'环境学院')
        self.assertEqual(tree['院系设置'][0]['columns'][0]['department'].id,11)

if __name__ == '__main__':
    unittest.main()
