"""Literal source signatures from saved official article metadata.

A signature is not a verdict about a unit's current name, hierarchy, or ownership.
Only explicitly supported metadata blocks are read; body mentions and authors
cannot substitute for the source field.
"""
import hashlib
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


FIELDS = re.compile(r'(?<!\S)(发布日期|发布时间|作者|文章来源|稿件来源|来源|浏览次数|浏览量|浏览|点击次数|点击|责任编辑|编辑|审核)\s*[：:]\s*')
SOURCE_FIELDS = {'来源', '文章来源', '稿件来源'}


def metadata_blocks(soup, host):
    if host in ('www.cupk.edu.cn', 'cupk.edu.cn'):
        for container in soup.select('.c-right-detail, .c-main__right'):
            if container.find_parent('blockquote') or container.find_parent(class_='c-right-detail__content'):
                continue
            if 'c-right-detail' in container.get('class', []):
                if container.select(':scope > .c-right-detail__header') and container.select(':scope > .c-right-detail__content'):
                    yield from container.select(':scope > .SecondList__msg, :scope > .c-right-detail__sub-title > .c-main__time')
            elif container.select(':scope > .c-right-detail > .c-right-detail__header'):
                # Older units place metadata beside the title, or in a separate
                # content wrapper above the body. Require that observed title shell.
                yield from container.select(':scope > .SecondList__msg, :scope > .c-main__time.SecondList__msg, '
                                            ':scope > .c-right-detail__content > .c-main__time.SecondList__msg')
    if host in ('www.cup.edu.cn', 'cup.edu.cn'):
        for container in soup.select('.subPageArticle, .subArticleTitle'):
            if container.find_parent('blockquote') or container.find_parent(class_='article'):
                continue
            if 'subPageArticle' in container.get('class', []):
                if container.select(':scope > .articleTitle') and container.select(':scope > .article'):
                    yield from container.select(':scope > .articleAuthor > p > span')
            elif container.find(['h1', 'h2'], recursive=False):
                yield from container.select(':scope > span')


def article_provenance(html, reference_url):
    result = {'state': 'unrecognized', 'labels': [], 'evidence': []}
    if not html:
        return dict(result, state='no_saved_html')
    try:
        host = urlsplit(reference_url or '').hostname
    except ValueError:
        return result
    if host not in ('www.cupk.edu.cn', 'cupk.edu.cn', 'www.cup.edu.cn', 'cup.edu.cn'):
        return result
    soup = BeautifulSoup(html, 'lxml')
    from backend.scraper.discovery.structure import locator
    seen = set()
    for block in metadata_blocks(soup, host):
        if id(block) in seen:
            continue
        seen.add(id(block))
        result['state'] = 'field_unavailable'
        text = block.get_text(' ', strip=True)
        fields = list(FIELDS.finditer(text))
        for index, field in enumerate(fields):
            if field.group(1) not in SOURCE_FIELDS:
                continue
            end = fields[index + 1].start() if index + 1 < len(fields) else len(text)
            label = text[field.end():end].strip()
            if not label or len(label) > 200 or label in ('-', '--', '—', '|', '｜'):
                continue
            if label not in result['labels']:
                result['labels'].append(label)
            result['evidence'].append({'label': label, 'field': field.group(1),
                'reference_url': reference_url, 'locator': locator(block),
                'metadata_text': text, 'content_hash': hashlib.sha256(html.encode()).hexdigest(),
                'method': 'explicit_article_source_field'})
    if result['labels']:
        result['state'] = 'observed' if len(result['labels']) == 1 else 'multiple_signatures'
    return result
