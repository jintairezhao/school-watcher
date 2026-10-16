"""Execute public source evidence before installing or replacing a parser."""
import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from backend.database.db import db
from backend.database.models import Announcement, AnnouncementSource
from backend.database.source_governance_models import SourceValidation
from backend.scraper.http_client import same_school_url, validate_public_url
from backend.services.source_inventory import canonical_url

VALIDATION_VERSION = 8


def sample_failure_kind(samples):
    """Classify executed failures without treating a blocked read as a CSS error."""
    restricted = {'http_401', 'http_403', 'http_451', 'source_login_required',
                  'source_access_restricted', 'access_denied_page', 'human_verification', 'access_challenge'}
    kinds = []
    for sample in samples:
        if sample.get('matched'):
            continue
        reason, code = sample.get('reason', ''), sample.get('error_code', '')
        if code in restricted or reason in restricted or reason == 'needs_manual':
            kinds.append('access_restricted')
        elif reason == 'article_title_abbreviated' or code in ('http_404', 'http_410', 'response_too_large', 'non_html_resource',
                      'invalid_pdf', 'unsupported_content_encoding', 'dynamic_material_pending', 'dynamic_material_incomplete'):
            kinds.append('material_incomplete')
        else:
            kinds.append('rule_error')
    # An actual contradictory title, URL or source scope still needs attention,
    # even when another sample on the same page happens to return HTTP 403.
    return ('rule_error' if 'rule_error' in kinds else
            'material_incomplete' if 'material_incomplete' in kinds else
            'access_restricted' if kinds else '')


def _article_layout_ancestors(soup, config, title, page_url):
    """Disambiguate a navigation-named layout using its local article proof.

    A CMS may put its main content inside .nav_right. Only an exact visible
    headline, its verified local publication line and substantive article
    paragraphs can protect those enclosing layout divs. Actual navigation
    elements and sidebar/menu/related-content markers remain excluded.
    """
    from backend.scraper.article_observation import article_node_state, title_key, prune_unready_article_nodes
    from backend.services.publication_dates import inspect_article_date
    nav = re.compile(r'(?:^|[\s_-])(?:nav|navigation)(?:$|[\s_-])', re.I)
    other = re.compile(r'(?:^|[\s_-])(?:menu|sidebar|breadcrumb|related|recommend|toolbar|footer|copyright)(?:$|[\s_-])', re.I)
    protected = set()
    for heading in soup.select('h1,h2,h3,h4,h5,h6,[itemprop="headline"]'):
        if title_key(heading.get_text(' ', strip=True)) != title_key(title) or not title:
            continue
        branch = [heading, *heading.parents]
        if (article_node_state(heading) != 'ready' or any(node.name in ('a','nav','aside','footer')
                or node.name == 'form' and node.select('input:not([type="hidden"]),textarea,select,button')
                or other.search(' '.join(node.get('class', [])) + ' ' + node.get('id', ''))
                for node in branch if node.attrs is not None)):
            continue
        marked = [node for node in branch if node.attrs is not None and
                  nav.search(' '.join(node.get('class', [])) + ' ' + node.get('id', ''))]
        if not marked:
            continue
        for scope in heading.parents:
            if scope.name in ('body','html','[document]') or any(scope is node for node in marked):
                break
            if not scope.select('p,td,blockquote,pre'):
                continue
            material = BeautifulSoup(str(scope), 'lxml')
            if inspect_article_date(str(material), page_url, title).get('state') != 'verified':
                continue
            prune_unready_article_nodes(material)
            for node in list(material.find_all(True)):
                if node.attrs is not None and (node.name in ('nav','aside','footer','script','style')
                        or nav.search(' '.join(node.get('class', [])) + ' ' + node.get('id', ''))
                        or other.search(' '.join(node.get('class', [])) + ' ' + node.get('id', ''))):
                    node.decompose()
            evidence = _article_body_evidence(material, config or {}, title_key(title), page_url)
            if len(evidence['body_text']) >= 40:
                protected.update(id(node) for node in marked)
                break
    return protected


