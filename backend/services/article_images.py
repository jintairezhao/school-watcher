"""Read stored article images through the collection transport, including split DNS."""
import hashlib
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from backend.scraper.http_client import requests, validate_public_url
from backend.scraper.sanitizer import sanitize_html

MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _source(value, base_url):
    if not value or not value.strip():
        return None
    try:
        url = urljoin(base_url, (value or '').strip())
        return validate_public_url(url, resolve=False)
    except ValueError:
        return None


def _key(url):
    return hashlib.sha256(url.encode('utf-8')).hexdigest()


def image_sources(html, base_url):
    soup = BeautifulSoup(sanitize_html(html), 'lxml')
    sources = {}
    for img in soup.find_all('img', src=True):
        url = _source(img['src'], base_url)
        if url:
            sources[_key(url)] = url
    return sources


def render_article_html(html, ann_id=None, base_url=''):
    """Rewrite only the displayed copy; retain original URLs in the archive/export."""
    if ann_id is None or not html:
        return sanitize_html(html)
    soup = BeautifulSoup(html, 'lxml')
    for img in soup.find_all('img', src=True):
        url = _source(img['src'], base_url)
        if url:
            img['src'] = f'/api/announcements/{int(ann_id)}/images/{_key(url)}'
        else:
            del img['src']
    # Keep the security parser as the last serialization boundary.
    return sanitize_html(str(soup))


def _image_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    if data.startswith(b'BM'):
        return 'image/bmp'
    if data[4:8] == b'ftyp' and data[8:12] in (b'avif', b'avis'):
        return 'image/avif'
    raise ValueError('官网未返回可显示的图片')


def fetch_article_image(url, article_url):
    # This client pins public addresses and revalidates every redirect. Never
    # forward the reader's cookies, authorization or upstream response headers.
    response = requests.get(url, headers={'Referer': article_url},
                            stream=True, timeout=(6, 16))
    try:
        response.raise_for_status()
        if int(response.headers.get('Content-Length', '0')) > MAX_IMAGE_BYTES:
            raise ValueError('图片过大')
        data = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError('图片过大')
        return bytes(data), _image_type(data)
    finally:
        response.close()
