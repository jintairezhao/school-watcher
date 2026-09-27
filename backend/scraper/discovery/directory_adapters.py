"""Small, evidence-based adapters for official directories with nonsemantic markup.

Selectors describe observed page structure; they do not supply the roster itself.
Independent reviewed rosters belong in data/source_baselines, not in a parser.
"""
from urllib.parse import urlsplit
import re


def tsinghua_directory(page_url):
    page = urlsplit(page_url)
    return page.hostname == 'www.tsinghua.edu.cn' and page.path == '/yxsz.htm'


def heading_relation(soup, heading, page_url):
    if (tsinghua_directory(page_url) and '为非实体学院' in re.sub(r'\s+', '', soup.get_text()) and
            re.fullmatch(r'\*\s*.+学院\s*\*', heading.get_text(' ', strip=True))):
        return 'non_entity_directory_entry'
    return 'directory_entry'


def directory_variants(soup, page_url):
    """Compare explicit views without choosing which official version is correct."""
    from .medical_directories import medical_directory, medical_variants
    if medical_directory(page_url):
        return medical_variants(soup, page_url)
    from .directory_labels import fudan_directory, fudan_directory_variants
    if fudan_directory(page_url):
        return fudan_directory_variants(soup, page_url)
    if not tsinghua_directory(page_url):
        return []
    from .structure import clean
    from backend.services.source_inventory import canonical_url
    from urllib.parse import urljoin
    views = []
    selectors = [('树状目录', '.yxszCon > .setL > div, .yxszCon > .setR > div'),
                 ('移动版目录', '.yxszConMobile > .MCon'), ('字母索引', '.orgaCon dd')]
    for label, selector in selectors:
        blocks = soup.select(selector)
        if not blocks:
            continue
        entries = {}
        for block in blocks:
            heading = block.find(['h3', 'h4'], recursive=False)
            if heading is None:
                continue
            name = clean(heading.get_text(' ', strip=True))
            anchor = heading.find('a')
            href = anchor.get('href', '') if anchor else ''
            entries.setdefault((name, ''), set()).add(canonical_url(urljoin(page_url, href)) if href else '')
            listing = heading.find_next_sibling()
            if listing and listing.name in ('ul', 'p'):
                for child in listing.select('a'):
                    href = child.get('href', '')
                    entries.setdefault((clean(child.get_text(' ', strip=True)), name), set()).add(
                        canonical_url(urljoin(page_url, href)) if href else '')
        views.append((label, entries))
    if len(views) < 2:
        return []
    result = []
    for identity in sorted(set().union(*(set(entries) for _, entries in views))):
        signatures = [frozenset(entries[identity]) if identity in entries else None for _, entries in views]
        if len(set(signatures)) > 1:
            result.append({'name': identity[0], 'parent': identity[1], 'versions': [
                {'name': label, 'present': identity in entries, 'urls': sorted(entries.get(identity, set()))}
                for label, entries in views]})
    return result


def heading_lists(soup, page_url):
    page = urlsplit(page_url)
    if page.hostname == 'www.pku.edu.cn' and page.path == '/department.html':
        for heading in soup.select('.text > .fz30'):
            listing = heading.find_next_sibling()
            if listing and 'links' in listing.get('class', []):
                yield heading, listing
    if page.hostname == 'www.whu.edu.cn' and page.path == '/jgsz/yxsz.htm':
        for heading in soup.select('.top > h4'):
            listing = heading.parent.find_next_sibling()
            if listing and 'bottom' in listing.get('class', []):
                listing = listing.find('ul', class_='list21', recursive=False)
            if listing and listing.name == 'ul' and 'list21' in listing.get('class', []):
                yield heading, listing


