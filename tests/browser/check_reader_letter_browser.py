"""Exercise the real letter component and verify its 3D occlusion in Chromium."""
import json
import os
from pathlib import Path

from jinja2 import Environment
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / '.local' / 'letter-layer-check'


def paper_occlusion(page):
    # Hit testing observes the composited surfaces, unlike comparing z-index.
    return page.locator('.letter-paper').evaluate('''paper => {
        const results = [];
        for (const left of ['20px', '50%', 'calc(100% - 20px)']) {
            const probe = document.createElement('span');
            probe.style.cssText = `position:absolute;left:${left};top:5px;width:2px;height:2px;pointer-events:none`;
            paper.append(probe);
            const r = probe.getBoundingClientRect();
            const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            results.push({left, visible: Boolean(top?.closest('.letter-paper')), occluder: top?.className});
            probe.remove();
        }
        return results;
    }''')


def check():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    markup = Environment().from_string(
        (ROOT / 'frontend/templates/_reader_empty.html').read_text(encoding='utf-8')
    ).render(announcements=[])
    css = '\n'.join((ROOT / path).read_text(encoding='utf-8') for path in (
        'frontend/static/css/style.css', 'frontend/static/css/reader-empty.css'))
    script = (ROOT / 'frontend/static/js/reader-empty.js').read_text(encoding='utf-8')
    checks = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        try:
            for theme, reduced in [('dark', False), ('light', False), ('dark', True)]:
                context = browser.new_context(viewport={'width': 760, 'height': 650},
                    device_scale_factor=2, reduced_motion='reduce' if reduced else 'no-preference',
                    has_touch=reduced)
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.set_content('<style>' + css + '''
                    body { margin: 0; }
                    .reader-placeholder { height: 600px; }
                    .letter-scene, .letter-scene * { pointer-events: auto; }
                </style><div class="inbox-workspace">''' + markup + '</div>')
                if theme == 'dark':
                    page.add_style_tag(content='''body { background:#1c1c1c; --color-surface:#1c1c1c;
                        --color-text:#eee; --color-primary:#80b5f0; --color-text-secondary:#adb2ba; }''')
                page.add_script_tag(content=script)
                letter = page.locator('[data-reader-letter]')
                if not reduced:
                    letter.hover()
                    page.wait_for_timeout(650)
                    visible = paper_occlusion(page)
                    assert all(row['visible'] for row in visible), visible
                    letter.screenshot(path=str(OUTPUT / f'{theme}-hover.png'))
                    checks.append(f'{theme}: hovering exposes the entire paper edge in front of the flap')

                letter.focus()
                letter.press('Enter')
                expect(letter).to_have_attribute('aria-pressed', 'true')
                page.mouse.move(5, 5)
                page.wait_for_timeout(650 if not reduced else 0)
                visible = paper_occlusion(page)
                assert all(row['visible'] for row in visible), visible
                expect(letter).to_have_accessible_name('收起信纸')
                letter.screenshot(path=str(OUTPUT / f'{theme}-open{"-reduced" if reduced else ""}.png'))

                # Reverse repeatedly before a transition completes.
                for _ in range(5):
                    letter.press('Space')
                    page.wait_for_timeout(35)
                expect(letter).to_have_attribute('aria-pressed', 'false')
                page.wait_for_timeout(650 if not reduced else 0)
                assert not any(row['visible'] for row in paper_occlusion(page))
                assert letter.evaluate('(el) => el.getAnimations({subtree:true}).length') == 0
                if reduced:
                    letter.tap()
                    expect(letter).to_have_attribute('aria-pressed', 'true')
                    assert letter.evaluate('(el) => el.getAnimations({subtree:true}).length') == 0
                assert not errors, errors
                checks.append(f'{theme}, reduced={reduced}: open, close, keyboard, rapid reversal, idle')
                context.close()
        finally:
            browser.close()
    print(json.dumps({'passed': checks, 'screenshots': str(OUTPUT)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    check()
