"""Recognize public attachments inside an already identified article body."""
import re
from urllib.parse import urljoin, urlsplit

from backend.scraper.http_client import validate_public_url


def has_public_body_resource(body, page_url):
    """A scoped PDF/image notice is public content even without body text.

    Read literal resource URLs only; never execute embedded viewers or download
    attachments during column discovery. Navigation images cannot supply proof.
    """
    for container in body:
        for node in container.select('a[href], img[src], [pdfsrc], embed[src], object[data]'):
            if node.name == 'img' and (node.get('width') in ('0', '1') or node.get('height') in ('0', '1')):
                continue
            resource = node.get('pdfsrc') or node.get('href') or node.get('src') or node.get('data')
            try:
                address = urljoin(page_url, resource)
                validate_public_url(address, resolve=False)
                if re.search(r'\.(?:pdf|docx?|xlsx?|pptx?|odt|ods|odp|png|jpe?g|webp|gif|svg|avif)$',
                             urlsplit(address).path, re.I):
                    return True
            except (TypeError, ValueError):
                continue
    return False

