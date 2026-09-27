"""Explicit undergraduate table columns and admissions college/programme blocks."""
import json
import re
from urllib.parse import urlsplit

from backend.services.source_inventory import resolve_page_link

TABLES = {
    ('www.snnu.edu.cn', '/jyjx/bkzy.htm'): (
        '.table > table', ['序号', '专业名称', '专业代码', '所在学院名称', '修业年限', '授予学位', '专业标识']),
    ('ugs.hrbeu.edu.cn', '/2819/list.htm'): (
        '.wp_articlecontent .paging_content > table', ['专业代码', '专业名称', '学科门类', '专业类', '授予学位']),
}
CQMU = ('bzkzs.cqmu.edu.cn', '/xxgk/xyzy.htm')


def catalogue_scopes(soup, url):
    parsed = urlsplit(url)
    signature = (parsed.hostname, parsed.path)
    if signature in TABLES:
        return soup.select(TABLES[signature][0])
    if signature == CQMU:
        return soup.select('.content_box2 > div')
    return []


def catalogue_relationships(soup, url, owner_key):
    from .structure import clean, locator, node_key
    parsed = urlsplit(url)
    signature = (parsed.hostname, parsed.path)
    scopes = catalogue_scopes(soup, url)
    nodes, links, replaced, notes, attributes = [], [], set(), [], []
    if not scopes:
        return nodes, links, replaced, notes

    def add(element, name, kind, parent, relation, target='', identity=''):
        position = locator(element)
        key = node_key(kind, name, url + '#parent:' + parent + ':' + (identity or position))
        nodes.append({'key': key, 'name': name, 'kind': kind, 'url': target, 'parent': parent,
                      'locator': position, 'relation': relation})
        return key

    def replace(scope):
        replaced.update(locator(el) for el in [scope, *scope.find_all()])

    if signature in TABLES:
        expected = TABLES[signature][1]
        for table in scopes:
            rows = [r for r in table.find_all('tr') if r.find_parent('table') is table]
            cells = [r.find_all(['td', 'th'], recursive=False) for r in rows]
            if not cells or [clean(c.get_text(' ', strip=True)) for c in cells[0]] != expected:
                notes.append('programme_catalog_requires_review:table_headers_changed')
                continue
            # These reviewed tables repeat every field on each row. New spans or
            # malformed rows require review instead of shifting a programme's owner.
            if any(len(row) != len(expected) or any(c.get('rowspan', '1') != '1' or c.get('colspan', '1') != '1'
                   for c in row) for row in cells):
                notes.append('programme_catalog_requires_review:table_shape_changed')
                continue
            records = [dict(zip(expected, [clean(c.get_text(' ', strip=True)) for c in row])) for row in cells[1:]]
            if not records or any(not r['专业名称'] or not re.fullmatch(r'\d{6}[A-Z]{0,2}', r['专业代码'])
                                  or ('所在学院名称' in r and not r['所在学院名称']) for r in records):
                notes.append('programme_catalog_requires_review:incomplete_programme_row')
                continue
            replace(table)
            colleges = {}
            for row, record in zip(cells[1:], records):
                parent = owner_key
                college = record.get('所在学院名称')
                if college:
                    if college not in colleges:
                        colleges[college] = add(row[expected.index('所在学院名称')], college, 'unit', owner_key,
                                                'programme_college', identity=college)
                    parent = colleges[college]
                element = row[expected.index('专业名称')]
                key = add(element, record['专业名称'], 'major', parent, 'major_directory_entry',
                          identity=record['专业代码'] + ':' + record['专业名称'])
                attributes.append({'key': key, 'fields': {k: v for k, v in record.items()
                                   if k not in ('序号', '专业名称', '所在学院名称') and v}})
        if nodes:
            period = re.search(r'陕西师范大学本科专业目录[（(]([^）)]+)[）)]', soup.get_text('', strip=True)) if signature[0] == 'www.snnu.edu.cn' else None
            context = {'scope': '官网本科专业目录', 'period': period.group(1) if period else '',
                       'notice': '按官网表格列示；目录日期不等于当前开设状态，表中未给出的学院归属和独立入口不补猜。'}
            if signature[0] == 'www.snnu.edu.cn':
                context['legend'] = 'S 表示师范专业；J 表示师范非师范兼招专业。'
            notes.append('programme_catalog_context:' + json.dumps(context, ensure_ascii=False))
            notes.append('programme_catalog_attributes:' + json.dumps(attributes, ensure_ascii=False))
    elif signature == CQMU:
        for scope in scopes:
            for heading in scope.select(':scope > a:has(> .zszy_box_top)'):
                listing = heading.find_next_sibling()
                if listing is None or 'containerq' not in listing.get('class', []):
                    notes.append('programme_catalog_requires_review:missing_college_programmes')
                    continue
                name = clean(heading.get_text(' ', strip=True))
                target = resolve_page_link(url, heading.get('href'))
                # A joint admissions heading is not a newly invented college.
                kind = 'group' if '+' in name else 'unit'
                parent = add(heading, name, kind, owner_key, 'programme_joint_group' if kind == 'group'
                             else 'programme_college', target)
                replace(heading)
                replace(listing)
                for anchor in listing.select(':scope > div > a[href]'):
                    label = clean(anchor.get_text(' ', strip=True))
                    destination = resolve_page_link(url, anchor.get('href'))
                    if not label or not destination:
                        continue
                    add(anchor, label, 'major', parent, 'major_directory_entry', destination)
                    links.append({'label': label, 'url': destination, 'kind': 'major', 'path': ['招生专业', name],
                                  'locator': locator(anchor), 'decision': 'follow' if urlsplit(destination).hostname == parsed.hostname
                                  else 'external_review'})
                # The college label links into admissions introductions, not the college website.
                if target:
                    links.append({'label': name, 'url': target, 'kind': 'directory', 'path': ['招生专业'],
                                  'locator': locator(heading), 'decision': 'follow' if urlsplit(target).hostname == parsed.hostname
                                  else 'external_review'})
        if nodes:
            notes.append('programme_catalog_context:' + json.dumps({
                'scope': '本科招生学院与专业目录', 'period': '',
                'notice': '这是招生网站列示的专业及培养方向，不能代替当前全部开设专业或院系发布来源。同一专业在多个学院下列示时分别保留。'}, ensure_ascii=False))
    return nodes, links, replaced, notes
