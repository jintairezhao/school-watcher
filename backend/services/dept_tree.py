"""部门树构建辅助"""
from datetime import datetime, timedelta

from backend.database.models import Announcement


def _fallback_group_name(name):
    """为无导航分组（group_name 为空）的部门推断合理分组。

    发现阶段对「组织机构」这类单链接部门常留空 group_name，导致它们被当成
    独立 tab 平铺。这里按命名规则归位：学院归入「院系设置」，其余（教务部、
    研究生部、学生工作与安全保卫部等职能机构）归入「组织机构」。
    """
    if '学院' in name:
        return '院系设置'
    return '组织机构'


def build_dept_tree(departments):
    """将扁平部门列表构建为分组树结构。

    两级结构：
    1. 导航分组（group_name）— 来自官网导航分类（如"院系设置""科学研究"）
    2. 部门父子关系（通过名称中「-」分隔符检测）

    无 group_name 的部门按命名规则归入"组织机构"或"院系设置"，DAILY NEWS 始终置顶。
    """
    dept_list = list(departments)

    # 学校官网首页聚合板块（如「校区通知公告」「通知公告」）是平台、非来源单位：
    # 它每一条通知其实都来自某个学院/部门，且已包含在「全部部门」视图里，
    # 故从导航中排除，避免与真正的来源单位（各学院 / 教务处 / 职能处室）并列。
    # 判定：list_url 指向学校根域名（与 School.url 一致），来源单位均为子路径/子域名。
    if dept_list:
        school_root = (dept_list[0].school.url or '').strip().rstrip('/')
    else:
        school_root = ''
    dept_list = [
        d for d in dept_list
        if not (school_root and (d.list_url or '').strip().rstrip('/') == school_root)
    ]

    # 0 条通知的子部门不进部门栏（用户规则：零信息不显示，如学院下抓不到内容的
    # 「学生工作」）；等内容入库后自然出现
    dept_list = [
        d for d in dept_list
        if not ('-' in (d.name or '') and d.announcements.count() == 0)
    ]

    dept_name_set = {d.name for d in dept_list}

    # ---- 第一层：识别父子部门关系（基于名称 "-" 分隔符） ----
    child_ids = set()
    parent_children = {}  # parent_name -> [child_depts]

    for dept in dept_list:
        if '-' in dept.name:
            parent_name = dept.name.split('-', 1)[0]
            if parent_name in dept_name_set:
                child_ids.add(dept.id)
                parent_children.setdefault(parent_name, []).append(dept)

    # ---- 构建叶子节点（部门项） ----
    def make_dept_item(dept):
        """构建单个部门项（可能是父部门，含子部门列表）"""
        is_daily = dept.name.upper() == 'DAILY NEWS'
        children = parent_children.get(dept.name, [])

        if is_daily:
            cutoff = datetime.utcnow() - timedelta(days=7)
            total_count = Announcement.query.filter(
                Announcement.school_id == dept.school_id,
                Announcement.published_at >= cutoff,
            ).count()
        else:
            total_count = dept.announcements.count() + sum(
                c.announcements.count() for c in children
            )

        return {
            'dept': dept,
            'children': children,
            'has_children': len(children) > 0,
            'total_count': total_count,
            'child_ids': [c.id for c in children],
            'is_daily_news': is_daily,
        }

    # ---- 第二层：按 group_name 分组 ----
    groups = {}  # group_name -> [dept_items]
    daily_news_item = None

    for dept in dept_list:
        if dept.id in child_ids:
            continue  # 子部门嵌套在父部门下

        item = make_dept_item(dept)

        if item['is_daily_news']:
            daily_news_item = item
            continue

        gn = (dept.group_name or '').strip()
        if not gn:
            gn = _fallback_group_name(dept.name)
        groups.setdefault(gn, []).append(item)

    # ---- 组装最终树 ----
    tree = []

    # DAILY NEWS 始终置顶
    if daily_news_item:
        tree.append(daily_news_item)

    # 按分组展示（分组内部门按名称排序）
    for gn in sorted(groups.keys()):
        items = groups[gn]
        items.sort(key=lambda x: x['dept'].name)
        group_total = sum(it['total_count'] for it in items)
        tree.append({
            'type': 'group',
            'group_name': gn,
            'depts': items,  # 用 depts 避免与 dict.items() 方法冲突
            'total_count': group_total,
            'has_nested': any(it['has_children'] for it in items),
        })

    return tree
