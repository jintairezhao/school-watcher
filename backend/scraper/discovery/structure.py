"""Evidence-preserving parsing of university units, menus and publication channels."""
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from backend.services.source_inventory import resolve_page_link
from backend.scraper.http_client import same_school_url
from backend.scraper.article_urls import ARTICLE

DIRECTORIES = ('院系设置', '院系专业', '专业设置', '专业介绍', '专业目录', '本科专业', '系所设置',
               '学院设置', '学院部门', '院系部门', '院系介绍', '教学单位',
               '教学机构', '组织机构', '机构设置', '机构职能', '党政部门', '职能部门', '直属单位',
               '附属单位', '科研机构', '研究机构', '学部设置', '网站导航', '网站地图', '站点导航',
               '部门导航', '单位网站', '机构目录', '院系列表', '学部与院系', '实体研究机构')
UNIT = re.compile(r'(学院|学部|书院|研究院|研究所|实验室|中心|办公室|办事处|[部处系馆社院]|委员会|'
                  r'基金会|联合会|出版社|校区|基地|团委|工会|学堂)([（(].{1,25}[）)])?$')
CHANNEL = re.compile(r'通知|公告|新闻|动态|资讯|公示|招标|采购|招聘|招生|就业|讲座|报告|学术活动|'
                     r'推免|保研|夏令营|政策|文件|规章|制度|下载|信息公开|办事指南|工作安排|科研项目|成果')
TOPICS = re.compile(r'教学|教务|学生|研究生|本科生|党[建群委]|团学|工会|培养|学位|科研|学术|合作|'
                    r'交流|国际|人才|人事|财务|后勤|保卫|网络|信息|校园|服务|概况|简介|校友|捐赠')
SERVICE = re.compile(r'登录|登陆|身份认证|邮件系统|办公系统|办事大厅|融合门户|信息门户|VPN', re.I)
FILES = re.compile(r'\.(pdf|docx?|xlsx?|pptx?|zip|rar|7z|mp4|jpe?g|png|gif|svg|css|js|ico)(?:$|\?)', re.I)
SKIP = {'首页', '返回首页', '网站首页', '联系我们', '联系方式', 'English', 'ENGLISH', 'EN',
        'Русский', '中文', '打印', '关闭', '返回', 'TOP', 'Top'}
MORE = {'更多', '更多>>', '更多>', '查看更多', 'more', 'MORE', 'More', '>>', '>'}


def clean(text):
    text = re.sub(r'\s+', ' ', text or '').strip()
    # Official tables often split Chinese names across spans or insert layout spaces.
    return re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', text)


def locator(element):
    parts = []
    while isinstance(element, Tag) and element.name != '[document]':
        siblings = [s for s in element.previous_siblings if isinstance(s, Tag) and s.name == element.name]
        parts.append(f'{element.name}:nth-of-type({len(siblings) + 1})')
        element = element.parent
    return ' > '.join(reversed(parts))


def node_key(kind, label, url):
    if kind == 'unit' and url.startswith(('http://', 'https://')):
        parsed = urlsplit(url)
        # A same-named institution at one host/path can advertise both protocols.
        # This merges its identity only; page URLs and every observed entry stay separate.
        url = '//' + parsed.netloc + parsed.path + ('?' + parsed.query if parsed.query else '') + ('#' + parsed.fragment if parsed.fragment else '')
    return hashlib.sha256((kind + '\0' + label + '\0' + url).encode()).hexdigest()[:24]


def classify(label):
    label = label.strip(' *·')
    if any(word in label for word in DIRECTORIES) or label in (
            '院系', '机构', '学部', '二级学院', '二级院系', '教学学院', '教学院', '学部学院',
            '教学科研内设机构', '教学和科研单位', '教学科研单位', '教学科研机构'):
        return 'directory'
    if len(label) <= 80 and UNIT.search(label):
        return 'unit'
    if len(label) <= 40 and CHANNEL.search(label):
        return 'channel'
    if len(label) <= 30 and TOPICS.search(label):
        return 'navigation'
    return None