def _remove_article_noise(soup, config=None, *, title='', page_url=''):
    """Exclude site chrome before either identifying a title or measuring text."""
    # A class containing "header" is not necessarily site chrome: real CMS
    # pages wrap their entire article in .headerpage. Match marker tokens,
    # not arbitrary substrings that can remove the actual notification.
    noise = re.compile(r'(?:^|[\s_-])(?:nav|navigation|menu|footer|sidebar|copyright|breadcrumb|related|recommend|toolbar)(?:$|[\s_-])', re.I)
    header_marker = re.compile(r'(?:^|[\s_-])header(?:$|[\s_-])', re.I)
    layout_ancestors = _article_layout_ancestors(soup, config, title, page_url)
    # An article's own headline can live in .article__header. Preserve that
    # branch only after the title, local publication line and body agree.
    from backend.scraper.article_observation import article_heading, title_bound_body, prune_unready_article_nodes
    heading = article_heading(soup, title) if title and title_bound_body(soup, page_url, title) is not None else None
    headline_branch = {id(node) for node in [heading, *heading.parents]} if heading is not None else set()
    prune_unready_article_nodes(soup)
    # Some public CMS templates use aside for their main article column. Keep
    # ancestors of an executed, bounded body selector; explicit navigation and
    # sidebar markers still take precedence over any model-proposed selector.
    regions = soup.select(config['content_selector']) if config and config.get('content_selector') else []
    body_ancestors = {id(parent) for region in regions
                      if region.name not in ('html', 'body', 'head', 'aside', 'nav', 'header', 'footer')
                      for parent in region.parents}
    for node in list(soup.find_all(True)):
        if node.attrs is None:
            continue
        markers = ' '.join(node.get('class', [])) + ' ' + node.get('id', '')
        if (node.name in ('script', 'style', 'nav', 'footer')
                or (node.name == 'header' or header_marker.search(markers)) and id(node) not in headline_branch
                or node.name == 'aside' and id(node) not in body_ancestors
                or noise.search(markers) and id(node) not in layout_ancestors):
            node.decompose()


def _article_body_evidence(soup, config, title, page_url):
    """Use executed body regions or non-navigation prose/notice-labelled files."""
    from backend.services.source_governance import _normal
    from backend.scraper.article_resources import has_public_body_resource
    from backend.scraper.article_observation import (article_heading, publication_nodes,
                                                     without_publication_metadata)
    heading = article_heading(soup, title)
    metadata = publication_nodes(soup, heading)[0] if heading is not None else []
    configured = soup.select(config['content_selector']) if config.get('content_selector') else []
    # An all-page selector cannot promote navigation/files to article evidence.
    bounded = [node for node in configured if node.name not in ('body', 'html', 'head', 'title')]
    semantic = [] if config.get('content_selector') else soup.select('article,[itemprop="articleBody"]')
    regions = bounded or semantic
    paragraphs = regions if regions else soup.select('p,td,blockquote,pre') if not config.get('content_selector') or configured else []
    if not regions and (not config.get('content_selector') or configured):
        # A notice may be a table of faculties and their linked notices. Its
        # individual cells are short, while the actual data table is substantive.
        paragraphs += [table for table in soup.select('table') if len(table.select('tr')) >= 2
                       and all(len(row.find_all(['td', 'th'], recursive=False)) >= 2
                               for row in table.select('tr'))]
    texts = []
    for node in paragraphs:
        clean = without_publication_metadata(node, metadata)
        for link in clean.select('a,button,select'):
            link.decompose()
        text = clean.get_text(' ', strip=True)
        normalized = _normal(text).replace(title, '')
        if re.search(r'copyright|版权所有|备案号', text, re.I):
            continue
        if len(normalized) < 15:
            continue
        if len(normalized) < 120 and re.search(r'发布时间|发布日期|浏览次数|浏览量|点击量|文章来源|作者[:：]|编辑[:：]', text):
            continue
        texts.append(text)
    resources = []
    candidates = [resource for region in regions for resource in region.select(
        'a[href],img[src],[pdfsrc],embed[src],object[data]')]
    if not config.get('content_selector'):
        # Some CMS attachment notices have only a named PDF link in a plain div.
        # The complete notice label is required; generic material-download links
        # elsewhere on the page cannot prove the article body exists.
        candidates += [node for node in soup.select('a[href]')
                       if title and _normal(node.get('title') or node.get_text(' ', strip=True)) == title]
    for node in candidates:
        isolated = BeautifulSoup(str(node), 'lxml')
        if not has_public_body_resource([isolated], page_url):
            continue
        resource = node.get('pdfsrc') or node.get('href') or node.get('src') or node.get('data')
        address = canonical_url(urljoin(page_url, resource))
        if address not in resources:
            resources.append(address)
    return {'body_text': ' '.join(dict.fromkeys(texts)), 'attachment_urls': resources[:5],
            'body_basis': 'executed_selector' if bounded else 'semantic_article' if semantic else 'non_navigation_paragraphs'}


