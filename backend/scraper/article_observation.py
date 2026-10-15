"""Title-bound article observations, independent of a site's CSS class names.

Never use all-page text as an article body. Walk the observed title's enclosing
regions, stopping at prose, and require local publication metadata for a custom
body fallback. No date is inferred from an address, the clock or article prose.
"""
import re
from copy import deepcopy
from datetime import datetime
from bs4 import BeautifulSoup

DATE = re.compile(r'(?<!\d)(\d{4})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})日?(?!\d)')
PUBLISH = re.compile(r'^(?:发布日期|发布时间|发文日期|发布于)\s*[：:]?\s*')
COMPACT_PUBLISH = re.compile(r'(?:发布日期|发布时间|发文日期|发布于)\s*[：:]?\s*'
                             r'(\d{4})(\d{2})(\d{2})(?!\d)')
EDITOR = re.compile(r'责任编辑|编辑[:：]|作者[:：]|来源[:：]|浏览(?:量|次数)?[:：]')
NOISE = re.compile(r'(?:^|[\s_-])(?:nav|navigation|menu|sidebar|breadcrumb|related|recommend|toolbar|footer)(?:$|[\s_-])', re.I)
BODY_HEADER = re.compile(r'(?:^|[\s_-])header(?:$|[\s_-])', re.I)
LOADING_BODY = re.compile(r'^(?:(?:正文|文章|页面|内容|数据)\s*)?(?:正在)?(?:加载|读取|获取)(?:中)?'
    r'(?:[，,。.…\s]+(?:请稍候|请稍等|请等待|正在(?:加载|读取|获取)|稍后重试).*)?[。.…\s]*$'
    r'|^loading(?:\.{1,3}|…)?$', re.I)


def title_key(value):
    return re.sub(r'\W+', '', value or '').casefold()


def compact_publication_date(value):
    """An actual YYYYMMDD publication field, never an ID, event date or URL."""
    if not PUBLISH.match(value or ''):
        return None
    matches = COMPACT_PUBLISH.findall(value)
    if len(matches) != 1:
        return None
    try:
        return datetime(*map(int, matches[0]))
    except ValueError:
        return None


def article_node_state(node):
    """Read visibility/loading on this material and its enclosing region."""
    for current in [node, *node.parents]:
        if current.attrs is None:
            return 'hidden'
        style = re.sub(r'\s+', '', current.get('style', '')).lower()
        if (current.name in ('script', 'style', 'template', 'noscript') or current.has_attr('hidden')
                or str(current.get('aria-hidden', '')).lower() == 'true'
                or 'display:none' in style or 'visibility:hidden' in style or 'visibility:collapse' in style):
            return 'hidden'
        if any(str(current.get(key, '')).lower() in ('true', '1') for key in ('aria-busy', 'data-loading')):
            return 'pending'
    text = node.get_text(' ', strip=True)
    return 'pending' if len(text) <= 160 and LOADING_BODY.fullmatch(text) else 'ready'


def prune_unready_article_nodes(region):
    """Remove hidden/loading nodes in place; return the observed reasons."""
    removed = set()
    for node in list(region.find_all(True)):
        if node.attrs is None:
            continue
        state = article_node_state(node)
        if state != 'ready':
            removed.add(state)
            node.decompose()
    return removed


def article_material(region, url):
    """Return ready material from this body region, excluding hidden/busy parts.

    A remaining headline/publication line cannot replace an unfinished body.
    Unrelated busy widgets do not block substantive ready prose or attachments.
    """
    from backend.scraper.article_resources import has_public_body_resource
    state = article_node_state(region)
    if state != 'ready':
        return None, state
    clean = deepcopy(region)
    removed = prune_unready_article_nodes(clean)
    if removed:
        proof = deepcopy(clean)
        for node in list(proof.select('h1,h2,h3,h4,h5,h6,[role="heading"],nav,aside,footer,a,button')):
            node.decompose()
        metadata = [(node, node.get_text(' ', strip=True)) for node in proof.find_all(['div', 'p', 'span', 'time'])
                    if PUBLISH.match(node.get_text(' ', strip=True)) and len(node.get_text(' ', strip=True)) <= 220]
        for node, _ in _metadata_leaves(metadata):
            node.decompose()
        if len(proof.get_text(' ', strip=True)) < 8 and not has_public_body_resource([clean], url):
            return None, 'pending' if 'pending' in removed else 'hidden'
    return clean, 'ready'