def menu_path(element):
    """Only actual nested menu ancestors; never split a name on '-'."""
    result = []
    for li in reversed(element.find_parents()):
        if li.name != 'li':
            # Campus menus also use a div containing one heading and a child ul.
            # Restrict this to explicit navigation/header containers so ordinary
            # content widgets are not turned into menu ancestry.
            if li.name not in ('div', 'section') or not any(
                    p.name in ('nav', 'header') or re.search(r'nav|menu|header',
                        ' '.join(p.get('class', [])) + ' ' + p.get('id', ''), re.I)
                    for p in [li, *li.parents] if isinstance(p, Tag)):
                continue
            lists = li.find_all(['ul', 'ol'], recursive=False)
            if not any(element in child.descendants for child in lists):
                continue
        head = li.find(['a', 'h2', 'h3', 'h4', 'span'], recursive=False)
        if head is None or head is element or element in head.descendants:
            continue
        text = clean(head.get_text(' ', strip=True))
        if text and len(text) <= 60 and text not in SKIP:
            result.append((text, head))
    return result


def nearby_heading(element):
    for parent in list(element.parents)[:4]:
        if not isinstance(parent, Tag) or parent.name in ('body', 'html'):
            break
        for child in parent.find_all(recursive=False):
            if child is element or element in child.descendants:
                continue
            classes = ' '.join(child.get('class', []))
            if child.name in ('h1', 'h2', 'h3', 'h4', 'strong') or re.search(r'tit|heading|hd|(?:^|[_\s-])bt\d*(?:$|[_\s-])', classes, re.I):
                text = clean(child.get_text(' ', strip=True))
                if 2 <= len(text) <= 40 and text not in MORE:
                    return text, child
    return '', None


def table_cell_entries(cell, base_url):
    """Keep block/line boundaries while joining inline fragments of a single name."""
    entries, fragments = [], []

    def flush():
        name = clean(''.join(fragments))
        fragments.clear()
        if name and name not in ('无', '—', '-', '/') and len(name) <= 100:
            entries.append((name, '', locator(cell) + f'::text({len(entries)})'))

    def visit(tag):
        if not isinstance(tag, Tag):
            fragments.append(str(tag))
            return
        if tag.name in ('script', 'style'):
            return
        if tag.name == 'br':
            flush()
        elif tag.name == 'a':
            flush()
            name = clean(tag.get_text(' ', strip=True))
            href = (tag.get('href') or '').strip()
            url = resolve_page_link(base_url, href)
            if name and len(name) <= 100:
                entries.append((name, url, locator(tag)))
        else:
            block = tag.name in ('p', 'div', 'li', 'dt', 'dd')
            if block:
                flush()
            for child in tag.children:
                visit(child)
            if block:
                flush()

    visit(cell)
    flush()
    return entries


def table_relationships(soup, page_url, base_url, owner_key):
    """Use column headings as relationship evidence, including unlinked attached units."""
    nodes, links, replaced = [], [], set()

    def add(entry, parent, kind, relation):
        name, url, loc = entry
        key = node_key(kind, name, url or (page_url + '#parent:' + parent))
        nodes.append({'key': key, 'name': name, 'kind': kind, 'url': url,
                      'parent': parent, 'locator': loc, 'relation': relation})
        decision = ('missing_link' if not url else 'restricted_or_service' if SERVICE.search(name)
                    else 'document_reference' if FILES.search(url)
                    else 'follow' if same_school_url(url, base_url) else 'official_external_link')
        links.append({'label': name, 'url': url, 'kind': kind, 'path': [],
                      'locator': loc, 'decision': decision})
        return key

    for table in soup.find_all('table'):
        rows = [r for r in table.find_all('tr') if r.find_parent('table') is table]
        if not rows:
            continue
        headers = [re.sub(r'\s+', '', c.get_text()) for c in rows[0].find_all(['th', 'td'], recursive=False)]
        parent_index = next((i for i, h in enumerate(headers) if h in ('学院', '单位名称', '部门名称', '机构名称')), None)
        child_indexes = [i for i, h in enumerate(headers) if h in ('系', '系部', '挂靠单位', '下属单位', '下设单位', '所属单位')]
        if parent_index is None or not child_indexes:
            continue
        # Generic cell parsing otherwise invents a concatenated unit alongside all
        # the correctly split names, and loses the context of the remarks column.
        replaced.update(locator(tag) for tag in [table, *table.find_all(True)])
        reference_indexes = [i for i, h in enumerate(headers) if h in ('备注', '网站', '相关网站', '工作链接')]
        active_parent, remaining_span = None, 0
        for row in rows[1:]:
            cells = row.find_all(['td', 'th'], recursive=False)
            if not cells:
                continue
            if remaining_span:
                parent = active_parent
                remaining_span -= 1
                offset = 1
            else:
                if parent_index >= len(cells):
                    continue
                cell = cells[parent_index]
                parents = table_cell_entries(cell, base_url)
                if not parents:
                    continue
                if len(parents) == 1:
                    parent = add(parents[0], owner_key, 'unit', 'directory_entry')
                else:
                    name = ' / '.join(p[0] for p in parents)
                    parent = node_key('group', name, page_url + '#' + locator(cell))
                    nodes.append({'key': parent, 'name': name, 'kind': 'group', 'url': '',
                                  'parent': owner_key, 'locator': locator(cell), 'relation': 'shared_table_row'})
                    for entry in parents:
                        add(entry, parent, 'unit', 'same_table_row')
                try:
                    remaining_span = max(0, int(cell.get('rowspan', '1')) - 1)
                except ValueError:
                    remaining_span = 0
                active_parent, offset = parent, 0
            for index in child_indexes + reference_indexes:
                actual_index = index - (offset if index > parent_index else 0)
                if actual_index >= len(cells) or actual_index < 0:
                    continue
                cell = cells[actual_index]
                relation = 'attached_unit' if headers[index] == '挂靠单位' else 'sub_unit'
                for entry in table_cell_entries(cell, base_url):
                    kind = 'unit'
                    if index in reference_indexes:
                        kind = classify(entry[0])
                        kind = kind if kind in ('unit', 'channel') else 'group'
                        relation = 'table_reference'
                    add(entry, parent, kind, relation)
    return nodes, links, replaced


