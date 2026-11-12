"""Actual local inspections -> publication -> API -> replay, without live crawling."""
from pathlib import Path
from urllib.parse import urlsplit
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_exploration_events import ROOT_URL, HOME, DEPARTMENTS
from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import inspect_page
from test_structure_snapshot import StructureSnapshotTests
from backend.database.db import db
from playwright.sync_api import expect, sync_playwright


def check(output=None):
    output = Path(output) if output else ROOT / '.local/structure-facts-20261021/events-browser'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        inventory = Inventory(Path(folder) / 'inventory.db')
        key = inventory.ensure_site('程序交互用例', ROOT_URL)
        for url, html in ((ROOT_URL, HOME), (ROOT_URL+'departments/', DEPARTMENTS)):
            inspect_page(inventory, inventory.report(key)['site'], inventory.get_page(key, url),
                fetcher=lambda address, html=html: dict(html=html, url=address, status=200), ai_assist=False)
        original_events = inventory.report(key)['exploration_events']
        fixture = StructureSnapshotTests(); fixture.setUp()
        results = []
        try:
            fixture.school.url = ROOT_URL; db.session.commit()
            fixture.source('本地交互栏目', url=ROOT_URL+'departments/')
            fixture.fixture.catalog.publish(inventory, key)
            baseline = fixture.get()
            child = next(node for node in baseline['nodes'] if node['entity_type'] == 'page' and node['url'].endswith('/departments/'))
            assert len(baseline['exploration_events']) == len(original_events)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel='msedge', headless=True,
                    args=['--enable-webgl', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
                try:
                    for width in (1440, 390):
                        page = browser.new_page(viewport={'width':width,'height':1000}, reduced_motion='reduce')
                        errors, writes = [], []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        def serve(route):
                            req = route.request; url = urlsplit(req.url)
                            assert url.hostname == 'localhost', req.url
                            if req.method != 'GET': writes.append(dict(method=req.method, url=url.path))
                            if url.path.startswith('/_desktop/'):
                                return route.fulfill(json={'phase':'ready'})
                            response = fixture.client.open(url.path+('?' + url.query if url.query else ''), method=req.method,
                                data=req.post_data, headers={k:v for k,v in req.headers.items() if k.lower() in ('content-type','x-csrf-token')})
                            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
                        page.route('**/*', serve)
                        page.goto('http://localhost'+fixture.fixture.page)
                        page.locator('#languageSelect').select_option('en')
                        expect(page.locator('#atlasStage')).to_have_attribute('data-renderer','ready')
                        page.locator('#monitorAuto').uncheck()
                        page.wait_for_function('()=>discoveryMonitor.monitorSnapshot?.exploration_events?.length===5')
                        page.evaluate('id=>discoveryMonitor.discoveryAtlas.select(id)', child['id'])
                        expect(page.locator('#atlasPlay')).to_be_enabled()
                        before = page.evaluate('''()=>({selected:discoveryMonitor.discoveryAtlas.current.id,
                            states:discoveryMonitor.monitorSnapshot.nodes.map(n=>[n.id,n.state]),
                            sources:discoveryMonitor.monitorSnapshot.source_count})''')
                        writes_before_play = len(writes)
                        page.locator('#atlasPlay').click()
                        expect(page.locator('#monitorPathMode')).to_have_value('exploration')
                        expect(page.locator('#atlasPlay')).to_have_attribute('aria-pressed','true')
                        expect(page.locator('#atlasPathEvidence')).to_contain_text('First discovered and saved the entrance')
                        assert 'page_inspection_' not in page.locator('#atlasPathEvidence').inner_text()
                        expect(page.locator('#discoveryMonitor')).to_have_attribute('data-atlas-progress','1.0000',timeout=10000)
                        after = page.evaluate('''()=>({selected:discoveryMonitor.discoveryAtlas.current.id,
                            states:discoveryMonitor.monitorSnapshot.nodes.map(n=>[n.id,n.state]),
                            sources:discoveryMonitor.monitorSnapshot.source_count})''')
                        assert before == after, (before, after)
                        assert writes[writes_before_play:] == [], 'Replay must issue no write request'
                        expect(page.locator('#atlasPlay')).to_have_attribute('aria-pressed','false')
                        page.locator('#languageSelect').select_option('zh-CN')
                        expect(page.locator('#atlasPathEvidence')).to_contain_text('首次发现并保存入口')
                        page.screenshot(path=str(output/f'events-{width}.png'), full_page=True)
                        page.locator('#atlasPlay').click()
                        expect(page.locator('#atlasPlay')).to_have_attribute('aria-pressed','true')
                        # A new snapshot with a missing ancestor cancels playback;
                        # the UI must not retain a now-unprovable old sequence.
                        page.evaluate('''()=>{
                            const root=document.getElementById('discoveryMonitor');
                            const next={...root.monitorSnapshot,structure_token:root.monitorSnapshot.structure_token+'-gap',
                                exploration_events:root.monitorSnapshot.exploration_events.filter(e=>e.event_type!=='exploration_started')};
                            root.monitorSnapshot=next;
                            root.dispatchEvent(new CustomEvent('monitor-data',{detail:next}));
                        }''')
                        expect(page.locator('#atlasPlay')).to_have_attribute('aria-pressed','false')
                        expect(page.locator('#atlasPlay')).to_be_disabled()
                        expect(page.locator('#atlasPathEvidence')).to_contain_text('尚无完整的真实探索记录')
                        assert page.evaluate('()=>discoveryMonitor.discoveryAtlas.current.id') == child['id']
                        # A source configuration has no activation event in this
                        # journal; its page trace cannot become a source event.
                        source = next(node for node in baseline['nodes'] if node['entity_type'] == 'source')
                        page.evaluate('id=>discoveryMonitor.discoveryAtlas.select(id)', source['id'])
                        expect(page.locator('#atlasPlay')).to_be_disabled()
                        assert not errors, errors
                        forbidden_writes = [request for request in writes if request['url'] != '/api/preferences/localization']
                        assert not forbidden_writes, forbidden_writes
                        assert page.evaluate('()=>document.documentElement.scrollWidth-document.documentElement.clientWidth') <= 1
                        results.append(dict(width=width, locales=['en','zh-CN'], events=len(original_events),
                            complete_page_replay=True, selection_unchanged=True, states_unchanged=True,
                            missing_ancestor_update_cancels_replay=True,
                            source_without_activation_not_replayed=True, errors=errors, replay_write_requests=0,
                            localization_preference_writes=writes, forbidden_writes=forbidden_writes,
                            live_website_requests=0, model_calls=0,
                            evidence_scope='Local behavior fixture; not a real website acceptance or threshold sample'))
                        page.close()
                finally:
                    browser.close()
        finally:
            fixture.tearDown()
        (output/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2), 'utf8')
        print(json.dumps(results,ensure_ascii=False))


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--output')
    check(parser.parse_args().output)