def _observed_article_headlines(soup, config):
    """Read explicit local headline roles, excluding body sections/site slogans."""
    from backend.services.source_governance import _normal
    title_role = re.compile(r'(?:article|news|content|detail|entry|post)[-_]?(?:title|headline)'
                            r'|(?:title|headline)[-_]?(?:article|news|content|detail|entry|post)', re.I)
    site_role = re.compile(r'(?:^|[\s_-])(?:site|logo|brand|slogan|banner)(?:$|[\s_-])', re.I)
    regions = soup.select(config['content_selector']) if config.get('content_selector') else []
    preceding = {id(region.find_previous_sibling()) for region in regions
                 if region.name not in ('body', 'html', 'head')}
    titles = []
    for node in soup.select('h1,h2,h3,[itemprop="headline"]'):
        local = [node] + list(node.parents)[:2]
        markers = [' '.join(parent.get('class', [])) + ' ' + parent.get('id', '') for parent in local]
        if any(site_role.search(value) for value in markers):
            continue
        explicit = node.get('itemprop') == 'headline' or any(title_role.search(value) and
            (parent is node or len(parent.select('h1,h2,h3')) == 1) for parent, value in zip(local, markers))
        article = node.find_parent('article')
        semantic = node.name == 'h1' and article is not None and len(article.select('h1')) == 1
        adjacent = node.name == 'h1' and id(node) in preceding
        if explicit or semantic or adjacent:
            value = _normal(node.get_text(' ', strip=True))
            if value and value not in titles:
                titles.append(value)
    return titles


