"""Bound model input by publishing regions while retaining original CSS locations."""
from bs4 import BeautifulSoup
from backend.ai.skill_loader import canonical
from .structure import locator


def materials(school_id, url, html):
    soup = BeautifulSoup(html, 'lxml')
    for node in soup.select('script,style,svg'):
        node.clear()
    for node in soup.find_all(True):
        for attr in list(node.attrs):
            if attr.startswith('on') or attr in ('style', 'srcset') or (attr == 'src' and str(node[attr]).startswith('data:')):
                del node.attrs[attr]

    def evidence(node, root=''):
        return {'school_id': school_id, 'candidates': [{'candidate_id': 'page', 'url': url}],
                'evidence': [{'evidence_id': 'page', 'url': url, 'html': str(node),
                              'original_root_selector': root}]}

    whole = evidence(soup)
    if len(canonical(whole).encode()) <= 24 * 1024:
        return [whole]
    regions = {}
    # Headings can also be tab labels. They must coexist with observed links,
    # and every distinct region gets a resumable turn rather than a first-N cut.
    for heading in soup.select('h1,h2,h3,h4,h5,h6,[role=tab],.title,.tit,.hd'):
        chosen = None
        for parent in heading.parents:
            if parent.name in ('html', 'body', '[document]'):
                break
            if len(parent.select('a[href]')) < 2:
                continue
            item = evidence(parent, locator(parent))
            if len(canonical(item).encode()) > 23 * 1024:
                break
            chosen = item
        if chosen:
            regions[chosen['evidence'][0]['original_root_selector']] = chosen
    return list(regions.values())
