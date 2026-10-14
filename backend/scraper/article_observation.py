"""Title-bound article observations, independent of a site's CSS class names.

Never use all-page text as an article body. Walk the observed title's enclosing
regions, stopping at prose, and require local publication metadata for a custom
body fallback. No date is inferred from an address, the clock or article prose.
"""
import re
from bs4 import BeautifulSoup

DATE = re.compile(r'(?<!\d)(\d{4})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})日?(?!\d)')
PUBLISH = re.compile(r'^(?:发布日期|发布时间|发文日期|发布于)\s*[：:]?\s*')
EDITOR = re.compile(r'责任编辑|编辑[:：]|作者[:：]|来源[:：]|浏览(?:量|次数)?[:：]')
NOISE = re.compile(r'(?:^|[\s_-])(?:nav|navigation|menu|sidebar|breadcrumb|related|recommend|toolbar|footer)(?:$|[\s_-])', re.I)


def title_key(value):
    return re.sub(r'\W+', '', value or '').casefold()


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
    return any(parent.name in ('nav','aside','footer','blockquote','script','style')
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
        if len(DATE.findall(text))==1 and (explicit or editorial or semantic):
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


def title_bound_body(soup, url, title):
    """Return observed sibling body regions only when their article is identified."""
    from backend.scraper.article_resources import has_public_body_resource
    heading = article_heading(soup,title)
    if heading is None:
        return None
    metadata, scope = publication_nodes(soup,heading)
    if not metadata or scope is None:
        return None
    regions = []; branch = heading
    for parent in heading.parents:
        for sibling in branch.find_next_siblings():
            if _noise(sibling) or sibling.name in ('script','style','a','button'):
                continue
            if any(sibling is node or sibling in node.parents or node in sibling.parents for node,_ in metadata):
                continue
            if sibling.name in ('h1','h2','h3') or sibling.select('h1,[itemprop="headline"]'):
                break
            clean = BeautifulSoup(str(sibling),'lxml')
            for noise in list(clean.select('script,style,nav,aside,footer,form,a,button')):
                noise.decompose()
            prose = [node for node in clean.select('p,td,pre,table')
                     if len(node.get_text(' ',strip=True))>=15 and not PUBLISH.match(node.get_text(' ',strip=True))]
            resource = has_public_body_resource([sibling],url)
            if prose or resource:
                regions.append(sibling)
        if parent is scope:
            break
        branch = parent
    if not regions:
        return None
    result = BeautifulSoup('<article></article>','lxml').article
    for node in regions:
        result.append(BeautifulSoup(str(node),'lxml').body.contents[0])
    return result