def article_samples(config, records, fetcher):
    from backend.services.discovery_policy import get_policy
    from backend.services.source_governance import _normal
    from backend.services.source_review_recovery import page_problem
    from backend.scraper.acquisition import FetchFailure
    policy = get_policy()
    unique = list({canonical_url(row['url']): row for row in records}.values())
    target = min(len(unique), max(1, int(policy['article_sample_size'])))
    attempts = min(len(unique), max(target, int(policy['article_sample_attempts'])))
    required = 1 if len(unique) == 1 else max(2, int(policy['article_sample_matches']))
    samples, last_network_error = [], None
    retry_after = 0
    for record in unique[:attempts]:
        address = canonical_url(record['url'])
        sample = {'url': address, 'title': record['title'], 'matched': False}
        validate_public_url(address, resolve=False)
        if address == canonical_url(config.get('list_url', '')):
            sample['reason'] = 'article_is_listing'
            samples.append(sample)
            continue
        db.session.commit()
        try:
            html = fetcher(address, 'article')
        except FetchFailure as exc:
            if exc.outcome in ('network_error', 'unavailable'):
                last_network_error = exc
            retry_after = max(retry_after, exc.result.retry_after or 0)
            sample['reason'] = exc.outcome
            sample['error_code'] = exc.result.error_code
            sample['final_url'] = exc.result.final_url or address
            if exc.result.html and exc.result.status == 200 and exc.outcome in ('needs_adapter', 'denied'):
                from backend.services.source_governance import _snapshot
                sample['snapshot'] = _snapshot(exc.result.html, sample['final_url'], role='article', outcome=exc.outcome)
            samples.append(sample)
            continue
        final = canonical_url(getattr(html, 'final_url', address))
        sample.update(final_url=final, content_hash=hashlib.sha256(html.encode()).hexdigest())
        if not same_school_url(final, address):
            sample['reason'] = 'article_redirect_outside_site'
        elif final == canonical_url(config.get('list_url', '')):
            sample['reason'] = 'article_redirects_to_listing'
        elif (attachment := getattr(getattr(html, 'result', None), 'attachment', {})):
            filename = attachment.get('filename', '')
            stem = re.sub(r'\.pdf$', '', filename, flags=re.I)
            # Opaque CMS file IDs carry no textual title. The scoped list's
            # observed literal URL plus a complete read supplies limited file
            # identity; a different descriptive filename cannot supply it.
            opaque = bool(re.fullmatch(r'[a-fA-F0-9_-]{6,}', stem))
            sample['matched'] = bool(attachment.get('verified') and
                (_normal(stem) == _normal(record['title']) or opaque and final == address))
            sample.update(attachment=True, attachment_urls=[final], attachment_evidence=attachment,
                content_hash=attachment.get('sha256'), reason='matched' if sample['matched'] else 'attachment_identity_unconfirmed',
                body_basis='verified_pdf_and_observed_opaque_url' if opaque else 'verified_pdf_and_matching_filename',
                limited_identity=bool(opaque))
        elif (problem := page_problem(html)) not in ('', 'article_instead_of_column', 'attachment_instead_of_column'):
            # The recovery classifier describes why a document is not a LIST.
            # An actual article/attachment is precisely the expected document
            # here. Its title, URL and body still need the checks below.
            sample['reason'] = problem
        else:
            soup = BeautifulSoup(html, 'lxml')
            _remove_article_noise(soup, config, title=record['title'], page_url=final)
            title = _normal(record['title'])
            headings = [_normal(node.get_text(' ', strip=True)) for node in soup.select('h1,h2,h3,title')]
            headings += [_normal(node.get('content')) for node in soup.select('meta[property="og:title"]')]
            # A matching sidebar link alone is not an article identity.
            named = any(title and title in heading for heading in headings)
            if not named and title:
                # CMS templates often render the article heading in a plain
                # div/p/span. Require the complete standalone visible title,
                # excluding links and sidebar/navigation copies.
                named = any(_normal(node.get_text(' ', strip=True)) == title
                            and not node.find_parent(['a', 'aside', 'nav', 'header', 'footer'])
                            and not node.select('a[href]')
                            for node in soup.select('div,p,span,strong'))
            sample['title_matched'] = named
            sample['content_selector_count'] = len(soup.select(config['content_selector'])) if config.get('content_selector') else 0
            evidence = _article_body_evidence(soup, config, title, final)
            resources = evidence['attachment_urls']
            visible = _normal(soup.get_text(' ', strip=True))
            enough_body = bool(evidence['body_text'] or resources)
            sample['matched'] = bool(named and enough_body and len(visible) >= len(title))
            primary_titles = [_normal(node.get('content') or node.get_text(' ', strip=True))
                              for node in soup.select('h1,h2,h3,meta[property="og:title"]')]
            observed_headlines = _observed_article_headlines(soup, config)
            abbreviated = bool(re.search(r'(?:\.{3,}|…+)\s*$', record['title']))
            prefix = _normal(re.sub(r'(?:\.{3,}|…+)\s*$', '', record['title'])) if abbreviated else ''
            prefix_observed = bool(prefix and any(prefix in heading for heading in (observed_headlines or headings)))
            # A CMS's literal ellipsis is incomplete title evidence. Do not
            # turn a matching real prefix into a contradictory identity, or
            # count an arbitrary short prefix as a verified full notification.
            unclear_abbreviation = bool(abbreviated and (prefix_observed or len(prefix) < 12))
            if observed_headlines and not unclear_abbreviation and not any(title and title in heading for heading in observed_headlines):
                # A stale document/OG title cannot cover a contradictory actual
                # article heading, even when other sampled notices are correct.
                named = False
                sample.update(title_matched=False, matched=False)
            identity_conflict = bool(not named and enough_body and (primary_titles or observed_headlines) and not unclear_abbreviation)
            sample.update(listed_title_abbreviated=abbreviated,
                          observed_title_candidates=list(dict.fromkeys(headings))[:6], title_prefix_observed=prefix_observed,
                          observed_primary_title_candidates=observed_headlines[:3])
            sample['reason'] = ('matched' if sample['matched'] else 'article_title_abbreviated'
                                if unclear_abbreviation else 'article_identity_conflict'
                                if identity_conflict else 'article_identity_or_body_mismatch')
            sample['attachment'] = bool(resources)
            sample.update(attachment_urls=resources, body_basis=evidence['body_basis'],
                          body_excerpt=evidence['body_text'][:240])
            if sample['matched']:
                from backend.services.publication_dates import inspect_article_date
                sample['publication_date'] = inspect_article_date(html,address,record['title'])
            if not sample['matched']:
                # Reuse this exact read on the repair turn, including its original
                # DOM positions. No second network read or guessed body structure.
                from backend.services.source_governance import _snapshot
                sample['snapshot'] = _snapshot(html, final, role='article')
        samples.append(sample)
        matched = sum(row['matched'] for row in samples)
        if len(samples) >= target and matched >= required:
            break
    matched = sum(row['matched'] for row in samples)
    if matched < required and last_network_error:
        raise last_network_error
    limited = bool(matched and (len(unique) == 1 or any(row.get('limited_identity') for row in samples if row['matched'])
                               or any(row.get('reason') == 'article_title_abbreviated' for row in samples)))
    identity_conflicts = [row['url'] for row in samples if row.get('reason') in (
        'article_identity_conflict', 'attachment_identity_unconfirmed', 'article_is_listing',
        'article_redirects_to_listing', 'article_redirect_outside_site')]
    passed = matched >= required and not identity_conflicts
    return {'passed': passed, 'status': 'limited' if limited else 'full',
            'failure_kind': '' if passed else sample_failure_kind(samples),
            'identity_conflicts': identity_conflicts, 'retry_after': retry_after,
            'matched_count': matched, 'required_matches': required, 'available_count': len(unique),
            'sample_count': len(samples), 'samples': samples}