def navigation_only_body(html, url):
    """Recognize a cached column menu/breadcrumb with no article material.

    Keep short notices and attachment/link-only bodies. Only a home breadcrumb
    or column heading plus links, with nothing beyond the label 正文, is stale.
    """
    from backend.scraper.article_resources import has_public_body_resource
    if not html:
        return False
    soup = BeautifulSoup(html, 'lxml')
    links = soup.select('a[href]')
    if len(links) < 2 or not any(a.get_text(' ', strip=True) in ('首页', 'Home') for a in links):
        return False
    if not soup.select('h1,h2,h3,h4,h5,h6') and '正文' not in soup.get_text():
        return False
    if has_public_body_resource([soup], url):
        return False
    for node in soup.select('a,h1,h2,h3,h4,h5,h6,script,style,iframe,button'):
        node.decompose()
    return re.sub(r'[\W_]+', '', soup.get_text()) in ('', '正文')


def _noise(node):
    return article_node_state(node) == 'hidden' or any(parent.name in ('nav','aside','footer','blockquote','script','style')
        or parent.name=='form' and parent.select('input:not([type="hidden"]),textarea,select,button')
        or NOISE.search(' '.join(parent.get('class', []))+' '+parent.get('id',''))
        for parent in [node]+list(node.parents) if parent.attrs is not None)


def article_heading(soup, title):
    key = title_key(title)
    if not key:
        return None
    heads = [node for node in soup.select('h1,h2,h3,[itemprop="headline"]')
        if not _noise(node) and not node.find_parent('a') and key in title_key(node.get_text(' ',strip=True))]
    if not heads:
        named = [node for node in soup.select('div,p,span,strong') if not _noise(node)
            and not node.find_parent('a') and not node.select('a[href]')
            and title_key(node.get_text(' ',strip=True)) == key]
        heads = [node for node in named if not any(other is not node and node in other.parents for other in named)]
    return heads[0] if len(heads) == 1 else None


def _scope_metadata(scope, heading):
    from backend.scraper.date_elements import NON_PUBLICATION, TIME_RANGE
    candidates = []; after = False; prose = False
    for node in scope.find_all(True):
        if node is heading:
            after = True; continue
        if not after or heading in node.parents or _noise(node):
            continue
        if node.name in ('h1','h2','h3'):
            prose = True; break
        if node.name not in ('span','time','p','div','td'):
            continue
        semantic = node.get('itemprop') == 'datePublished'
        text = (node.get('datetime') or node.get('content') or node.get_text(' ',strip=True)) if semantic else node.get_text(' ',strip=True)
        if node.find_parent('a') or node.select('a[href]') and len(text) < 25:
            continue  # Back/print/share controls are not prose boundaries.
        if node.name in ('p','td') and (NON_PUBLICATION.search(text) or len(text)>180):
            prose = True; break
        if not text or len(text)>220 or NON_PUBLICATION.search(text) or TIME_RANGE.search(text):
            continue
        explicit = bool(PUBLISH.match(text))
        date_only = bool(DATE.fullmatch(text.strip()))
        parent_text = node.parent.get_text(' ',strip=True)
        editorial = date_only and len(parent_text)<220 and len(DATE.findall(parent_text))==1 and EDITOR.search(parent_text)
        if node.name in ('p','td') and not (explicit or editorial or semantic or date_only):
            prose = True; break
        dates = len(DATE.findall(text)) + bool(compact_publication_date(text))
        if dates==1 and (explicit or editorial or semantic):
            candidates.append((node,text))
    return candidates, prose


def publication_nodes(soup, heading):
    for scope in heading.parents:
        if scope.name in ('html','[document]') or _noise(scope):
            break
        candidates, prose = _scope_metadata(scope, heading)
        if candidates or prose:
            return candidates, scope
    return [], None


def _metadata_leaves(metadata):
    return [(node, text) for node, text in metadata if not any(
        other is not node and node in other.parents for other, _ in metadata)]


