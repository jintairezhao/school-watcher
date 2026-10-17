"""Observe publication fields beside a real article title.

This observer supplies metadata only. It deliberately does not change the
stricter title/body observer or any saved article and attachment material.
"""
import re
from bs4.element import Comment

from backend.scraper.article_observation import (
    DATE, NOISE, article_node_state, compact_publication_date, title_key,
)
from backend.scraper.date_elements import NON_PUBLICATION, TIME_RANGE

HEADINGS = ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')
PUBLICATION = re.compile(r'(?:发布日期|发布时间|发表时间|发表日期|发文日期|发布于|\b(?:Published(?:\s+(?:on|at))?|Release|Publication\s+date))\s*[：:]?\s*', re.I)
FIELD = re.compile(r'(?:日期|时间)\s*[：:]\s*')
EDITOR = re.compile(r'责任编辑|编辑\s*[:：]|作者\s*[:：]|来源\s*[:：]|浏览(?:量|次数)?\s*[:：]|点击(?:量|次数)?\s*[:：]|\b(?:Source|Author|Editor|Writer|Hits)\s*:', re.I)
CONTROL = re.compile(r'^(?:分享到\s*[:：]?|打印|关闭|返回|字号|大\s*中\s*小|阅读次数|浏览次数)[\s\d\[\]【】]*$')


def _decorative_title(node):
    quote = node if node.name == 'blockquote' else node.find_parent('blockquote')
    return quote is not None and quote.find_parent('header') is not None


def _excluded(node, *, decorative=False):
    if article_node_state(node) != 'ready':
        return True
    for parent in [node, *node.parents]:
        if parent.attrs is None:
            return True
        if parent.name in ('nav', 'aside', 'footer', 'script', 'style', 'template', 'noscript'):
            return True
        if parent.name == 'blockquote' and not decorative:
            return True
        if parent.name == 'form' and parent.select('input:not([type="hidden"]),textarea,select,button'):
            return True
        if NOISE.search(' '.join(parent.get('class', [])) + ' ' + parent.get('id', '')):
            return True
    return False


def publication_headings(soup, title):
    key = title_key(title)
    if not key:
        return []
    candidates = []
    for node in soup.select('h1,h2,h3,h4,h5,h6,[itemprop="headline"],[role="heading"],div,p,span,strong,td,blockquote'):
        if _excluded(node, decorative=_decorative_title(node)) or node.find_parent('a', href=True):
            continue
        if node.select('a[href]'):
            continue
        text_key = title_key(node.get_text(' ', strip=True))
        semantic = node.name in HEADINGS or node.get('itemprop') == 'headline' or node.get('role') == 'heading'
        if text_key == key or semantic and len(key) >= 8 and key in text_key and len(text_key) <= len(key) * 2:
            candidates.append(node)
    # Nested wrappers are one physical title, not competing article identities.
    return [node for node in candidates if not any(other is not node and node in other.parents for other in candidates)]


def _field_text(node):
    text = re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip()
    semantic = str(node.get('itemprop', '')).casefold() == 'datepublished'
    if semantic:
        text = node.get('datetime') or node.get('content') or text
    if not text or len(text) > 220:
        return None
    # Read an explicitly labelled publication field independently of adjacent
    # update/event fields. Never promote the other date from the same row.
    published = PUBLICATION.search(text)
    if published and (published.start() == 0 or EDITOR.search(text[:published.start()])):
        fields = list(PUBLICATION.finditer(text))
        values = {DATE.match(text[field.end():]).group(0)
                  for field in fields if DATE.match(text[field.end():])}
        if len(values) > 1:
            return None
        rest = text[published.end():]
        match = DATE.match(rest)
        if match:
            return text[:published.end()] + match.group(0)
        compact = compact_publication_date('发布时间：' + rest)
        if compact:
            # Preserve the observed label and digits in the evidence. Parsing
            # a compact date must not invent different text for its DOM node.
            return text[:published.end()] + rest[:8]
    markers = ' '.join(node.get('class', [])) + ' ' + str(node.get('itemprop', ''))
    if NON_PUBLICATION.search(text + ' ' + markers) or TIME_RANGE.search(text):
        return None
    dates = list(DATE.finditer(text))
    if len(dates) != 1:
        return None
    if semantic or DATE.fullmatch(text):
        return text
    if FIELD.match(text) or EDITOR.search(text):
        # Header-only short fields may use 时间/日期; prose after that field
        # is not a publication field, even when it contains a real date.
        prefix = text[:dates[0].start()]
        suffix = text[dates[0].end():].strip()
        if (FIELD.fullmatch(prefix) or EDITOR.search(prefix)) and (
                not suffix or EDITOR.match(suffix) or re.fullmatch(r'\d{1,2}:\d{2}(?::\d{2})?', suffix)):
            return text
    return None


def _header_region(region):
    """Return fields and whether substantive material ends this header."""
    if _excluded(region) or region.name in ('script', 'style', 'link', 'button'):
        return [], False
    text = re.sub(r'\s+', ' ', region.get_text(' ', strip=True)).strip()
    if not text or CONTROL.fullmatch(text):
        return [], False
    field = _field_text(region)
    if field:
        return [(region, field)], False
    markers = ' '.join(region.get('class', [])) + ' ' + str(region.get('itemprop', ''))
    if NON_PUBLICATION.search(markers) or len(text) <= 220 and (
            NON_PUBLICATION.search(text) or TIME_RANGE.search(text)):
        return [], True
    if region.name in HEADINGS or region.get('itemprop') == 'headline':
        return [], True
    # Descend only through containers, preserving the first prose boundary.
    # A paragraph/quote containing a date cannot be searched for convenient
    # subspans after its surrounding sentence was rejected.
    if region.name in ('p', 'blockquote', 'span', 'time', 'td', 'i', 'em') or not region.find(True, recursive=False):
        return [], True
    result = []
    for child in region.children:
        if isinstance(child, Comment):
            continue
        if getattr(child, 'name', None) is None:
            if str(child).strip():
                return result, True
            continue
        fields, ended = _header_region(child)
        result.extend(fields)
        if ended:
            return result, True
    return result, False


def publication_header(heading):
    branch = heading
    for parent in heading.parents:
        if parent.name in ('html', '[document]'):
            break
        result = []
        for sibling in branch.next_siblings:
            if isinstance(sibling, Comment):
                continue
            if getattr(sibling, 'name', None) is None:
                if str(sibling).strip():
                    return result
                continue
            fields, ended = _header_region(sibling)
            result.extend(fields)
            if ended:
                return result
        if result:
            return result
        if parent.name in ('article', 'body'):
            break
        branch = parent
    return []
