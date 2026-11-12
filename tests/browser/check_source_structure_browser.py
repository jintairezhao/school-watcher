"""A real locale switch preserves directory observations and evidence identity."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_source_structure_view import SourceStructureViewTests
from backend.database.db import db
from backend.database.models import Department, DepartmentDirectoryEntry
from backend.services.localization import stage_preferences
from playwright.sync_api import expect, sync_playwright


def check():
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        try:
            for width in (1440, 390):
                fixture = SourceStructureViewTests(); fixture.setUp()
                page = None
                try:
                    fixture.records[1].update(name='官网记录的学院名称', url=fixture.url, material_available=False)
                    fixture.publish()
                    parent = Department(school_id=fixture.school.id, name='官网上级名称', kind='unit', structure_key='parent')
                    child = Department(school_id=fixture.school.id, name='官网记录的学院名称', kind='unit', structure_key='child')
                    db.session.add_all([parent, child]); db.session.flush()
                    db.session.add(DepartmentDirectoryEntry(parent_id=parent.id, department_id=child.id, facts_json=[dict(
                        type='organization_membership', observed_relation='attached_unit',
                        assertion=dict(kind='explicit_table_header', predicate='attached_unit', direction='parent_to_child',
                            text='挂靠单位', locator='thead th', parent_locator='tbody .parent', child_locator='tbody .child'),
                        evidence=[dict(fixture.records[1], locator='thead th'), dict(fixture.records[1], locator='tbody .child')])]))
                    stage_preferences(fixture.fixture.fixture.admin.id, {'locale': 'en'}); db.session.commit()
                    page = browser.new_page(viewport={'width': width, 'height': 960}, reduced_motion='reduce')
                    errors, writes, navigations = [], [], []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('framenavigated', lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)
                    def serve(route):
                        request = route.request; parsed = urlsplit(request.url)
                        assert parsed.hostname == 'localhost', request.url
                        if request.method != 'GET': writes.append(parsed.path)
                        response = fixture.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                            method=request.method, data=request.post_data,
                            headers={key: value for key, value in request.headers.items()
                                     if key.lower() in ('content-type', 'x-csrf-token')})
                        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
                    page.route('**/*', serve)
                    page.goto('http://localhost' + fixture.endpoint)
                    metadata = page.locator('.structure-page [data-i18n="目录列示，隶属关系待核实"]')
                    expect(metadata).to_have_text('Directory listing; affiliation unverified')
                    expect(page.locator('.structure-page [data-i18n="有近期发布"]')).to_have_text('Recent publications found')
                    expect(page.locator('#provenStructureRelations > ul > li > p')).to_contain_text('挂靠单位')
                    marker = page.locator('.structure-page').get_attribute('data-structure-revision')
                    before = len(navigations)
                    page.locator('#languageSelect').select_option('zh-CN')
                    expect(metadata).to_have_text('目录列示，隶属关系待核实')
                    page.locator('#languageSelect').select_option('en')
                    expect(metadata).to_have_text('Directory listing; affiliation unverified')
                    expect(page.locator('.structure-page [data-i18n="有近期发布"]')).to_have_text('Recent publications found')
                    expect(page.locator('#provenStructureRelations > ul > li > p')).to_contain_text('挂靠单位')
                    expect(page.locator('.structure-leaf > span').first).to_have_text('官网记录的学院名称')
                    assert page.locator('.structure-page').get_attribute('data-structure-revision') == marker
                    assert len(navigations) == before, 'Language switch reloaded the page'
                    assert page.locator('.structure-page [data-i18n="原始材料未保留"]').count() > 0
                    assert not errors, errors
                    assert writes == ['/api/preferences/localization'] * 2, writes
                    results.append(dict(width=width, live_locale_switch=True, original_text_unchanged=True,
                        structure_revision_unchanged=True, reloads=0, page_errors=errors, writes=writes))
                finally:
                    if page: page.close()
                    fixture.tearDown()
        finally:
            browser.close()
    print(json.dumps(dict(status='passed', checks=results, official_requests=0, model_calls=0), ensure_ascii=False))


if __name__ == '__main__':
    check()