def row_hierarchy(soup, page_url):
    """USTC's roster encodes groups, colleges and departments in explicit row styles."""
    page = urlsplit(page_url)
    if page.hostname != 'www.ustc.edu.cn' or page.path != '/yxjs.htm':
        return
    sections = {'学部/学院', '国家级科研平台', '校地合作机构'}
    for table in soup.select('.wp_articlecontent > table'):
        section = primary = secondary = None
        for row in table.find_all('tr'):
            if row.find_parent('table') is not table:
                continue
            cells = row.find_all('td', recursive=False)
            anchors = [a for cell in cells for a in cell.find_all('a')]
            if len(anchors) != 1:
                primary = secondary = None
                continue
            anchor = anchors[0]
            name = anchor.get_text(' ', strip=True)
            if not name:
                continue
            main_row = any('line01' in c.get('class', []) and c.get('colspan') == '2' for c in cells)
            if main_row:
                if name in sections:
                    yield anchor, None, 'group'
                    section, primary, secondary = anchor, None, None
                else:
                    yield anchor, section, 'unit'
                    primary, secondary = anchor, None
            elif primary is not None and any('line02' in c.get('class', []) for c in cells):
                bold = bool(re.search(r'font-weight\s*:\s*(?:bold|[7-9]00)', anchor.get('style', ''), re.I))
                yield anchor, primary if bold else (secondary if secondary is not None else primary), 'unit'
                if bold:
                    secondary = anchor
            else:
                primary = secondary = None


def has_adapter(page_url):
    page = urlsplit(page_url)
    return (page.hostname, page.path) in {('www.pku.edu.cn', '/department.html'),
                                         ('www.shutcm.edu.cn', '/ejxy/list.htm'),
                                         ('www.csust.edu.cn', '/jgsz/jxy.htm'),
                                         ('www.muc.edu.cn', '/zzjg/jxhkydw1.htm'),
                                         ('www.hubu.edu.cn', '/zzjg/xbxy.htm'),
                                         ('www.tsinghua.edu.cn', '/yxsz.htm'),
                                         ('www.whu.edu.cn', '/jgsz/yxsz.htm'),
                                         ('www.sdu.edu.cn', '/zzjg/xysz.htm'),
                                         ('www.buaa.edu.cn', '/jgsz/jxkyjg02.htm'),
                                         ('www.nju.edu.cn', '/xybm.htm'),
                                         ('www.fudan.edu.cn', '/489/list.htm'),
                                         ('www.cmm.zju.edu.cn', '/55143/list.htm'),
                                         ('www.cmm.zju.edu.cn', '/55144/list.htm'),
                                         ('www.ustc.edu.cn', '/yxjs.htm')}


def campus_relationships(soup, page_url, root_url, owner_key):
    """SDU lists locations, including the same college at more than one campus."""
    page = urlsplit(page_url)
    nodes, links, replaced = [], [], set()
    if (page.hostname, page.path) != ('www.sdu.edu.cn', '/zzjg/xysz.htm'):
        return nodes, links, replaced
    from urllib.parse import urljoin
    from .structure import clean, classify, locator, node_key
    from backend.services.source_inventory import canonical_url
    from backend.scraper.http_client import same_school_url

    def add(element, name, kind, parent, relation, anchor=None):
        href = (anchor.get('href') or '').strip() if anchor is not None else ''
        url = canonical_url(urljoin(page_url, href)) if href and not href.startswith(
            ('#', 'javascript:', 'mailto:', 'tel:')) else ''
        loc = locator(element)
        key = node_key(kind, name, url or (page_url + '#parent:' + parent))
        nodes.append({'key': key, 'name': name, 'kind': kind, 'url': url, 'parent': parent,
                      'locator': loc, 'relation': relation})
        links.append({'label': name, 'url': url, 'kind': 'unit' if kind == 'unit' or relation == 'campus_group' else 'navigation',
                      'path': [], 'locator': loc, 'decision': 'missing_link' if not url else
                      'follow' if same_school_url(url, root_url) else 'official_external_link'})
        return key

    for block in soup.select('.nygljg > .wp > dl'):
        headings = block.select(':scope > dt, :scope > a > dt')
        if len(headings) != 1:
            continue
        heading = headings[0]
        name = re.match(r'^(.{1,20}?校区)(?:\s|$)', heading.get_text(' ', strip=True))
        if not name:
            continue
        anchor = heading.parent if heading.parent.name == 'a' else heading.find('a')
        campus = add(heading, name.group(1), 'group', owner_key, 'campus_group', anchor)
        for item in block.select(':scope > dd > ul > li > h4'):
            label = clean(item.get_text(' ', strip=True))
            if not label:
                continue
            child = item.find('a')
            add(child if child is not None else item, label, 'unit' if classify(label) == 'unit' else 'group',
                campus, 'campus_directory_entry', child)
        replaced.update(locator(tag) for tag in [block, *block.find_all(True)])
    return nodes, links, replaced