def pagination_sample(config, records, html, reference, scope, fetcher):
    from backend.scraper.engine import _next_page_url, PaginationScopeError
    from backend.services.source_governance import _column_scope, _snapshot
    try:
        target = _next_page_url(html, reference['url'], 1,
                                list_selector=config['list_selector'], scope=scope)
    except PaginationScopeError as exc:
        return {'state': 'rejected', 'errors': [str(exc)]}
    if not target:
        return {'state': 'not_observed', 'errors': []}
    validate_public_url(target, resolve=False)
    db.session.commit()
    document = fetcher(target, 'list')
    final = canonical_url(getattr(document, 'final_url', target))
    if urlsplit(final).hostname != urlsplit(config['list_url']).hostname:
        return {'state': 'rejected', 'url': target, 'final_url': final, 'errors': ['pagination_redirect_outside_site']}
    snapshot = _snapshot(document, final, role='pagination')
    following, errors, _ = _column_scope(config, snapshot, scope)
    if not following:
        errors.append('pagination_list_empty')
    if following and {row['url'] for row in following} == {row['url'] for row in records}:
        errors.append('pagination_repeats_first_page')
    return {'state': 'validated' if not errors else 'rejected', 'url': target, 'final_url': final,
            'content_hash': hashlib.sha256(document.encode()).hexdigest(), 'item_count': len(following),
            'sample_records': following[:5], 'errors': sorted(set(errors))}


