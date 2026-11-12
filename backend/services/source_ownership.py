"""Evidence for university units hosted outside the university's primary domain.

An ordinary mention or friendly link is not a site's identity. These records
support candidate discovery only; independent source review is still required.
"""
import json
import re
import unicodedata

from bs4 import BeautifulSoup

from backend.scraper.http_client import official_domain, same_school_url

PREFIX = 'external_identity_evidence:'
BRANDING_PREFIX = 'page_branding_evidence:'
PROFILE_PREFIX = 'unit_profile_identity_evidence:'
PROFILE_LABELS = frozenset(('学院概况', '学院简介', '中心概况', '中心简介', '中心介绍', '单位概况', '单位简介',
                            '部门职责', '部门职能', '机构职能', '工作职责', '现任领导及职能'))


def _visible_profile_text(node):
    return node is not None and node.find_parent(['blockquote', 'q']) is None and not any(
        parent.has_attr('hidden') or 'display:none' in re.sub(r'\s+', '', parent.get('style', '')).lower()
        for parent in [node, *node.parents] if getattr(parent, 'attrs', None) is not None)


def profile_identity_candidates(soup):
    """Opening self-descriptions only; using one also requires a profile route.

    These observations are separate from site branding. A title disagreement
    remains recorded and does not turn into an alias or a renamed organization.
    """
    from backend.scraper.discovery.structure import locator
    containers = soup.select('.c-right-detail__content, .v_news_content, .wp_articlecontent, '
                             'article .entry-content, main article, .sub-right, #vsb_content, .WordSection1')
    pattern = re.compile(r'^(?:校区|本校)?(?P<name>[\u3400-\u9fffA-Za-z0-9·（）()]{2,45}?'
        r'(?:学院|中心|研究院|研究所|学部|部门))'
        r'(?:(?:依托|由)[^。！？]{2,120}?(?:组建成立|组建而成)|成立于\s*[一二三四五六七八九十\d]{4}'
        r'|隶属于[\u3400-\u9fffA-Za-z0-9·（）()]{2,60}?'
        r'(?:大学|学院|学校|中心|研究院|研究所|学部|部门|办公室|图书馆|部|处)(?=[，,。；;]))')
    # Responsibility statements have a distinct, explicit context. A news page
    # that merely repeats the same sentence is not an introductory profile.
    responsibility_page = bool(soup.title and re.match(
        r'^(?:部门职责|部门职能|机构职能|工作职责|现任领导及职能)(?:\s*[-—_|·：:]|$)',
        soup.title.get_text(' ', strip=True)))
    department = re.compile(r'^(?P<name>[\u3400-\u9fffA-Za-z0-9·]{2,45}?'
        r'(?:学院|中心|研究院|研究所|学部|部门|办公室|图书馆|院|部|处)'
        r'(?:[（(][\u3400-\u9fffA-Za-z0-9·]{2,45}[）)])?)'
        r'是(?:学校)?(?:负责|主管)[^。！？]{3,120}的职能(?:部门|机构)(?=[。；;])')
    result = []
    for container in containers:
        if any(other is not container and container in other.parents for other in containers):
            continue
        paragraphs = [p for p in container.select('p') if p.get_text(' ', strip=True)]
        paragraph = paragraphs[0] if paragraphs else None
        if not _visible_profile_text(paragraph):
            continue
        text = paragraph.get_text(' ', strip=True)
        match = pattern.match(text)
        if not match and responsibility_page:
            heading = re.fullmatch(r'(?P<name>[\u3400-\u9fffA-Za-z0-9·（）()]{2,90}?)(?:主要)?工作职责', text)
            if heading:
                # Skip only one explicit subject heading, not an unrelated
                # opening paragraph or a later mention elsewhere in the page.
                paragraph = paragraphs[1] if len(paragraphs) > 1 else None
                if not _visible_profile_text(paragraph):
                    continue
                text = paragraph.get_text(' ', strip=True)
            match = department.match(text)
            if match and heading:
                name, title = normalized_name(match['name']), normalized_name(heading['name'])
                prefix = title[:-len(name)] if title.endswith(name) else None
                if prefix is None or prefix and not re.fullmatch(r'[\u3400-\u9fff]{2,40}大学', prefix):
                    match = None
        if match and 30 <= len(text) <= 1500:
            # The subject identifies this profile. A named parent in the text
            # remains raw evidence, not a saved administrative relationship.
            result.append(dict(source='unit_profile_self_description', entity_name=match['name'],
                identity=text, locator=locator(paragraph), container=locator(container)))
    return result


