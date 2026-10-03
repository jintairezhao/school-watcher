"""Human-facing source grouping must preserve the original source identities."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def dept(ident, name, group='组织机构', school=1):
    return SimpleNamespace(id=ident, name=name, group_name=group, school_id=school)


class SourceHierarchyTests(unittest.TestCase):
    def test_columns_directly_under_group_remain_selectable_without_fake_department(self):
        from backend.services.inbox import source_hierarchy
        root = dept(10, '招生就业', '招生就业')
        root.kind, root.list_url = 'group', 'https://school.example/admissions/'
        first, second = dept(11, '招生通知', '招生就业'), dept(12, '就业通知', '招生就业')
        for row in (first, second):
            row.kind, row.list_url = 'column', f'https://school.example/{row.id}/'
        entries = [SimpleNamespace(parent_id=root.id, department_id=row.id, position=i)
                   for i, row in enumerate((first, second))]
        tree = source_hierarchy([first, second], [root, first, second], entries)
        leaves = tree['招生就业']
        self.assertEqual({c['department'].id for u in leaves for c in u['columns']}, {11, 12})
        self.assertTrue(all(not u['expandable'] and not u['children'] for u in leaves))
        self.assertEqual({u['name'] for u in leaves}, {'招生通知', '就业通知'})
        restricted = source_hierarchy([second], [root, first, second], entries)
        self.assertEqual([c['department'].id for u in restricted['招生就业'] for c in u['columns']], [12])

    def test_related_columns_share_one_unit_despite_missing_group(self):
        from backend.services.inbox import source_hierarchy
        rows = [dept(1, '学生工作与安全保卫部'), dept(2, '学生工作与安全保卫部-学生管理'),
                dept(3, '学生工作与安全保卫部-学生资助', None)]
        units = source_hierarchy(rows)['组织机构']
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]['name'], rows[0].name)
        self.assertEqual([c['label'] for c in units[0]['columns']], ['学生管理', '学生资助', '本部门通知'])
        self.assertEqual([c['department'].id for c in units[0]['columns']], [2, 3, 1])
        self.assertEqual(rows[2].group_name, None)
        self.assertEqual(rows[2].name, '学生工作与安全保卫部-学生资助')

    def test_known_parent_does_not_add_unsubscribed_columns(self):
        from backend.services.inbox import source_hierarchy
        parent, child = dept(1, '化工与环境学院', '院系设置'), dept(2, '化工与环境学院-重要通知', '院系设置')
        unit = source_hierarchy([child], [parent, child])['院系设置'][0]
        self.assertEqual(unit['name'], parent.name)
        self.assertEqual([c['department'].id for c in unit['columns']], [2])

    def test_hyphen_alone_does_not_invent_an_owner(self):
        from backend.services.inbox import source_hierarchy
        rows = [dept(1, '科学-技术'), dept(2, '科学-研究')]
        units = source_hierarchy(rows)['组织机构']
        self.assertEqual([u['name'] for u in units], [r.name for r in rows])
        self.assertTrue(all(not u['expandable'] for u in units))

    def test_same_name_in_another_school_cannot_supply_parent(self):
        from backend.services.inbox import source_hierarchy
        child, foreign = dept(1, '化工学院-通知'), dept(2, '化工学院', school=2)
        unit = source_hierarchy([child], [child, foreign])['组织机构'][0]
        self.assertFalse(unit['expandable'])
        self.assertEqual(unit['name'], child.name)

    @patch('backend.services.source_placements.official_source_placements')
    def test_official_parent_replaces_flat_college_group_without_merging_unknown_entry(self, placements):
        from backend.services.inbox import source_hierarchy, source_groups
        rows = [dept(21, '通知公告', '地球科学与工程学院'),
                dept(299, '院内新闻', '地球科学与工程学院'),
                dept(300, '地球科学与工程学院', '')]
        placements.return_value = {d.id: [{'group': '院系设置', 'nodes': [
            {'key': 'earth', 'name': '地球科学与工程学院'}], 'label': d.name}] for d in rows[:2]}
        tree = source_hierarchy(rows)
        self.assertNotIn('地球科学与工程学院', tree)
        self.assertEqual(len(tree['院系设置']), 1)
        self.assertEqual([c['department'].id for c in tree['院系设置'][0]['columns']], [21, 299])
        self.assertEqual(tree['其他信息来源'][0]['columns'][0]['department'].id, 300)
        self.assertEqual([d.id for d in source_groups(rows)['院系设置']], [21, 299])
        self.assertEqual(rows[0].group_name, '地球科学与工程学院')

    @patch('backend.services.source_placements.official_source_placements')
    def test_nested_units_keep_distinct_leaves_and_multiple_paths(self, placements):
        from backend.services.inbox import source_hierarchy
        rows = [dept(1, '教务部'), dept(2, '教务部-创新创业学院'), dept(3, '教务部-教学安排')]
        office = {'key': 'office', 'name': '教务部'}
        attached = {'key': 'attached', 'name': '创新创业学院'}
        placements.return_value = {
            1: [{'group': '组织机构', 'nodes': [office], 'label': '通知公告'}],
            2: [{'group': '组织机构', 'nodes': [office, attached], 'label': '通知公告'},
                {'group': '院系设置', 'nodes': [attached], 'label': '通知公告'}]}
        tree = source_hierarchy(rows)
        parent = tree['组织机构'][0]
        self.assertEqual([c['department'].id for c in parent['own_columns']], [3, 1])
        self.assertEqual({c['department'].id for c in parent['columns']}, {1, 2, 3})
        child = parent['children'][0]
        self.assertEqual(child['name'], '创新创业学院')
        self.assertEqual([c['department'].id for c in child['columns']], [2])
        self.assertNotEqual(child['key'], tree['院系设置'][0]['key'])
        # Resolving ancestors must not add unsubscribed siblings or their notifications.
        restricted = source_hierarchy([rows[2]], rows)['组织机构'][0]
        self.assertEqual([c['department'].id for c in restricted['columns']], [3])
        self.assertFalse(restricted['children'])

    @patch('backend.services.source_placements.official_source_placements')
    def test_explicit_membership_takes_priority_over_other_discovery_paths(self, placements):
        from backend.services.inbox import source_hierarchy
        owner, child = dept(291, '教学科研单位、研究机构', '学院部门'), dept(301, '信息学院', '学院部门')
        child.list_url = 'https://school.example/info/'
        entry = SimpleNamespace(parent_id=291, department_id=301, position=0)
        placements.return_value = {301: [{'group': '其他网站目录', 'nodes': [
            {'key': 'info', 'name': '信息学院'}], 'label': '本部门通知'}]}
        tree = source_hierarchy([child], [owner, child], [entry])
        self.assertEqual(list(tree), ['学院部门'])
        self.assertEqual(tree['学院部门'][0]['key'], '291')
        self.assertEqual([c['department'].id for c in tree['学院部门'][0]['columns']], [301])

    def test_nested_directory_memberships_preserve_ancestors_and_path_identity(self):
        from backend.services.inbox import source_hierarchy
        root, other, middle, child = (dept(10, '学院部门'), dept(11, '科研机构'),
            dept(12, '教学科研单位'), dept(13, '信息学院'))
        child.list_url = 'https://school.example/info/'
        links = [SimpleNamespace(parent_id=p, department_id=c, position=0)
                 for p, c in [(10, 12), (11, 12), (12, 13)]]
        tree = source_hierarchy([child], [root, other, middle, child], links)['组织机构']
        self.assertEqual([u['name'] for u in tree], ['学院部门', '科研机构'])
        self.assertEqual([u['children'][0]['name'] for u in tree], ['教学科研单位'] * 2)
        self.assertNotEqual(tree[0]['children'][0]['key'], tree[1]['children'][0]['key'])
        self.assertEqual([c['department'].id for c in tree[0]['columns']], [13])
        self.assertFalse(tree[0]['own_columns'])


if __name__ == '__main__':
    unittest.main()