def heading_relationships(soup, page_url, base_url, owner_key):
    """A unit heading followed by its list is a real, common official hierarchy."""
    from .directory_adapters import heading_lists, heading_relation
    nodes, replaced = [], set()
    standard = [(h, h.find_next_sibling()) for h in soup.select('h2,h3,h4')]
    adapted = list(heading_lists(soup, page_url))
    for heading, listing in standard + adapted:
        name = clean(heading.get_text(' ', strip=True))
        is_adapted = any(heading is h for h, _ in adapted)
        parent_kind = 'unit' if classify(name) == 'unit' else 'group'
        if parent_kind != 'unit' and not is_adapted:
            continue
        if not listing or (not is_adapted and listing.name not in ('ul', 'ol', 'dl', 'p')):
            continue
        anchor = heading.find('a')
        url = resolve_page_link(base_url, anchor.get('href')) if anchor else ''
        loc = locator(anchor or heading)
        key = node_key(parent_kind, name, url or (page_url + '#parent:' + owner_key))
        nodes.append({'key': key, 'name': name, 'kind': parent_kind, 'url': url,
                      'parent': owner_key, 'locator': loc, 'relation': heading_relation(soup, heading, page_url)})
        replaced.add(loc)
        for child in listing.select('a'):
            if not is_adapted and listing.name != 'p' and child.find_parent(['ul', 'ol', 'dl']) is not listing:
                continue
            label = clean(child.get_text(' ', strip=True))
            child_kind = classify(label)
            if child_kind != 'unit' and not (is_adapted and child_kind == 'directory'):
                continue
            target = resolve_page_link(base_url, child.get('href'))
            child_loc = locator(child)
            child_kind = 'unit' if child_kind == 'unit' else 'group'
            nodes.append({'key': node_key(child_kind, label, target or (page_url + '#parent:' + key)),
                          'name': label, 'kind': child_kind, 'url': target, 'parent': key,
                          'locator': child_loc, 'relation': 'nested_directory_entry'})
            replaced.add(child_loc)
    return nodes, replaced


def reconcile_unlinked_copies(nodes):
    """Merge a no-link copy only when its same-parent/name placement is unambiguous."""
    while True:
        groups = {}
        for node in nodes:
            groups.setdefault((node['kind'], node['name'], node['parent']), []).append(node)
        remap = {}
        for group in groups.values():
            linked = {n['key'] for n in group if n['url']}
            if len(linked) == 1:
                target = next(iter(linked))
                for n in group:
                    if not n['url'] and n['key'] != target:
                        remap[n['key']] = target
        if not remap:
            return nodes
        for n in nodes:
            n['key'] = remap.get(n['key'], n['key'])
            n['parent'] = remap.get(n['parent'], n['parent'])


