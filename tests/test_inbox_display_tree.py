"""Picker grouping remains navigation, distinct from shared ownership facts."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.services.inbox_tree import display_tree


class InboxDisplayTreeTests(unittest.TestCase):
    def capture(self):
        records = [dict(id=1, school_id=1, kind='group', name='组织机构', group_name=''),
            dict(id=2, school_id=1, kind='unit', name='科技与信息部', group_name='组织机构'),
            dict(id=3, school_id=1, kind='column', name='科研项目', group_name='科技与信息部'),
            dict(id=4, school_id=1, kind='column', name='科研成果', group_name='组织机构')]
        column = lambda ident: dict(department=SimpleNamespace(**records[ident-1]),
            label=records[ident-1]['name'], display_group=records[ident-1]['group_name'], unavailable=False)
        capture = dict(department_records=records, facts=dict(nodes=[dict(id='owner:2', entity_type='organization',
            department_id=2, label='科技与信息部')], relations=[dict(type='source_ownership', source='owner:2', target='source:3')]),
            directory_entries=[SimpleNamespace(parent_id=1, department_id=2), SimpleNamespace(parent_id=2, department_id=4)])
        a, b = column(3), column(4)
        choices = {'有依据的栏目归属': [dict(key='owner:2',name='科技与信息部',own_columns=[a],columns=[a],children=[],expandable=True)],
            '栏目（归属待核实）': [dict(key='source:4',own_columns=[b],columns=[b],children=[],expandable=False)]}
        return capture, choices

    def test_saved_directory_groups_verified_and_legacy_choices_without_changing_facts(self):
        capture, choices = self.capture(); before = deepcopy(capture)
        with patch('backend.services.starter_catalog.placements', return_value=[]):
            tree = display_tree(capture, choices, 'https://example.edu.cn/')
        self.assertEqual(list(tree), ['组织机构'])
        self.assertEqual(len(tree['组织机构']), 1)
        unit = tree['组织机构'][0]
        self.assertEqual(unit['name'], '科技与信息部')
        self.assertEqual({c['department'].id for c in unit['columns']}, {3, 4})
        self.assertEqual(capture, before)
        self.assertTrue(all(not c['display_group'] for c in unit['columns']))

    def test_cycles_do_not_hide_a_source_or_invent_an_owner(self):
        capture, choices = self.capture()
        capture['directory_entries'].append(SimpleNamespace(parent_id=2, department_id=1))
        with patch('backend.services.starter_catalog.placements', return_value=[]):
            tree = display_tree(capture, choices, 'https://example.edu.cn/')
        self.assertEqual({c['department'].id for units in tree.values() for unit in units for c in unit['columns']}, {3, 4})
        self.assertEqual(len(capture['facts']['relations']), 1)

    def test_preset_display_path_preserves_distinct_source_ids(self):
        capture, choices = self.capture(); capture['directory_entries'] = []
        leaf = choices['栏目（归属待核实）'][0]
        duplicate = deepcopy(leaf['own_columns'][0]); duplicate['department'].id = 5
        capture['department_records'].append(dict(vars(duplicate['department'])))
        leaf['own_columns'].append(duplicate)
        paths = [dict(group='组织机构',nodes=[dict(key='legacy-science',name='科技与信息部')],label='科研成果')]
        with patch('backend.services.starter_catalog.placements', return_value=paths):
            tree = display_tree(capture, choices, 'https://example.edu.cn/')
        self.assertEqual({c['department'].id for unit in tree['组织机构'] for c in unit['columns']}, {3,4,5})

    def test_redundant_legacy_branch_does_not_duplicate_current_picker_branch(self):
        capture, choices = self.capture()
        capture['department_records'].append(dict(id=5,school_id=1,kind='unit',name='科技与信息部',group_name='组织机构'))
        capture['directory_entries'].append(SimpleNamespace(parent_id=5,department_id=4))
        with patch('backend.services.starter_catalog.placements',return_value=[]):
            tree = display_tree(capture,choices,'https://example.edu.cn/')
        self.assertEqual(len(tree['组织机构']),1)
        self.assertEqual({c['department'].id for c in tree['组织机构'][0]['columns']},{3,4})

    def test_two_verified_same_name_publishers_stay_distinct(self):
        capture, choices = self.capture()
        capture['department_records'].append(dict(id=5,school_id=1,kind='unit',name='科技与信息部',group_name='组织机构'))
        capture['facts']['nodes'].append(dict(id='owner:5',entity_type='organization',department_id=5,label='科技与信息部'))
        other = deepcopy(choices['有依据的栏目归属'][0]);other['key']='owner:5'
        choices['有依据的栏目归属'].append(other)
        with patch('backend.services.starter_catalog.placements',return_value=[]):
            tree = display_tree(capture,choices,'https://example.edu.cn/')
        self.assertEqual(len(tree['组织机构']),2)
        self.assertEqual(len({u['key'] for u in tree['组织机构']}),2)

    def test_same_entrance_and_exact_sources_collapse_legacy_display_only(self):
        capture, choices = self.capture()
        choices = {'待核实': [choices['栏目（归属待核实）'][0]]}
        capture['department_records'][1]['list_url'] = 'https://example.edu.cn/science/'
        capture['department_records'].append(dict(id=5, school_id=1, kind='unit',
            name='科技与信息部', group_name='组织机构', list_url='https://example.edu.cn/science/'))
        capture['directory_entries'].append(SimpleNamespace(parent_id=5, department_id=4))
        before = deepcopy(capture)
        with patch('backend.services.starter_catalog.placements', return_value=[]):
            tree = display_tree(capture, choices, 'https://example.edu.cn/')
        self.assertEqual(len(tree['组织机构']), 1)
        self.assertEqual({c['department'].id for c in tree['组织机构'][0]['columns']}, {4})
        self.assertEqual(capture, before)

    def test_same_name_with_different_entrance_does_not_collapse(self):
        capture, choices = self.capture()
        choices = {'待核实': [choices['栏目（归属待核实）'][0]]}
        capture['department_records'][1]['list_url'] = 'https://example.edu.cn/science/'
        capture['department_records'].append(dict(id=5, school_id=1, kind='unit',
            name='科技与信息部', group_name='组织机构', list_url='https://other.example.edu.cn/'))
        capture['directory_entries'].append(SimpleNamespace(parent_id=5, department_id=4))
        with patch('backend.services.starter_catalog.placements', return_value=[]):
            tree = display_tree(capture, choices, 'https://example.edu.cn/')
        self.assertEqual(len(tree['组织机构']), 2)

    def test_same_entrance_unions_partial_columns_and_preserves_each_source(self):
        capture, choices = self.capture()
        capture['department_records'][1]['list_url'] = 'https://example.edu.cn/science/'
        capture['department_records'].append(dict(id=5,school_id=1,kind='unit',name='科技与信息部',
            group_name='组织机构',list_url='https://example.edu.cn/science/'))
        capture['directory_entries'] = [SimpleNamespace(parent_id=1,department_id=2),
            SimpleNamespace(parent_id=1,department_id=5),SimpleNamespace(parent_id=2,department_id=3),
            SimpleNamespace(parent_id=5,department_id=4)]
        columns = [choices['有依据的栏目归属'][0]['own_columns'][0],
                   choices['栏目（归属待核实）'][0]['own_columns'][0]]
        choices = {'待核实':[dict(key='unverified',own_columns=columns,columns=columns,expandable=False)]}
        before = deepcopy(capture)
        with patch('backend.services.starter_catalog.placements',return_value=[]):
            tree = display_tree(capture,choices,'https://example.edu.cn/')
        self.assertEqual(len(tree['组织机构']),1)
        self.assertEqual({c['department'].id for c in tree['组织机构'][0]['columns']},{3,4})
        self.assertEqual(capture,before)

    def test_merged_display_keeps_distinct_children_and_shared_child_once(self):
        capture, choices = self.capture()
        capture['department_records'][1]['list_url'] = 'https://example.edu.cn/science/'
        capture['department_records'] += [dict(id=5,school_id=1,kind='unit',name='科技与信息部',
            group_name='组织机构',list_url='https://example.edu.cn/science/'),
            dict(id=6,school_id=1,kind='unit',name='服务中心',group_name='',list_url='')]
        capture['directory_entries'] = [SimpleNamespace(parent_id=1,department_id=2),
            SimpleNamespace(parent_id=1,department_id=5),SimpleNamespace(parent_id=2,department_id=6),
            SimpleNamespace(parent_id=5,department_id=6),SimpleNamespace(parent_id=6,department_id=4),
            SimpleNamespace(parent_id=2,department_id=3)]
        columns = [choices['有依据的栏目归属'][0]['own_columns'][0],
                   choices['栏目（归属待核实）'][0]['own_columns'][0]]
        choices = {'待核实':[dict(key='unverified',own_columns=columns,columns=columns,expandable=False)]}
        with patch('backend.services.starter_catalog.placements',return_value=[]):
            tree = display_tree(capture,choices,'https://example.edu.cn/')
        self.assertEqual(len(tree['组织机构']),1)
        self.assertEqual(len(tree['组织机构'][0]['children']),1)
        self.assertEqual({c['department'].id for c in tree['组织机构'][0]['columns']},{3,4})

    def test_explicit_preset_heading_reuses_the_unique_recorded_display_entrance(self):
        capture, choices = self.capture()
        capture['department_records'][1]['list_url'] = 'https://example.edu.cn/science/'
        columns = [choices['有依据的栏目归属'][0]['own_columns'][0], choices['栏目（归属待核实）'][0]['own_columns'][0]]
        choices = {'待核实':[dict(key='unverified',own_columns=columns,columns=columns,expandable=False)]}
        def preset(url, source):
            return [dict(group='组织机构',nodes=[dict(key='configured-display',name='科技与信息部')])] if source.id==3 else []
        with patch('backend.services.starter_catalog.placements',side_effect=preset):
            tree = display_tree(capture,choices,'https://example.edu.cn/')
        self.assertEqual(len(tree['组织机构']),1)
        self.assertEqual({c['department'].id for c in tree['组织机构'][0]['columns']},{3,4})

    def test_preset_cannot_choose_between_two_distinct_recorded_entrances(self):
        capture, choices = self.capture()
        capture['department_records'][1]['list_url'] = 'https://example.edu.cn/science/'
        capture['department_records'].append(dict(id=5,school_id=1,kind='unit',name='科技与信息部',
            group_name='组织机构',list_url='https://other.example.edu.cn/'))
        capture['directory_entries'].append(SimpleNamespace(parent_id=5,department_id=4))
        columns = [choices['有依据的栏目归属'][0]['own_columns'][0], choices['栏目（归属待核实）'][0]['own_columns'][0]]
        choices = {'待核实':[dict(key='unverified',own_columns=columns,columns=columns,expandable=False)]}
        with patch('backend.services.starter_catalog.placements',side_effect=lambda url,source:
                [dict(group='组织机构',nodes=[dict(key='configured-display',name='科技与信息部')])] if source.id==3 else []):
            tree = display_tree(capture,choices,'https://example.edu.cn/')
        self.assertEqual(len(tree['组织机构']),3)

    def test_identical_addressless_navigation_controls_fold_without_merging_records(self):
        capture, choices = self.capture()
        capture['department_records'].append(dict(id=5,school_id=1,kind='unit',name='科技与信息部',group_name='组织机构',list_url=''))
        capture['directory_entries'].append(SimpleNamespace(parent_id=5,department_id=4))
        choices = {'待核实':[choices['栏目（归属待核实）'][0]]}
        before = deepcopy(capture)
        with patch('backend.services.starter_catalog.placements',return_value=[]):
            tree = display_tree(capture,choices,'https://example.edu.cn/')
        self.assertEqual(len(tree['组织机构']),1)
        self.assertEqual(capture,before)


if __name__ == '__main__': unittest.main()
