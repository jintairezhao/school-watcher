"""Read-only smoke check of a few official homepages; no database writes."""
import json
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.scraper.discovery.lightweight import discover_columns
from backend.scraper.engine import _fetch_html


def check(url):
    try:
        html = _fetch_html(url, allow_browser_fallback=False)
        columns = discover_columns(url, html)
        return {'url': url, 'html_bytes': len(html.encode()), 'columns': [
            {'name': d['name'], 'group': d['group_name'], 'url': d['list_url']} for d in columns]}
    except Exception as exc:
        return {'url': url, 'error': str(exc)}


if __name__ == '__main__':
    urls = sys.argv[1:] or ['https://www.tsinghua.edu.cn', 'https://www.pku.edu.cn', 'https://www.cupk.edu.cn']
    with ThreadPoolExecutor(max_workers=3) as pool:
        print(json.dumps(list(pool.map(check, urls)), ensure_ascii=True, indent=2))