def adapted_row_relationships(soup, page_url, base_url, root_url, owner_key):
    from .directory_adapters import row_hierarchy
    nodes, links, replaced, keys = [], [], set(), {}
    for element, parent_element, kind in row_hierarchy(soup, page_url):
        parent = keys.get(id(parent_element), owner_key)
        name = clean(element.get_text(' ', strip=True))
        url = resolve_page_link(base_url, element.get('href'))
        loc = locator(element)
        key = node_key(kind, name, url or (page_url + '#parent:' + parent))
        keys[id(element)] = key
        nodes.append({'key': key, 'name': name, 'kind': kind, 'url': url, 'parent': parent,
                      'locator': loc, 'relation': 'nested_directory_entry' if parent_element is not None else 'directory_entry'})
        links.append({'label': name, 'url': url, 'kind': 'unit' if kind == 'unit' else 'directory',
                      'path': [], 'locator': loc, 'decision': 'missing_link' if not url else
                      'follow' if same_school_url(url, root_url) else 'official_external_link'})
        replaced.add(loc)
    return nodes, links, replaced


def extract_structure(html, page_url, root_url, page_kind='root', page_label='', inherited_path=None):
    from .directory_adapters import (has_adapter, directory_variants, campus_relationships,
                                     buaa_academic_relationships, nju_department_relationships,
                                     profile_directory_relationships, fudan_academic_relationships)
    from .directory_labels import fudan_directory, directory_label
    from .publication_tabs import tab_bindings
    from .unit_profiles import nju_business_profile
    if has_adapter(page_url):
        page_kind = 'directory'
    soup = BeautifulSoup(html, 'lxml')
    profile = nju_business_profile(soup, page_url)
    tabs = tab_bindings(soup, page_url)
    inherited_path = list(inherited_path or [])
    links, nodes, notes = [], [], []
    base = soup.find('base', href=True)
    base_url = urljoin(page_url, base['href']) if base else page_url
    owner_kind = 'unit' if page_kind in ('root', 'unit') and not profile else 'group'
    if profile:
        notes.append('unit_profile_page:' + json.dumps(profile['evidence'], ensure_ascii=False))
        if profile['evidence']['content_pending']:
            notes.append('unit_profile_content_pending')
    owner_key = node_key(owner_kind, page_label, page_url)
    nodes.append({'key': owner_key, 'name': page_label, 'kind': owner_kind, 'url': page_url,
                  'parent': '', 'locator': 'document', 'relation': 'page_identity'})
    elements = soup.select('a, area[href], option[value], [onclick]')
    from .medical_publications import medical_article_locators
    publication_positions = medical_article_locators(soup, page_url)
    seen_elements = set()
    from .wordpress_publications import article_positions, column_links
    post_positions = article_positions(soup, base_url)
    taxonomy_positions = {link['locator'] for link in column_links(soup, base_url)}
    for a in elements:
        if publication_positions and locator(a) in publication_positions:
            continue
        address = a.get('href') or (a.get('value') if a.name == 'option' else '') or ''
        if address.strip() in ('{栏目URL}', '{栏目url}'):
            continue
        raw_label = clean(a.get_text(' ', strip=True) or a.get('title') or a.get('aria-label'))
        if not raw_label:
            img = a.find('img')
            raw_label = clean(img.get('alt', '')) if img else ''
        if (not address or address.startswith('javascript:')) and a.get('onclick'):
            literal = re.search(r"(?:open|href\s*=|location\s*=)\s*\(?\s*['\"]([^'\"]+)['\"]", a['onclick'])
            address = literal.group(1) if literal else ''
        label = raw_label
        label_evidence = None
        if fudan_directory(page_url) and a.name == 'a' and a.get_text(strip=True):
            label, label_evidence = directory_label(a, soup, base_url)
            if label_evidence:
                notes.append('directory_label_evidence:' + json.dumps(label_evidence, ensure_ascii=False))
        heading = None
        if label in MORE or not label:
            from .publication_lists import heading_evidence
            heading = heading_evidence(a, base_url, tabs)
            label = heading['name'] or label
        if not label or len(label) > 120:
            continue
        position = locator(a)
        kind = 'channel' if position in taxonomy_positions or position in post_positions else classify(label)
        if not kind and heading and heading.get('heading_method') in (
                'explicit_tab_control', 'official_indexed_tab_control') and heading['name']:
            kind = 'navigation'
        trail = menu_path(a)
        nav_container = bool(trail) or any(p.name == 'nav' or re.search(r'nav|menu', ' '.join(p.get('class', [])) + ' ' + p.get('id', ''), re.I)
                            for p in a.parents if isinstance(p, Tag))
        if not kind and nav_container and len(label) <= 30 and label not in SKIP:
            kind = 'navigation'
        if label in ('下一页', '下页', 'Next') and page_kind == 'directory':
            kind = 'directory'
        if not kind:
            continue
        marker = (label, address, locator(a))
        if marker in seen_elements:
            continue
        seen_elements.add(marker)
        try:
            url = resolve_page_link(base_url, address)
        except ValueError:
            url = ''
        decision = 'follow'
        if label in SKIP or SERVICE.search(label) or re.search(r'https?://(?:vpn|webvpn|auth|sso|portal|mail|mails|id)\.', url, re.I):
            decision = 'restricted_or_service'
        elif not url:
            decision = 'missing_link'
        elif FILES.search(url):
            decision = 'document_reference'
        elif position in post_positions or ARTICLE.search(url) and not (
                (kind == 'directory' and (label in DIRECTORIES or nav_container)) or
                (kind == 'unit' and (page_kind == 'directory' or nav_container))):
            decision = 'article_reference'
        elif not same_school_url(url, root_url):
            # External university-operated domains are retained with official backlink evidence.
            if same_school_url(page_url, root_url) and kind in ('unit', 'directory', 'channel'):
                decision = 'official_external_link'
            else:
                decision = 'external_review'
        path = inherited_path + [text for text, _ in trail]
        if decision == 'article_reference' and not nav_container:
            links.append({'label': label, 'raw_label': raw_label, 'url': url, 'kind': kind,
                          'path': path, 'locator': locator(a), 'decision': decision})
            continue
        parent = owner_key
        for text, tag in trail:
            # Menu grouping is not evidence of administrative subordination.
            ancestor_kind = 'unit' if classify(text) == 'unit' else 'group'
            ancestor_url = resolve_page_link(base_url, tag.get('href'))
            group_key = node_key(ancestor_kind, text, ancestor_url or (page_url + '#' + locator(tag)))
            if group_key != parent:
                nodes.append({'key': group_key, 'name': text, 'kind': ancestor_kind, 'url': ancestor_url,
                              'parent': parent, 'locator': locator(tag),
                              'relation': 'nested_directory_entry' if page_kind == 'directory' else 'menu_group'})
            parent = group_key
        auxiliary = nav_container or any(
            p.name in ('header', 'footer') or re.search(r'header|footer|foot|breadcrumb',
                ' '.join(p.get('class', [])) + ' ' + p.get('id', ''), re.I)
            for p in a.parents if isinstance(p, Tag))
        relation = 'directory_entry' if page_kind == 'directory' and not auxiliary else 'navigation_entry'
        if page_kind == 'unit' and kind == 'unit':
            if trail:
                relation = 'navigation_entry'
            else:
                relation = 'linked_unit_unverified'
                parent = ''
        if kind == 'channel' and page_kind == 'unit':
            relation = 'unit_channel'
        if profile and kind == 'channel':
            relation = 'shared_site_navigation' if nav_container else 'profile_reference'
        if kind == 'channel' and any(
                p.name == 'footer' or re.search(r'footer|foot', ' '.join(p.get('class', [])) + ' ' + p.get('id', ''), re.I)
                for p in a.parents if isinstance(p, Tag)):
            relation = 'footer_link'
        if kind == 'navigation' and not nav_container and not heading and page_kind != 'directory':
            relation = 'linked_navigation_unverified'
        if label_evidence and label_evidence['status'] == 'unresolved':
            relation = 'directory_label_pending'
        structure_kind = 'unit' if kind == 'unit' else ('channel' if kind == 'channel' else 'group')
        child_key = node_key(structure_kind, label, url or (page_url + '#parent:' + parent))
        if child_key != parent:
            nodes.append({'key': child_key, 'name': label, 'kind': structure_kind, 'url': url,
                          'parent': parent, 'locator': locator(a), 'relation': relation})
        links.append({'label': label, 'raw_label': raw_label, 'url': url, 'kind': kind,
                      'path': path, 'locator': locator(a), 'decision': decision})
    # No-link units in official directories remain represented, including heading-only parents.
    if page_kind == 'directory':
        for tag in soup.select('h2,h3,h4,td,li,dt,dd'):
            if tag.find('a') or tag.find(['li', 'td', 'dt', 'dd']):
                continue
            text = clean(tag.get_text(' ', strip=True))
            if 2 <= len(text) <= 70 and classify(text) == 'unit':
                loc = locator(tag)
                nkey = node_key('unit', text, page_url + '#parent:' + owner_key)
                nodes.append({'key': nkey, 'name': text, 'kind': 'unit', 'url': '',
                              'parent': owner_key, 'locator': loc, 'relation': 'directory_entry_no_link'})
                links.append({'label': text, 'url': '', 'kind': 'unit', 'path': inherited_path,
                              'locator': loc, 'decision': 'missing_link'})
        table_nodes, table_links, table_locations = table_relationships(soup, page_url, base_url, owner_key)
        nodes = [n for n in nodes if n['locator'] not in table_locations] + table_nodes
        links = [l for l in links if not (l['locator'] in table_locations and l['decision'] == 'missing_link')]
        links.extend(table_links)
        if any(n['relation'] == 'shared_table_row' for n in table_nodes):
            notes.append('shared_table_row_parent_requires_review')
        heading_nodes, heading_locations = heading_relationships(soup, page_url, base_url, owner_key)
        nodes = [n for n in nodes if n['locator'] not in heading_locations] + heading_nodes
        row_nodes, row_links, row_locations = adapted_row_relationships(soup, page_url, base_url, root_url, owner_key)
        nodes = [n for n in nodes if n['locator'] not in row_locations] + row_nodes
        links = [l for l in links if l['locator'] not in row_locations] + row_links
        campus_nodes, campus_links, campus_locations = campus_relationships(soup, page_url, root_url, owner_key)
        nodes = [n for n in nodes if n['locator'] not in campus_locations] + campus_nodes
        links = [l for l in links if l['locator'] not in campus_locations] + campus_links
        buaa_nodes, buaa_links, buaa_locations = buaa_academic_relationships(soup, page_url, root_url, owner_key)
        nodes = [n for n in nodes if n['locator'] not in buaa_locations] + buaa_nodes
        links = [l for l in links if l['locator'] not in buaa_locations] + buaa_links
        nju_nodes, nju_links, nju_locations = nju_department_relationships(soup, page_url, root_url, owner_key)
        nodes = [n for n in nodes if n['locator'] not in nju_locations] + nju_nodes
        links = [l for l in links if l['locator'] not in nju_locations] + nju_links
        fudan_nodes, fudan_links, fudan_locations = fudan_academic_relationships(soup, page_url, root_url, owner_key)
        nodes = [n for n in nodes if n['locator'] not in fudan_locations] + fudan_nodes
        links = [l for l in links if l['locator'] not in fudan_locations] + fudan_links
        from .medical_directories import medical_relationships
        medical_nodes, medical_links, medical_locations, medical_notes = medical_relationships(soup, page_url, root_url, owner_key)
        nodes = [n for n in nodes if n['locator'] not in medical_locations] + medical_nodes
        links = [l for l in links if l['locator'] not in medical_locations] + medical_links
        notes.extend(medical_notes)
        from .academic_directories import academic_relationships
        academic_nodes, academic_links, academic_locations, academic_notes = academic_relationships(
            soup, page_url, root_url, owner_key)
        nodes = [n for n in nodes if n['locator'] not in academic_locations] + academic_nodes
        links = [l for l in links if l['locator'] not in academic_locations] + academic_links
        notes.extend(academic_notes)
    if profile:
        profile_nodes, profile_links, profile_locations = profile_directory_relationships(profile, page_url, root_url, owner_key)
        nodes = [n for n in nodes if n['locator'] not in profile_locations] + profile_nodes
        links = [l for l in links if l['locator'] not in profile_locations] + profile_links
    from .script_directories import zju_relationships
    script_nodes, script_links, script_notes = zju_relationships(soup, page_url, root_url, owner_key)
    nodes.extend(script_nodes)
    links.extend(script_links)
    notes.extend(script_notes)
    from .major_directories import major_relationships
    major_nodes, major_links, major_locations, major_notes = major_relationships(soup, page_url, owner_key)
    nodes = [n for n in nodes if n['locator'] not in major_locations] + major_nodes
    links = [l for l in links if l['locator'] not in major_locations] + major_links
    notes.extend(major_notes)
    if major_nodes:
        notes.append('reviewed_major_directory')
    if soup.select('iframe[src]'):
        notes.append('embedded_frame_requires_review')
    if any(re.search(r'ajax|fetch\(|\.getJSON|document\.write', s.get_text(), re.I) for s in soup.find_all('script')):
        notes.append('script_generated_content_requires_review')
    if not links:
        notes.append('no_structural_links_detected')
    if '未指定栏目或指定的栏目不存在' in soup.get_text(' ', strip=True):
        notes.append('official_template_error_requires_review')
    for message in soup.select('.wp_error > .wp_error_msg'):
        text = clean(message.get_text(' ', strip=True))
        if '访问地址无效' in text and '找不到对应的栏目' in text:
            notes.append('official_template_error_requires_review')
            notes.append('official_template_error_message:' + text)
    variants = directory_variants(soup, page_url)
    if variants:
        notes.append('directory_variant_differences:' + json.dumps(variants, ensure_ascii=False))
    from backend.services.source_ownership import branding_candidates
    return {'links': links, 'nodes': reconcile_unlinked_copies(nodes), 'notes': notes,
            'branding_candidates': branding_candidates(soup),
            'title': clean(soup.title.get_text()) if soup.title else ''}