def buaa_academic_relationships(soup, page_url, root_url, owner_key):
    """Keep BUAA's groups, parallel row entries and explicit secondary labels.

    The xbt style describes a directory position, not administrative ownership.
    Inline-hidden entries remain evidence but cannot establish a roster owner.
    """
    page = urlsplit(page_url)
    nodes, links, replaced = [], [], set()
    if (page.hostname, page.path) != ('www.buaa.edu.cn', '/jgsz/jxkyjg02.htm'):
        return nodes, links, replaced
    from .structure import clean, locator, node_key
    from backend.services.source_inventory import resolve_page_link
    from backend.scraper.http_client import same_school_url

    def add(element, name, kind, parent, relation, anchor=None):
        url = resolve_page_link(page_url, anchor.get('href', '')) if anchor is not None else ''
        loc = locator(element)
        key = node_key(kind, name, (url or page_url) + '#position:' + loc)
        nodes.append({'key': key, 'name': name, 'kind': kind, 'url': url, 'parent': parent,
                      'locator': loc, 'relation': relation})
        links.append({'label': name, 'url': url, 'kind': 'unit' if kind == 'unit' else 'navigation',
                      'path': [], 'locator': loc, 'decision': 'missing_link' if not url else
                      'follow' if same_school_url(url, root_url) else 'official_external_link'})
        return key

    def hidden(element, block):
        while element is not None:
            if element.has_attr('hidden') or re.search(
                    r'(?:^|;)\s*display\s*:\s*none\s*(?:!important\s*)?(?:;|$)',
                    element.get('style', ''), re.I):
                return True
            if element is block:
                break
            element = element.parent
        return False

    for block in soup.select('.kyjg > .kyjg-box'):
        headings = block.select(':scope > .kyjg-tit > h3')
        rows = block.select(':scope > .kyjg-bd > ul > li')
        if len(headings) != 1 or not rows:
            continue
        heading = headings[0]
        name = clean(heading.get_text(' ', strip=True))
        if not name:
            continue
        group = add(heading, name, 'group', owner_key, 'academic_group')
        for row in rows:
            primaries = [a for a in row.find_all('a', recursive=False)
                         if 'xbt' not in a.get('class', []) and clean(a.get_text(' ', strip=True))]
            companions = row.select(':scope > a.xbt, :scope > .xbt a')
            parent = group
            if len(primaries) > 1:
                label = ' / '.join(clean(a.get_text(' ', strip=True)) for a in primaries)
                parent = add(row, label, 'group', group, 'shared_directory_row')
            primary_keys = []
            for anchor in primaries:
                relation = 'same_directory_row' if len(primaries) > 1 else 'directory_entry'
                if hidden(anchor, block):
                    relation = 'hidden_directory_entry'
                primary_keys.append(add(anchor, clean(anchor.get_text(' ', strip=True)),
                                        'unit', parent, relation, anchor))
            companion_parent = primary_keys[0] if len(primary_keys) == 1 else parent
            for anchor in companions:
                label = clean(anchor.get_text(' ', strip=True))
                if not label:
                    continue
                combined = '/' in label or '／' in label
                relation = 'shared_directory_label' if combined else 'directory_companion_entry'
                if hidden(anchor, block):
                    relation = 'hidden_directory_entry'
                add(anchor, label, 'group' if combined else 'unit', companion_parent, relation, anchor)
        replaced.update(locator(tag) for tag in [block, *block.find_all(True)])
    return nodes, links, replaced