def empty_region(html, config, scope):
    """Only an explicit empty message in the actual named region is conclusive."""
    import re
    from backend.services.source_governance import _normal
    if not scope:
        return None
    soup = BeautifulSoup(html, 'lxml')
    containers, headings = soup.select(scope['container_selector']), soup.select(scope['heading_selector'])
    if len(containers) != 1 or len(headings) != 1:
        return None
    container, heading = containers[0], headings[0]
    if container.name in ('html', 'body') or not (heading is container or container in heading.parents):
        return None
    name = heading.get_text(' ', strip=True)
    from backend.services.source_governance import _column_label
    if _normal(name) != _normal(_column_label(config['name'], config.get('group_name', ''))):
        return None
    text = container.get_text(' ', strip=True)
    if not re.search(r'暂无(?:内容|数据|记录|信息|通知)|没有相关(?:内容|数据|记录)|no\s+(?:records|results|data)', text, re.I):
        return None
    return {'name': name, 'container': scope['container_selector'], 'heading': scope['heading_selector'], 'empty': True}


def record_validation(school_id, config, validation, department_id=None):
    from backend.services.source_governance import _hash, _json
    detail = dict(validation, school_id=school_id, department_id=department_id, config_hash=_hash(config),
                  list_url=config['list_url'], name=config['name'], validation_version=VALIDATION_VERSION)
    event = SourceValidation(school_id=school_id, department_id=department_id,
        config_hash=_hash(config), validation_json=_json(detail))
    db.session.add(event)
    return detail


def same_column(source, config, records, validation):
    """Name/URL alone never merge distinct widgets on the same university page."""
    from backend.services.source_governance import _normal
    event = SourceValidation.query.filter_by(department_id=source.id).order_by(SourceValidation.id.desc()).first()
    previous = json.loads(event.validation_json) if event else {}
    before, after = previous.get('region') or {}, validation.get('region') or {}
    from backend.services.source_governance import _column_label
    same_name = _normal(_column_label(source.name, source.group_name or '')) == _normal(
        _column_label(config['name'], config.get('group_name', '')))
    if before and after:
        exact_region = all(before.get(key) and before.get(key) == after.get(key) for key in ('container', 'heading', 'name'))
        if exact_region:
            return True
        # Two independently named containers remain separate even when they
        # carry some of the same announcements or use identical labels.
        if before.get('container') and after.get('container') and before['container'] != after['container']:
            from backend.services.source_governance import read_snapshot
            try:
                current = BeautifulSoup(read_snapshot(validation['list_snapshot']), 'lxml')
                if current.select(before['container']):
                    return False
            except (KeyError, ValueError, OSError):
                return False
    addresses = {canonical_url(row['url']) for row in records}
    if not addresses or not same_name:
        return False
    observed = {canonical_url(url) for url, in db.session.query(AnnouncementSource.article_url).filter(
        AnnouncementSource.department_id == source.id, AnnouncementSource.article_url.in_(addresses))}
    observed.update(canonical_url(url) for url, in db.session.query(Announcement.url).filter(
        Announcement.department_id == source.id, Announcement.url.in_(addresses)))
    if len(observed) >= min(2, len(addresses)):
        return True
    # Identical executed extraction rules are reusable; a changed selector
    # without saved region/article continuity is a separate proposed source.
    return bool(same_name and source.list_selector == config['list_selector'] and
                source.title_selector == config['title_selector'] and source.link_selector == config['link_selector'])