def branding_candidates(soup):
    """Collect explicit site identity, never general body mentions or footlinks."""
    from backend.scraper.discovery.structure import locator
    candidates = []
    if soup.title:
        candidates.append({'source': 'title', 'identity': soup.title.get_text(' ', strip=True),
                           'locator': locator(soup.title)})
    for tag in soup.select('meta[property="og:site_name"]'):
        if tag.get('content', '').strip():
            candidates.append({'source': 'og:site_name', 'identity': tag['content'].strip(),
                               'locator': locator(tag)})
    for tag in soup.select('header img[alt], .logo img[alt], #logo img[alt]'):
        if tag.get('alt', '').strip():
            candidates.append({'source': 'header_logo_alt', 'identity': tag['alt'].strip(),
                               'locator': locator(tag)})
    from backend.scraper.article_observation import article_node_state
    footer_marker = re.compile(r'(?:^|[\s_-])footer(?:$|[\s_-])', re.I)
    partner_marker = re.compile(r'(?:^|[\s_-])(?:friend|friends|link|links|partner|partners)(?:$|[\s_-])', re.I)
    for footer in soup.find_all(['footer', 'div', 'section']):
        marker = ' '.join(footer.get('class', [])) + ' ' + footer.get('id', '')
        if footer.name != 'footer' and footer.get('role') != 'contentinfo' and not footer_marker.search(marker):
            continue
        for tag in footer.find_all(['div', 'p', 'span', 'small'], recursive=False):
            marker = ' '.join(tag.get('class', [])) + ' ' + tag.get('id', '')
            # Friend lists and nested blocks cannot become a site's signature.
            if (partner_marker.search(marker) or article_node_state(tag) != 'ready'
                    or any(node.name not in ('span', 'small', 'strong', 'b', 'em') for node in tag.find_all(True))):
                continue
            text = tag.get_text(' ', strip=True)
            if (not re.fullmatch(r'[\u3400-\u9fff（）()·\s]{4,100}', text)
                    or not re.search(r'大学|学院', text)
                    or not re.search(r'(?:学院|中心|研究院|研究所|学部|部门|办公室|图书馆|处|部)$', text)):
                continue
            position = locator(tag)
            if not any(item['locator'] == position for item in candidates):
                candidates.append(dict(source='footer_signature', identity=text, locator=position))
    return candidates


def normalized_name(value):
    return re.sub(r'[\s（）()·|_-]+', '', unicodedata.normalize('NFKC', value or ''))


def profile_subject_matches(observed, unit_name, school_name=''):
    """Match a literal unit subject, optionally prefixed by the known school.

    This is an identity-text check, not a page alias or entity merge rule.
    """
    if not all(isinstance(value, str) for value in (observed, unit_name, school_name)):
        return False
    subject, unit, school = map(normalized_name, (observed, unit_name, school_name))
    return bool(unit and (subject == unit or school and not unit.startswith(school)
                         and subject == school + unit))


def website_identity_forms(value):
    """Exact branding, allowing only explicit homepage prefixes/repeated titles.

    Do not use substring matching: a news headline mentioning a college is not
    that college's website identity.
    """
    value = unicodedata.normalize('NFKC', value or '').strip()
    forms = {normalized_name(value)}
    home = re.sub(r'^(?:首页|主页|Home)\s*[-—–_|·:]\s*', '', value, flags=re.I)
    forms.add(normalized_name(home))
    parts = [normalized_name(p) for p in re.split(r'\s*[|｜]\s*', home) if p.strip()]
    if parts and len(set(parts)) == 1:
        forms.add(parts[0])
    return forms


def website_identity_names(unit_name, school_name='', *, allow_unqualified=True, column_name=''):
    """Complete, known publisher names shared by validation and graph readers."""
    unit, school = normalized_name(unit_name), normalized_name(school_name)
    if not unit:
        return set()
    names = {unit if not school or unit.startswith(school) else school + unit}
    if allow_unqualified:
        names.add(unit)
    expected = {name + suffix for name in names
                for suffix in ('', '首页', '官网', '官方网站', '网站', '门户网站')}
    column = normalized_name(column_name)
    if column:
        # A cross-site list may title itself "单位名通知公告". Its complete
        # checked column name may surround the complete publisher, never a
        # free substring or an arbitrary remainder of a longer department.
        expected.update(name + column for name in names)
        expected.update(column + name for name in names)
    return expected


def identity_evidence(html, site, page, final_url, content_hash):
    if page['kind'] not in ('unit', 'root'):
        return None
    school = normalized_name(site['name'])
    unit = normalized_name(re.sub(r'[（(]共建[）)]$', '', page['label']))
    identity = unit if unit.startswith(school) else school + unit
    if not school or not unit:
        return None
    soup = BeautifulSoup(html, 'lxml')
    # Exact site identity prevents "某大学代表团来访" in a news title from
    # authorizing a partner's entire website as part of that university.
    expected = {identity + suffix for suffix in ('', '首页', '官网', '官方网站', '网站', '门户网站')}
    candidates = []
    if soup.title:
        candidates.append(('title', soup.title.get_text(' ', strip=True)))
    for tag in soup.select('meta[property="og:site_name"]'):
        candidates.append(('meta[property="og:site_name"]', tag.get('content', '')))
    for tag in soup.select('header img[alt], .logo img[alt], #logo img[alt]'):
        candidates.append(('header/logo img[alt]', tag.get('alt', '')))
    for locator, label in candidates:
        if normalized_name(label) in expected:
            return {'site_key': site['site_key'], 'reference_url': page['url'],
                    'final_url': final_url, 'content_hash': content_hash,
                    'domain': official_domain(final_url), 'locator': locator, 'identity': label}
    return None


def domain_evidence(pages, site_key, target):
    """Use only current, fetched branding evidence, including its snapshot hash."""
    for page in pages:
        if page['state'] != 'fetched':
            continue
        for note in json.loads(page.get('notes_json') or '[]'):
            if not isinstance(note, str) or not note.startswith(PREFIX):
                continue
            try:
                evidence = json.loads(note[len(PREFIX):])
                reference = page.get('final_url') or page['url']
                if (evidence['site_key'] == site_key and evidence['reference_url'] == page['url']
                        and evidence['content_hash'] == page.get('content_hash')
                        and evidence['domain'] == official_domain(reference)
                        and same_school_url(target, reference)):
                    return evidence
            except (ValueError, TypeError, KeyError):
                continue
    return None


def saved_domain_evidence(inventory, site_key, target):
    with inventory.connect() as connection:
        pages = [dict(row) for row in connection.execute(
            "SELECT * FROM pages WHERE site_key=? AND state='fetched' AND notes_json LIKE ?",
            (site_key, '%' + PREFIX + '%'))]
    return domain_evidence(pages, site_key, target)