def nju_department_relationships(soup, page_url, root_url, owner_key):
    """NJU's category lists contain h4 entries and explicit expandable bm-er lists.

    A shared address is not an alias, and a collapsed list is not an inactive unit.
    Preserve labels with internal spaces, including combined institutional names.
    """
    page = urlsplit(page_url)
    nodes, links, replaced = [], [], set()
    if (page.hostname, page.path) != ('www.nju.edu.cn', '/xybm.htm'):
        return nodes, links, replaced
    from .structure import locator, node_key
    from backend.services.source_inventory import resolve_page_link
    from backend.scraper.http_client import same_school_url

    def add(element, parent, kind, relation, anchor=None):
        name = re.sub(r'\s+', ' ', element.get_text(' ', strip=True)).strip()
        if not name:
            return None
        url = resolve_page_link(page_url, anchor.get('href')) if anchor is not None else ''
        loc = locator(element)
        key = node_key(kind, name, (url or page_url) + '#position:' + loc)
        nodes.append({'key': key, 'name': name, 'kind': kind, 'url': url, 'parent': parent,
                      'locator': loc, 'relation': relation})
        links.append({'label': name, 'url': url, 'kind': 'unit' if kind == 'unit' else 'directory',
                      'path': [], 'locator': loc, 'decision': 'missing_link' if not url else
                      'follow' if same_school_url(url, root_url) else 'official_external_link'})
        return key

    for block in soup.select('.yxbm > ul'):
        headings = block.select(':scope > h3.wl')
        if len(headings) != 1:
            continue
        group = add(headings[0], owner_key, 'group', 'directory_group')
        if group is None:
            continue
        replaced.add(locator(headings[0]))
        for row in block.select(':scope > li'):
            primary = row.select(':scope > h4.wl > a')
            if len(primary) != 1:
                continue
            parent = add(primary[0], group, 'unit', 'directory_entry', primary[0])
            if parent is None:
                continue
            for child in row.select(':scope > .bm-er > a'):
                add(child, parent, 'unit', 'nested_directory_entry', child)
            replaced.update(locator(tag) for tag in [row, *row.find_all(True)])
    return nodes, links, replaced


def profile_directory_relationships(profile, page_url, root_url, owner_key):
    """A reviewed sidebar is a unit-introduction directory, not a set of feeds."""
    from .structure import clean, classify, locator, node_key
    from backend.services.source_inventory import resolve_page_link
    from backend.scraper.http_client import same_school_url
    nodes, links, replaced = [], [], set()

    def visit(row, parent):
        anchor = row.find('a', recursive=False)
        if anchor is None:
            return
        name = clean(anchor.get_text(' ', strip=True))
        if not name:
            return
        target = resolve_page_link(page_url, anchor.get('href'))
        loc = locator(anchor)
        kind = 'unit' if classify(name) == 'unit' else 'group'
        key = node_key(kind, name, (target or page_url) + '#directory-position:' + loc)
        nodes.append({'key': key, 'name': name, 'url': target, 'kind': kind, 'parent': parent,
                      'locator': loc, 'relation': 'unit_profile_entry'})
        links.append({'label': name, 'url': target, 'kind': 'unit' if kind == 'unit' else 'navigation',
                      'path': [], 'locator': loc, 'decision': 'missing_link' if not target else
                      'follow' if same_school_url(target, root_url) else 'official_external_link'})
        for child in row.select(':scope > ul.wp_subcolumn > li'):
            visit(child, key)

    branch = profile['branch']
    visit(branch, owner_key)
    replaced.update(locator(tag) for tag in [branch, *branch.find_all(True)])
    return nodes, links, replaced


def fudan_academic_relationships(soup, page_url, root_url, owner_key):
    from .directory_labels import fudan_directory, directory_label
    from .structure import clean, locator, node_key
    from backend.services.source_inventory import resolve_page_link
    from backend.scraper.http_client import same_school_url
    nodes, links, replaced = [], [], set()
    if not fudan_directory(page_url):
        return nodes, links, replaced
    for block in soup.select('.col_news_list .part_xy > li.column-1'):
        headings = block.select(':scope > h3')
        if len(headings) != 1:
            continue
        heading = headings[0]
        name = clean(heading.get_text(' ', strip=True))
        group = node_key('group', name, page_url + '#section:' + locator(heading))
        nodes.append({'key': group, 'name': name, 'kind': 'group', 'url': '', 'parent': owner_key,
                      'locator': locator(heading), 'relation': 'directory_group'})
        for anchor in block.select(':scope > .sub-con > .sub-list > li > a'):
            name, evidence = directory_label(anchor, soup, page_url)
            if not name:
                continue
            target = resolve_page_link(page_url, anchor.get('href'))
            key = node_key('unit', name, target or (page_url + '#parent:' + group))
            relation = 'directory_label_pending' if evidence and evidence['status'] == 'unresolved' else 'directory_entry'
            loc = locator(anchor)
            nodes.append({'key': key, 'name': name, 'kind': 'unit', 'url': target, 'parent': group,
                          'locator': loc, 'relation': relation})
            links.append({'label': name, 'url': target, 'kind': 'unit', 'path': [], 'locator': loc,
                          'decision': 'missing_link' if not target else 'follow' if same_school_url(target, root_url)
                          else 'official_external_link'})
        replaced.update(locator(tag) for tag in [block, *block.find_all(True)])
    return nodes, links, replaced
