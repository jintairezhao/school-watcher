"""Reparse trusted captured material through the existing directory publisher."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import current_app

from backend.database.db import db
from backend.services.source_inventory import Inventory, canonical_url, site_key


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (AttributeError, TypeError, ValueError):
        raise ValueError('结构材料缺少有效的原始观察时间') from None


def _observed_at(reference, page):
    value = reference.get('observed_at')
    if value is None:
        # Legacy independent reads were stamped immediately on acquisition.
        # Retained DOM instead keeps its recorded page time when repackaged.
        value = (page.get('checked_at') if reference.get('role') != 'independent_list'
                 and page.get('content_hash') == reference.get('hash') else None) or reference.get('captured_at')
    _time(value)
    return value


def latest_list_reference(bundle):
    """Select only a retained, later verification of this exact list address."""
    first = bundle['list']
    independent = bundle.get('independent') or {}
    if (canonical_url(independent.get('url') or '') == canonical_url(first['url'])
            and independent.get('outcome') in (None, 'usable', 'needs_adapter')):
        page = next((p for p in bundle.get('identity_pages', []) if p['url'] == first['url']), {})
        if _time(_observed_at(independent, page)) > _time(_observed_at(first, page)):
            return independent
    return first


def proposal_publication(bundle, validation):
    """Use an executed region, keeping legacy activated receipts unchanged."""
    if 'publication' not in validation:
        return dict(list_snapshot=bundle['list'], region=bundle.get('region') or {})
    publication = validation['publication']
    if (not isinstance(publication, dict)
            or publication.get('list_snapshot') != latest_list_reference(bundle)
            or not isinstance(publication.get('region'), dict)):
        raise ValueError('已验证的栏目材料与发布凭据不一致')
    return publication


class _MaterialInventory(Inventory):
    def __init__(self, path, prior_report):
        super().__init__(path)
        self.prior_report = prior_report or {}

    def report(self, key):
        report = super().report(key)
        if report:
            # Reinterpreting retained DOM is not another website exploration.
            for field in ('exploration_events', 'exploration_history'):
                if field in self.prior_report:
                    report[field] = self.prior_report[field]
        return report


def capture_retained_structure(inventory, key, list_reference, *, report=None, relationships=None):
    """Freeze existing discovery evidence for a queued source; never acquire pages."""
    from backend.services.source_governance import _hash, _identity_observations, _snapshot, read_snapshot
    from backend.services.source_relationships import SourceRelationships
    report = report if report is not None else inventory.report(key)
    if not report or not list_reference:
        return None
    relationships = relationships if relationships is not None else SourceRelationships(report, inventory.structure(key))
    paths = relationships.paths_for(list_reference['url'])
    bundle = dict(schema=1, **_identity_observations(report, paths), identity_snapshots=[], material_gaps=[])
    for page in bundle['identity_pages']:
        url, digest = page['url'], page.get('content_hash')
        if page.get('state') != 'fetched' or not digest:
            continue
        html = inventory.snapshot(key, url)
        if not html:
            try:
                html = read_snapshot({'hash': digest})
            except ValueError:
                pass
        try:
            _time(page.get('checked_at'))
        except ValueError:
            bundle['material_gaps'].append(dict(url=url, hash=digest, reason='observation_time_missing'))
            continue
        if not html or _hash(html) != digest:
            bundle['material_gaps'].append(dict(url=url, hash=digest, reason='raw_material_unavailable'))
            continue
        reference = _snapshot(html, url, role='identity')
        reference['captured_at'] = page['checked_at']
        bundle['identity_snapshots'].append(reference)
    bundle['material_hash'] = _hash(bundle)
    return bundle


def native_structure_bundle(validation, config):
    from backend.services.source_governance import _hash
    material = validation.get('structure_material')
    if not material:
        return None
    body = {key: value for key, value in material.items() if key != 'material_hash'}
    reference = validation.get('list_snapshot') or {}
    if (body.get('schema') != 1 or _hash(body) != material.get('material_hash')
            or canonical_url(reference.get('url') or '') != canonical_url(config['list_url'])):
        raise ValueError('自动接入结构材料与已验证栏目不一致')
    # Use the page actually validated now, even after a queued read was retried.
    return dict(body, list=reference)


def _materials(school, bundle, config, published):
    from backend.services.source_governance import read_snapshot
    root, key = canonical_url(school.url), site_key(school.url)
    observed = bundle.get('identity_site') or {}
    pages = {p['url']: p for p in bundle.get('identity_pages', [])}
    if pages and (observed.get('site_key') != key or canonical_url(observed.get('root_url') or '') != root):
        raise ValueError('结构材料与学校身份不一致')
    references = {r['url']: r for r in bundle.get('identity_snapshots', [])}
    publisher = bundle.get('publisher_material') or {}
    proof = bundle.get('publisher_evidence') or {}
    roles = {}
    if proof:
        from backend.services.source_workflow import publisher_proven
        if publisher_proven(bundle, config):
            for field, kind in (('directory_evidence_id', 'directory'), ('homepage_evidence_id', 'unit')):
                reference = publisher.get(proof.get(field))
                if reference:
                    url = reference['url']
                    references[url] = reference
                    observed = pages.get(url) or published.get_page(key, url) or {}
                    # A shared directory keeps its own observed identity. The
                    # publisher name identifies only the unit homepage.
                    label = (proof.get('name', '') if kind == 'unit' else
                             observed.get('label') or observed.get('title') or '')
                    roles[url] = (kind, label)
    for reference in publisher.values():
        if canonical_url(reference.get('url') or '') == root:
            references[root] = reference
    if not references:
        return []
    first = bundle['list']
    latest = latest_list_reference(bundle)
    # Validation retains both acquisitions. Publish the latest verified DOM,
    # rather than replacing it with the retained first read after validation.
    references[first['url']] = latest
    result = []
    for url, reference in references.items():
        if reference.get('outcome') not in (None, 'usable', 'needs_adapter'):
            continue
        page = pages.get(url, {})
        kind, label = roles.get(url, (page.get('kind'), page.get('label') or page.get('title') or ''))
        if url == root:
            kind, label = 'root', school.name
        elif url == bundle['list']['url']:
            kind, label = 'channel', config['name']
        elif kind in ('navigation', 'group'):
            # Older captures used navigation/display-group labels for real
            # publisher entrance pages. Reparse their original DOM as a gateway;
            # the canonical writer still has to prove every ownership relation.
            kind = 'gateway'
        if kind not in ('root', 'directory', 'unit', 'channel', 'gateway', 'major'):
            continue
        # Keep an observed request-to-response mapping. Similar URL spellings
        # alone cannot introduce an alias or rewrite the configured entrance.
        requested = canonical_url(reference.get('requested_url') or '')
        material_url = (requested if url == first['url'] and requested == canonical_url(config['list_url']) else url)
        prior = published.get_page(key, material_url) or {}
        from backend.scraper.discovery.inventory_crawler import PARSER_REVISION
        observed_at = _observed_at(reference, page)
        stamp = _time(observed_at)
        if (prior.get('content_hash') == reference.get('hash') and published.snapshot(key, url)
                and prior.get('kind') == kind and prior.get('label') == label
                and prior.get('parser_revision') == PARSER_REVISION
                and prior.get('checked_at') and _time(prior['checked_at']) >= stamp):
            continue
        if (prior.get('content_hash') == reference.get('hash') and prior.get('checked_at')
                and _time(prior['checked_at']) > stamp):
            observed_at = prior['checked_at']
        if prior.get('checked_at') and prior.get('content_hash') != reference.get('hash') and _time(prior['checked_at']) >= stamp:
            raise ValueError('目录已有更新的材料，请重新核实此建议')
        if (reference.get('status') is None and prior.get('state') == 'fetched'
                and type(prior.get('status_code')) is int and 100 <= prior['status_code'] <= 599
                and prior.get('content_hash') == reference.get('hash') and prior.get('checked_at')
                and _time(prior['checked_at']) == _time(observed_at)):
            # Reinterpreting the same observed bytes must not erase known HTTP
            # evidence. Identical bytes from a later read are a new observation.
            reference = dict(reference, status=prior['status_code'], status_evidence=dict(
                kind='published_same_observation', url=material_url,
                content_hash=prior['content_hash'], observed_at=prior['checked_at']))
        result.append(dict(url=material_url, kind=kind, label=label, reference=reference,
            observed_at=observed_at, html=read_snapshot(reference),
            final_url=url if material_url != url else
                (page.get('final_url') or url) if page.get('content_hash') == reference.get('hash') else url))
    return sorted(result, key=lambda p: (('root', 'directory', 'unit', 'channel', 'gateway', 'major').index(p['kind']), p['url']))


@contextmanager
def proposal_structure_writer(school, bundle, config, published):
    """Caller holds the publication lock; no acquisition or model work is allowed."""
    from backend.scraper.discovery.inventory_crawler import _inspect_page
    from backend.services.runtime_catalog import RuntimeCatalog
    from backend.services import tasks
    materials = _materials(school, bundle, config, published)
    if not materials:
        yield published
        tasks.assert_owned()
        db.session.commit()
        return
    catalog = RuntimeCatalog(current_app.config['SOURCE_CATALOG_PATH'])
    root = Path(catalog.path).resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    key = site_key(school.url)
    with TemporaryDirectory(prefix='source-material-', dir=root.parent) as scratch:
        inventory = _MaterialInventory(Path(scratch) / 'inventory.sqlite3', published.report(key))
        inventory.ensure_site(school.name, school.url)
        site = inventory.report(key)['site']
        for material in materials:
            url = material['url']
            inventory.enqueue(key, url, material['label'], material['kind'], 1, [], 'captured_material')
            # Role selects parser context only; all relationships are extracted
            # again from hash-checked DOM, never copied from candidate paths.
            _inspect_page(inventory, site, inventory.get_page(key, url), ai_assist=False,
                fetcher=lambda address, item=material: dict(url=item['final_url'], status=item['reference'].get('status'),
                    html=item['html'], checked_at=item['observed_at'], transport=item['reference'].get('transport')))
            page = inventory.get_page(key, url)
            if page['state'] != 'fetched' or page['content_hash'] != material['reference']['hash']:
                raise ValueError('结构材料未能通过现有目录解析器')
            notes = json.loads(page['notes_json'] or '[]')
            notes = [note for note in notes if not note.startswith('full_snapshot:')]
            notes.append('full_snapshot:' + json.dumps(dict(material['reference'], captured_at=material['observed_at']), ensure_ascii=False))
            with inventory.connect() as writer:
                writer.execute('UPDATE pages SET notes_json=? WHERE site_key=? AND url=?', (json.dumps(notes, ensure_ascii=False), key, url))
                writer.execute('UPDATE edges SET observed_at=? WHERE site_key=? AND parent_url=?', (material['observed_at'], key, url))
                writer.execute('UPDATE structure SET observed_at=? WHERE site_key=? AND reference_url=?', (material['observed_at'], key, url))
        with inventory.connect() as writer:
            # Unvisited targets stay links; they are not newly inspected pages.
            writer.execute("DELETE FROM pages WHERE site_key=? AND state<>'fetched'", (key,))
        with catalog.publication_writer(inventory, key, merge=True) as (staged, _):
            yield staged
