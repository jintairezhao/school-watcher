"""Replay multi-image reading and host waits with local synthetic image bytes."""
import base64
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright, expect
from backend.services.article_images import render_article_html


def check(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    original = '<p>多图文章</p>' + ''.join(
        f'<p><img src="https://example.edu.cn/{i}.png" alt="图片{i}"></p>' for i in range(61))
    rendered = render_article_html(original, 17, 'https://example.edu.cn/news.htm')
    payload = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jBWQAAAAASUVORK5CYII=')
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel='msedge')
        for width in (1280, 390):
            context = browser.new_context(viewport={'width': width, 'height': 800})
            attempts, errors = {}, []
            def serve(route):
                path = route.request.url.split('reader.test', 1)[-1]
                if '/images/' in path:
                    attempts[path] = attempts.get(path, 0)+1
                    if sum(attempts.values()) == 1:
                        route.fulfill(status=503, content_type='application/json',
                            headers={'Retry-After': '1'}, body=json.dumps({'error_code': 'origin_busy'}))
                    elif len(attempts) == 2 and attempts[path] == 1:
                        route.fulfill(status=502, content_type='application/json', body='{}')
                    else:
                        route.fulfill(status=200, content_type='image/png', body=payload)
                elif path.endswith('/content'):
                    route.fulfill(status=200, content_type='application/json', body=json.dumps({'published_at': None}))
                elif path == '/content.js':
                    route.fulfill(status=200, content_type='text/javascript', body=(ROOT/'frontend/static/js/content.js').read_text('utf-8'))
                elif path == '/style.css':
                    route.fulfill(status=200, content_type='text/css', body=(ROOT/'frontend/static/css/style.css').read_text('utf-8'))
                elif path == '/':
                    route.fulfill(content_type='text/html', body=(
                        '<html><head><link rel="stylesheet" href="/style.css"></head><body>'
                        '<div data-content-id="17" data-cached="true"><div class="body-loader-content">'+rendered+
                        '</div></div><script>window.I18n={setText:(el,text)=>el.textContent=text};window.uiT=x=>x;'
                        'window.__peak=0;window.__active=0;const originalFetch=window.fetch;window.fetch=async(...args)=>{'
                        'window.__active++;window.__peak=Math.max(window.__peak,window.__active);try{return await originalFetch(...args);}'
                        'finally{window.__active--;}};</script><script src="/content.js"></script></body></html>'))
                else:
                    raise AssertionError('Unexpected external or local request: '+path)
            context.route('**/*', serve)
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto('http://reader.test/')
            images = page.locator('img[data-article-image-src]')
            expect(images).to_have_count(61)
            # Scroll through the whole article as a reader would; offscreen
            # pictures must remain deferred until they approach the viewport.
            deferred = page.locator('img[data-article-image-src]:not([src])').count()
            assert deferred > 0
            for index in range(61):
                images.nth(index).scroll_into_view_if_needed()
            page.wait_for_function('document.querySelectorAll(".article-image-retry").length === 1')
            page.locator('.article-image-retry').click()
            # Layout contracts as real pixels replace placeholders. Continue
            # the bounded reading pass to visit every retained image.
            for index in range(61):
                images.nth(index).scroll_into_view_if_needed()
            page.wait_for_function('Array.from(document.images).filter(i=>i.naturalWidth>0).length === 61', timeout=15000)
            assert page.evaluate('window.__peak') <= 2  # one image plus the content-date read
            assert sum(attempts.values()) == 63  # 61 successful images + one wait + one genuine failure
            assert not errors, errors
            page.screenshot(path=str(output/f'images-{width}.png'), full_page=False)
            results.append({'width': width, 'images_loaded': 61, 'host_wait_recovered': True,
                'manual_retry_recovered': True, 'offscreen_deferred': True, 'image_requests': 63,
                'script_errors': errors, 'external_requests': 0})
            context.close()
        browser.close()
    (output/'browser-result.json').write_text(json.dumps({'status': 'passed', 'checks': results}, indent=2)+'\n', 'utf-8')
    print(json.dumps(results))


if __name__ == '__main__':
    check(sys.argv[1])