def without_publication_metadata(region, metadata):
    """Copy a region while removing only its observed local publication lines."""
    clean = deepcopy(region)
    metadata_ids = {id(node) for node, _ in _metadata_leaves(metadata)}
    for original, copied in zip([region, *region.find_all(True)], [clean, *clean.find_all(True)]):
        if id(original) in metadata_ids and copied.attrs is not None:
            copied.decompose()
    return clean


def title_bound_material(soup, url, title):
    """Observe ready body siblings, or a pending body, within one local owner.

    Some templates put the complete title/date header beside their body. Only
    when that header contains no body may its immediate parent supply the next
    body region. Never climb through an article boundary or past that parent.
    """
    from backend.scraper.article_resources import has_public_body_resource
    heading = article_heading(soup,title)
    if heading is None:
        return None, 'missing'
    metadata, scope = publication_nodes(soup,heading)
    if not metadata or scope is None:
        return None, 'missing'
    metadata = _metadata_leaves(metadata)
    regions = []; branch = heading; adjacent_scope = None; missing_state = 'missing'
    for parent in heading.parents:
        adjacent = parent is adjacent_scope
        for sibling in branch.find_next_siblings():
            if _noise(sibling) or sibling.name in ('script','style','a','button'):
                if adjacent and sibling.name not in ('script', 'style'):
                    break
                continue
            if any(sibling is node or node in sibling.parents for node,_ in metadata):
                continue
            if adjacent and sibling.name in ('header','footer','nav','aside','form'):
                break
            state = article_node_state(sibling)
            if state != 'ready':
                missing_state = state
                if adjacent:
                    break
                continue
            if sibling.name in ('h1','h2','h3') or sibling.select('h1,[itemprop="headline"]'):
                break
            if any(_scope_metadata(sibling, next_heading)[0] for next_heading in sibling.select('h2,h3')):
                # A nested headline with its own publication line starts
                # another notice. Its prose cannot fill this missing body.
                break
            if adjacent and any(PUBLISH.match(node.get_text(' ', strip=True)) and
                    (DATE.search(node.get_text(' ', strip=True)) or compact_publication_date(node.get_text(' ', strip=True)))
                    for node in [sibling, *sibling.find_all(['div','p','span','time'])]):
                # A neighboring custom article can have a plain <p> headline.
                # Its own publication field is still an independent identity.
                break
            # A custom template can put its publication line and actual prose
            # inside the same sibling container. Exclude only the observed
            # metadata nodes, not that whole body container.
            prepared, state = article_material(without_publication_metadata(sibling, metadata), url)
            if prepared is None:
                missing_state = state
                if adjacent:
                    break
                continue
            # Filter only descendants of this observed body copy. Article
            # heading/header identity in the original document stays intact;
            # a nested site header/sidebar/logo cannot provide body evidence.
            for noise in list(prepared.find_all(True)):
                if noise.attrs is None:
                    continue
                label = ' '.join(noise.get('class', [])) + ' ' + noise.get('id', '')
                if noise.name in ('script','style','header','nav','aside','footer','form','button') or NOISE.search(label) or BODY_HEADER.search(label):
                    noise.decompose()
            resource = has_public_body_resource([prepared],url)
            clean = deepcopy(prepared)
            for noise in list(clean.select('a')):
                noise.decompose()
            prose_nodes = ([clean] if clean.name in ('p','td','pre','table') else []) + clean.select('p,td,pre,table')
            prose = [node for node in prose_nodes
                     if len(node.get_text(' ',strip=True))>=15 and not PUBLISH.match(node.get_text(' ',strip=True))]
            if prose or resource:
                regions.append(prepared)
                if adjacent and sibling.name not in ('p','td','pre','table'):
                    break
            elif adjacent:
                break
        if parent is scope:
            if (regions or scope.name in ('article','main') or scope.parent is None
                    or scope.parent.name in ('body','html','[document]') or _noise(scope.parent)):
                break
            adjacent_scope = scope.parent
        elif adjacent:
            break
        branch = parent
    if not regions:
        return None, missing_state
    result = BeautifulSoup('<article></article>','lxml').article
    for node in regions:
        result.append(BeautifulSoup(str(node),'lxml').body.contents[0])
    return result, 'ready'


def title_bound_body(soup, url, title):
    """Return observed ready body regions only when their article is identified."""
    return title_bound_material(soup, url, title)[0]
