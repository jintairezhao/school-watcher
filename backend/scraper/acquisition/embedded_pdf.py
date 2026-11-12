"""An observed static article PDF reference, without executing page scripts."""
import hashlib
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from backend.scraper.http_client import validate_public_url

VERSION = 'embedded-pdf-reference-1'
_ATOM = r'''(?:"[^"\\\r\n]*"|'[^'\\\r\n]*'|\d+|[A-Za-z_$][\w$]*)'''
_LOADER = re.compile(
    r'''\s*(?:(?:var|let|const)\s+[A-Za-z_$][\w$]*\s*=\s*\[\s*\]\s*;\s*)?'''
    r'''showVsbpdfIframe\s*\(\s*(?P<quote>["'])(?P<url>[^"'\\\r\n]+)(?P=quote)\s*,\s*'''
    + _ATOM + r'(?:\s*,\s*' + _ATOM + r'){4}\s*\)\s*;?\s*', re.S)


def reference(html, page_url):
    """Require one literal loader or PDF-player attribute in an article body."""
    from backend.scraper.discovery.structure import locator
    soup, found = BeautifulSoup(html, 'lxml'), []
    for script in soup.select('.v_news_content script:not([src])'):
        if any(node.name in ('nav', 'aside', 'header', 'footer', 'template', 'noscript')
                or node.has_attr('hidden') or re.search(r'display\s*:\s*none', node.get('style', ''), re.I)
                for node in script.parents if getattr(node, 'attrs', None) is not None):
            continue
        match = _LOADER.fullmatch(script.get_text())
        if match is None:
            continue
        address = urljoin(page_url, match['url'])
        try:
            validate_public_url(address, resolve=False)
            parent, target = urlsplit(page_url), urlsplit(address)
            if (target.scheme not in ('http', 'https') or target.username or target.password
                    or (parent.hostname, parent.port) != (target.hostname, target.port)):
                continue
        except (ValueError, TypeError):
            continue
        found.append(dict(version=VERSION, page_url=page_url,
            content_hash=hashlib.sha256(html.encode()).hexdigest(), locator=locator(script),
            literal_url=match['url'], requested_url=address))
    for node in soup.select('.wp_articlecontent [pdfsrc],.v_news_content [pdfsrc],article [pdfsrc]'):
        if any(parent.name in ('nav','aside','header','footer','template','noscript')
                or parent.has_attr('hidden') or parent.get('aria-hidden')=='true'
                or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden',parent.get('style',''),re.I)
                for parent in [node,*node.parents] if getattr(parent,'attrs',None) is not None):
            continue
        literal=node.get('pdfsrc','').strip()
        address=urljoin(page_url,literal)
        try:
            validate_public_url(address,resolve=False)
            parent,target=urlsplit(page_url),urlsplit(address)
            if (not literal or target.scheme not in ('http','https') or target.username or target.password
                    or (parent.hostname,parent.port)!=(target.hostname,target.port)
                    or not target.path.lower().endswith('.pdf')):
                continue
        except (ValueError,TypeError):
            continue
        # Keep old script-loader bindings byte-for-byte compatible. This
        # distinct kind binds the newly supported DOM attribute explicitly.
        found.append(dict(version=VERSION,page_url=page_url,
            content_hash=hashlib.sha256(html.encode()).hexdigest(),locator=locator(node),
            literal_url=literal,requested_url=address,reference_kind='pdfsrc_attribute'))
    return found[0] if len(found) == 1 else None


def valid_binding(attachment, html, page_url):
    observed = reference(html, page_url) if html else None
    binding = attachment.get('embedding')
    if not observed or not isinstance(binding, dict) or any(binding.get(k) != v for k, v in observed.items()):
        return False
    try:
        resource = attachment.get('resource_url', '')
        validate_public_url(resource, resolve=False)
        page, target = urlsplit(page_url), urlsplit(resource)
        return ((page.hostname, page.port) == (target.hostname, target.port)
                and not target.username and not target.password)
    except (TypeError, ValueError):
        return False