def add_publication_structure(parsed, evidence):
    """Represent a proven page widget without inventing administrative ancestry."""
    owner = next((n for n in parsed['nodes'] if n['relation'] == 'page_identity'), None)
    if owner is None or not evidence:
        return
    # Article titles can contain institution names or words such as "通知".
    # Correct only the proven article occurrences; a directory/navigation link
    # elsewhere on the page may legitimately share an article-shaped address.
    articles = {(link['url'], link['locator'])
                for column in evidence.get('lists', [evidence])
                for link in column.get('article_links', [])}
    if articles:
        parsed['nodes'] = [n for n in parsed['nodes'] if n['relation'] == 'page_identity'
                           or (n['url'], n['locator']) not in articles]
        for link in parsed['links']:
            if ((link['url'], link['locator']) in articles and link['decision'] in
                    ('follow', 'official_external_link', 'external_review', 'article_reference')):
                link['decision'] = 'article_reference'
    for column in evidence.get('lists', [evidence]):
        name = column.get('name')
        container = column.get('container_locator')
        heading = column.get('heading_locator')
        if not name or not container or not heading or column.get('heading_ambiguous'):
            continue
        target = column.get('column_url') or ''
        position = column.get('column_link_locator') if target else heading
        if not position:
            continue
        # A menu or heading already recorded at this exact location keeps its
        # existing parent and identity. Other appearances remain separate evidence.
        if any(n['name'] == name and n['url'] == target and n['locator'] == position
               and n['relation'] in ('navigation_entry', 'unit_channel', 'publication_column')
               for n in parsed['nodes']):
            continue
        identity = target or owner['url'] + '#publication-list:' + container
        parent_key = owner['key']
        if column.get('column_group_name') and column.get('column_group_locator'):
            group_name, group_position = column['column_group_name'], column['column_group_locator']
            parent_key = node_key('group', group_name, owner['url'] + '#publication-group:' + group_position)
            if not any(n['key'] == parent_key for n in parsed['nodes']):
                parsed['nodes'].append({'key': parent_key, 'name': group_name, 'kind': 'group', 'url': '',
                                        'parent': owner['key'], 'locator': group_position,
                                        'relation': 'publication_group'})
        parsed['nodes'].append({'key': node_key('channel', name, identity), 'name': name,
                                'kind': 'channel', 'url': target, 'parent': parent_key,
                                'locator': position, 'relation': 'publication_column'})


def publication_evidence(html, url):
    from .publication_lists import publication_lists
    feeds = publication_lists(html, url)
    if not feeds:
        return None
    latest = [f['latest_publication'] for f in feeds if f.get('latest_publication')]
    return dict(feeds[0], lists=feeds, latest_publication=max(latest) if latest else None)


def health_from_evidence(html, latest=None):
    text = BeautifulSoup(html, 'lxml').get_text(' ', strip=True)
    if re.search(r'(本站|本网站|本栏目).{0,12}(停止更新|停止维护|停用|关闭)|已迁移至|启用新网站', text):
        return 'retirement_notice'
    if latest:
        age = (datetime.now(timezone.utc).date() - datetime.fromisoformat(latest).date()).days
        return 'stale' if age > 730 else ('infrequent' if age > 365 else 'recent_publication')
    return 'date_unknown'
