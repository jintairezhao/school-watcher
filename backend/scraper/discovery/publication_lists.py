"""Discover separately scoped publication lists, including multiple lists on one page."""
from collections import defaultdict
from urllib.parse import urljoin, urlsplit
from datetime import datetime, timezone
import hashlib
import re

from bs4 import BeautifulSoup
import soupsieve

from backend.scraper.cms_registry import load_selector_profiles
from backend.scraper.change_detector import parse_date
from backend.scraper.detectors.dom_analyzer import find_repeating_blocks
from backend.scraper.detectors.title_quality import is_junk_title
from backend.services.source_inventory import canonical_url, resolve_page_link
from .structure import ARTICLE, MORE, SKIP, clean, locator, nearby_heading
from .publication_tabs import tab_bindings
from backend.scraper.date_elements import publication_date_text, infer_publication_date_selector


def is_more(text):
    return text in MORE or bool(re.fullmatch(r'(?:read(?:\s+more)?|learn\s+more|more|更多|查看更多|了解(?:更多)?)\s*[+>»]*', text.strip(), re.I))


def title_text(element):
    # Icon-font private-use characters do not belong to a column name.
    return clean(re.sub(r'[\ue000-\uf8ff]', '', element.get_text(' ', strip=True)))


def listing_alias(block, current, url, target):
    """Record a breadcrumb identity only when this URL matches its real pager.

    Literal templates are read as data. No script is executed and no URL is
    generated. The relationship layer still requires a second fetched list.
    """
    if urlsplit(target).netloc != urlsplit(url).netloc:
        return None
    token = r'\$?\{(?:PageIndex|page|pageNum|pageNo)\}'
    for script in block.parent.find_all('script'):
        for match in re.finditer(r'''(['"])([^'"\n]*\$?\{(?:PageIndex|page|pageNum|pageNo)\}[^'"\n]*)\1''', script.get_text()):
            template = urljoin(url, match.group(2))
            parts = re.split(token, template)
            if len(parts) != 2:
                continue
            pattern = r'\d+'.join(re.escape(part) for part in parts)
            if re.fullmatch(pattern, url):
                return {'url': canonical_url(url), 'canonical_url': target,
                        'breadcrumb_locator': locator(current),
                        'template': template, 'script_locator': locator(script)}
    return None


def pagination_identity(first_item, heading, url):
    """Bind pager evidence independently of which layout supplied the heading.

    Use the nearest shared heading/list container. A pager from a neighboring
    module cannot identify this list, and only literal URLs are read as data.
    """
    if not heading.get('column_url') or not heading.get('heading_locator'):
        return None
    document = first_item
    while document.parent is not None:
        document = document.parent
    current = document.select_one(heading['heading_locator'])
    if current is None:
        return None
    for scope in first_item.parents:
        if scope.name in ('body', 'html', '[document]'):
            break
        if not any(parent is scope for parent in [current, *current.parents]):
            continue
        # listing_alias examines the supplied block's parent. This branch sits
        # inside the nearest complete module containing the heading and list.
        branch = next((child for child in scope.find_all(recursive=False)
                       if child is first_item or any(parent is child for parent in first_item.parents)), None)
        if branch is None:
            return None
        alias = listing_alias(branch, current, canonical_url(url), heading['column_url'])
        if alias:
            script = document.select_one(alias['script_locator'])
            targets = {resolve_page_link(url, match.group(2)) for match in re.finditer(
                r'''\breturn\s+(['"])([^'"\n{}]+)\1\s*;''', script.get_text())}
            targets = {target for target in targets if target and not ARTICLE.search(target)
                       and urlsplit(target).netloc == urlsplit(url).netloc}
            if len(targets) == 1:
                alias['first_page_url'] = targets.pop()
            return alias
        # Do not climb into another module after finding the list's own heading.
        return None
    return None


def select_node(item, selector):
    if not selector:
        return None
    return item if soupsieve.match(selector, item) else item.select_one(selector)


def scope_selector(parent):
    """Prefer a unique stable ID; a full locator disambiguates identical CSS classes."""
    if parent.get('id'):
        selector = '#' + soupsieve.escape(parent['id'])
        document = parent
        while document.parent is not None:
            document = document.parent
        if len(document.select(selector)) == 1:
            return selector
    return locator(parent)


def wrapped_heading(first_item):
    """Traverse empty layout wrappers, stopping before adjacent content widgets."""
    branch = first_item.parent
    while branch is not None and branch.parent is not None:
        parent = branch.parent
        if parent.name in ('body', 'html', '[document]'):
            break
        siblings = [c for c in parent.find_all(recursive=False) if c is not branch
                    and c.name not in ('script', 'style') and clean(c.get_text(' ', strip=True))]
        if siblings:
            if len(siblings) == 1:
                candidate = siblings[0]
                if (candidate.name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6') or
                        re.search(r'tit|head|hd', ' '.join(candidate.get('class', [])), re.I)):
                    return clean(candidate.get_text(' ', strip=True)), candidate
                links = candidate.select('a[href]')
                text = title_text(candidate)
                # The sole adjacent short block supplies one title and its own
                # explicit "more" link, even when CSS classes are opaque.
                if (2 <= len(text) <= 50 and len(links) == 1 and is_more(title_text(links[0]))
                        and not candidate.select('ul,ol,li,article')):
                    return text, candidate
            break
        branch = parent
    return '', None


def heading_evidence(first_item, url, tabs=None):
    from .wordpress_publications import archive_heading
    wordpress = archive_heading(first_item, url)
    if wordpress:
        return wordpress
    from .metinfo_publications import metinfo_heading
    metinfo = metinfo_heading(first_item, url)
    if metinfo:
        return metinfo
    from .listing_headings import rightnew_heading
    listing = rightnew_heading(first_item, url)
    if listing:
        return listing
    from .cup_news_headings import cup_news_heading
    cup = cup_news_heading(first_item, url)
    if cup:
        return cup
    from .medical_publications import medical_heading
    medical = medical_heading(first_item, url)
    if medical:
        return medical
    # This ZCMS module wraps its headings in several divs. Bind only the
    # nearest complete module; broken neighboring modules cannot lend a title.
    module = first_item.find_parent(class_='c-shiyouxueyuan-new-module__half-width')
    if module is not None:
        headings = module.select('.c-shiyouxueyuan-new-module__head-title-words')
        more = module.select('.c-shiyouxueyuan-new-module__head-more a[href]')
        result = {'name': '', 'heading_locator': '', 'column_url': '', 'column_link_locator': ''}
        if len(headings) != 1 or len(more) > 1:
            return dict(result, heading_ambiguous=True)
        label = clean(headings[0].get_text(' ', strip=True))
        if not 2 <= len(label) <= 40 or label in MORE | SKIP:
            return dict(result, heading_ambiguous=True)
        result.update(name=label, heading_locator=locator(headings[0]))
        if more:
            href = more[0]['href'].strip()
            target = resolve_page_link(url, href)
            if target and not ARTICLE.search(target):
                result.update(column_url=target, column_link_locator=locator(more[0]))
        return result
    for parent in [first_item, *first_item.parents]:
        binding = (tabs or {}).get(id(parent))
        if binding is not None:
            panel, control = binding.panel, binding.control
            result = {'name': '', 'heading_locator': '', 'column_url': '', 'column_link_locator': '',
                      'tab_panel_locator': locator(panel), 'heading_method': binding.method}
            if control is None:
                return dict(result, heading_ambiguous=True)
            anchors = [control] if control.name == 'a' else control.select('a[href]')
            title = anchors[0] if len(anchors) == 1 else control
            name = clean(title.get_text(' ', strip=True))
            if not 2 <= len(name) <= 40 or len(anchors) > 1:
                return dict(result, heading_ambiguous=True)
            result.update(name=name, heading_locator=locator(title), tab_control_locator=locator(control))
            if binding.group_control is not None:
                result.update(column_group_name=clean(binding.group_control.get_text(' ', strip=True)),
                              column_group_locator=locator(binding.group_control))
            if binding.column_href and binding.column_script is not None and binding.column_link is not None:
                target = resolve_page_link(url, binding.column_href)
                if target and not ARTICLE.search(target):
                    result.update(column_url=target, column_link_locator=locator(binding.column_link),
                                  column_link_method='official_tab_handler',
                                  column_script_locator=locator(binding.column_script), column_tab_index=binding.tab_index)
                return result
            choices = ([binding.column_link] if binding.column_link is not None else anchors) + [
                a for a in panel.select('a[href]') if clean(a.get_text(' ', strip=True)) in MORE]
            for anchor in choices:
                href = anchor.get('href', '').strip()
                if not resolve_page_link(url, href):
                    continue
                target = resolve_page_link(url, href)
                if target and not ARTICLE.search(target):
                    result.update(column_url=target, column_link_locator=locator(anchor))
                    break
            return result
    label, heading = nearby_heading(first_item)
    if not heading:
        label, heading = wrapped_heading(first_item)
    if not heading:
        # On ZCMS listing pages the current column is named by its breadcrumb.
        # Require the terminal crumb to point to this exact page, and bind only
        # the adjacent main article list, never another widget in the sidebar.
        listing = first_item.find_parent('ul', class_='middleArticle__articleList')
        block = listing.parent if listing is not None else first_item.find_parent('div', class_='middleArticle__art')
        if block is not None and 'middleArticle__art' in block.get('class', []) and block.parent is not None:
            crumbs = block.parent.select(':scope > .middleArticle__position > .middleArticle__position--label')
            links = crumbs[0].select('a[href]') if len(crumbs) == 1 else []
            if links:
                current = links[-1]
                href = current['href'].strip()
                target = resolve_page_link(url, href)
                name = clean(current.get_text(' ', strip=True))
                if target == canonical_url(url) and 2 <= len(name) <= 40 and name not in MORE | SKIP:
                    return {'name': name, 'heading_locator': locator(current), 'column_url': target,
                            'column_link_locator': locator(current), 'heading_method': 'current_page_breadcrumb'}
                alias = listing_alias(block, current, canonical_url(url), target)
                if alias and 2 <= len(name) <= 40 and name not in MORE | SKIP:
                    return {'name': name, 'heading_locator': locator(current), 'column_url': target,
                            'column_link_locator': locator(current), 'heading_method': 'pagination_page_breadcrumb',
                            'listing_alias': alias}
            # The card layout names the list in its explicitly selected sidebar
            # item. Unselected navigation links cannot supply a column identity.
            if block.parent.parent is not None:
                selected = block.parent.parent.select(
                    ':scope > .c-main__left li.middleLeft__left--navClick > a[href]')
                if len(selected) == 1:
                    current = selected[0]
                    target = resolve_page_link(url, current['href'].strip())
                    name = clean(current.get_text(' ', strip=True))
                    alias = listing_alias(block, current, canonical_url(url), target)
                    if 2 <= len(name) <= 40 and name not in MORE | SKIP and (target == canonical_url(url) or alias):
                        return {'name':name, 'heading_locator':locator(current), 'column_url':target,
                            'column_link_locator':locator(current), 'heading_method':'current_page_sidebar',
                            **({'listing_alias':alias} if alias else {})}
        return {'name': '', 'heading_locator': '', 'column_url': '', 'column_link_locator': ''}
    # Action links are not part of the title, including translated buttons
    # between a Chinese title and its English subtitle.
    label = clean(' '.join(text for text in heading.stripped_strings if not is_more(text)))
    links = ([heading] if heading.name == 'a' else []) + heading.select('a')
    titles = {(clean(a.get_text(' ', strip=True)), a.get('href', '')) for a in links
              if 2 <= len(clean(a.get_text(' ', strip=True))) <= 40
              and not is_more(title_text(a)) and title_text(a) not in SKIP}
    if len(titles) > 1:
        return {'name': '', 'heading_locator': '', 'column_url': '', 'column_link_locator': '',
                'heading_ambiguous': True}
    label_node = heading
    # Separate an actual heading from subtitles in the same visual title block.
    for node in heading.find_all(True):
        classes = ' '.join(node.get('class', []))
        text = title_text(node)
        if (node.name in ('h1', 'h2', 'h3', 'h4', 'h5') or
                re.search(r'(?:^|\s)(?:fz|font)[-_]?\d+(?:\s|$)', classes)) and 2 <= len(text) <= 40:
            label, label_node = text, node
            break
    # A title block can contain both the title and a separate "more" link.
    for link in links:
        text = title_text(link) or clean(link.get('title'))
        if 2 <= len(text) <= 40 and not is_more(text) and text not in SKIP:
            label, label_node = text, link
            break
    label = clean(re.sub(r'查看更多|更多\s*[>»+]*|\b(?:read(?:\s+more)?|learn\s+more|more)\b\s*[+>»]*', '', label, flags=re.I))
    if is_more(label):
        label = ''
    if not (2 <= len(label) <= 40 and label not in SKIP):
        label = ''
    column_url, column_locator = '', ''
    for link in links:
        href = link.get('href', '').strip()
        if not resolve_page_link(url, href):
            continue
        target = resolve_page_link(url, href)
        text = clean(link.get_text(' ', strip=True))
        if target and not ARTICLE.search(target) and (text == label or is_more(text)):
            column_url, column_locator = target, locator(link)
            break
    if not column_url:
        # A "more" button can sit beside the title instead of inside it. Stop at their
        # nearest shared container so adjacent widgets cannot lend one another a URL.
        scope = heading
        while scope.parent is not None and not any(p is scope for p in first_item.parents):
            scope = scope.parent
        choices = {}
        for link in scope.select('a[href]'):
            if not is_more(title_text(link)):
                continue
            href = link['href'].strip()
            if not resolve_page_link(url, href):
                continue
            target = resolve_page_link(url, href)
            if target and not ARTICLE.search(target):
                choices[target] = locator(link)
        if len(choices) == 1:
            column_url, column_locator = next(iter(choices.items()))
    return {'name': label, 'heading_locator': locator(label_node) if label else '',
            'column_url': column_url, 'column_link_locator': column_locator}


def samples_for(items, config, url):
    samples, dates, article_links = {}, [], {}
    today = datetime.now(timezone.utc).date()
    for item in items:
        title_node = select_node(item, config.get('title_selector', 'a'))
        anchor = select_node(item, config.get('link_selector') or 'a[href]')
        if title_node is None or anchor is None or not anchor.get('href'):
            continue
        title = clean(title_node.get('title') or title_node.get('data-title') or title_node.get_text(' ', strip=True))
        href = anchor['href'].strip()
        if not resolve_page_link(url, href):
            continue
        target = resolve_page_link(url, href)
        date_node = select_node(item, config.get('date_selector', ''))
        # Dates quoted in a news summary describe the story, not its publication.
        # Use a date element/attribute or direct text beside the article link.
        text = publication_date_text(date_node) if date_node is not None else ' '.join(
            str(t) for t in item.find_all(string=True, recursive=False))
        match = re.search(r'20\d{2}[年./-]\s*\d{1,2}[月./-]\s*\d{1,2}', text)
        if not match and date_node is not None:
            match = re.fullmatch(r'\s*\d{1,2}\s+20\d{2}[-/.]\d{1,2}\s*', text)
        published = parse_date(match.group(0)) if match else None
        # Short labels also occur on document-shaped institution introductions.
        # Require a row publication date as well as an article address before
        # relaxing the short-title heuristic.
        if ((len(title) < 6 and not ARTICLE.search(target))
                or is_junk_title(title, article_url=target if published or config.get('explicit_video_publication') else '')):
            continue
        if not target or (published is None and not ARTICLE.search(target)):
            continue
        if published and published.date() <= today:
            dates.append(published.date().isoformat())
        samples[target] = {'title': title, 'url': target, 'date': published.date().isoformat() if published else None}
        # Keep every occurrence, including an image linking to the same article.
        # A title and URL alone cannot distinguish a news row from real navigation.
        for link in ([item] if item.name == 'a' else []) + item.select('a[href]'):
            if resolve_page_link(url, link.get('href')) == target:
                position = locator(link)
                article_links[(target, position)] = {'url': target, 'locator': position}
    return list(samples.values()), max(dates) if dates else None, list(article_links.values())


def publication_lists(html, url):
    soup = BeautifulSoup(html, 'lxml')
    from .wordpress_publications import POST_LIST, page_kind
    if page_kind(soup) in ('article', 'search'):
        return []
    from .major_directories import major_scopes
    reviewed_majors = major_scopes(soup, url)
    major_regions = {id(scope) for scope in reviewed_majors}
    spanning_majors = {id(parent) for scope in reviewed_majors for parent in scope.parents}
    tabs = tab_bindings(soup, url)
    spanning_panels = {id(ancestor) for binding in tabs.values() for ancestor in [binding.panel, *binding.panel.parents]}
    configs = [dict(p, method='profile_match', confidence=0.95) for p in load_selector_profiles()
               if p.get('list_selector', '').strip() not in ('a', 'a[href]')]
    from .medical_publications import medical_configs
    reviewed_configs, reviewed_widgets = medical_configs(soup, url)
    from .metinfo_publications import METINFO_CONFIG
    configs = reviewed_configs + [POST_LIST, METINFO_CONFIG] + configs
    # The analyzer removes script nodes; retain an untouched tree for evidence locators.
    for candidate in find_repeating_blocks(BeautifulSoup(html, 'lxml'), min_repeat=2, min_text_len=8):
        # Small or undated official lists remain eligible; article evidence is checked below.
        # An image and its title often repeat the same article URL; validate distinct articles later.
        if candidate['link_ratio'] < 0.6:
            continue
        selector = candidate['parent_selector'] + ' > ' + candidate['item_tag']
        if candidate['item_class']:
            selector += ''.join('.' + soupsieve.escape(c) for c in candidate['item_class'].split())
        configs.append({'list_selector': selector, 'title_selector': 'a', 'link_selector': 'a',
                        'date_selector': 'time, span, em, i, .date, .time',
                        'content_selector': 'div.v_news_content, div.article-content, article',
                        'method': 'dom_analysis', 'confidence': 0.8})

    found = []
    examined = set()
    for config in configs:
        signature = tuple(config.get(k, '') for k in ('list_selector', 'title_selector', 'link_selector', 'date_selector'))
        if signature in examined:
            continue
        examined.add(signature)
        try:
            matches = soup.select(config['list_selector'])
        except soupsieve.SelectorSyntaxError:
            continue
        groups = defaultdict(list)
        for item in matches:
            if id(item) in spanning_majors or any(id(parent) in major_regions for parent in [item, *item.parents]):
                continue
            if config['method'] != 'official_medical_module' and any(
                    id(parent) in reviewed_widgets for parent in [item, *item.parents]):
                continue
            # Navigation and institutional lists must not become publication channels.
            navigation = item.find_parent(['nav', 'header', 'footer']) or any(
                re.search(r'(?:^|[\s_-])(?:nav|menu|header|footer)(?:$|[\s_-])',
                          ' '.join(parent.get('class', [])) + ' ' + parent.get('id', ''), re.I)
                for parent in item.parents if parent.name not in ('body', 'html', '[document]'))
            if navigation or id(item) in spanning_panels:
                continue
            groups[id(item.parent)].append(item)
        for items in groups.values():
            if len(items) < config.get('minimum_items', 2):
                continue
            parent = items[0].parent
            # Keep the original selector as a filter, scoped to this concrete list parent.
            parent_selector = scope_selector(parent)
            selector = parent_selector + ' > :is(' + config['list_selector'] + ')'
            selected = soup.select(selector)
            effective = config
            if config['method'] == 'dom_analysis':
                effective = dict(config, date_selector=infer_publication_date_selector(selected))
            samples, latest, article_links = samples_for(selected, effective, url)
            if config['method'] == 'dom_analysis' and len(samples) < len(selected) * 0.6:
                title_link = 'a[title], .name a, h3 a, h2 a, a:not(:has(img))'
                alternative = dict(effective, title_selector=title_link, link_selector=title_link)
                other_samples, other_latest, other_links = samples_for(selected, alternative, url)
                if len(other_samples) > len(samples):
                    effective, samples, latest, article_links = alternative, other_samples, other_latest, other_links
            if len(samples) < config.get('minimum_items', 2) or len(samples) / max(1, len(selected)) < 0.6:
                continue
            heading = heading_evidence(items[0], url, tabs)
            if alias := pagination_identity(items[0], heading, url):
                heading['listing_alias'] = alias
            name = heading['name']
            if name and any(name == sample['title'] for sample in samples):
                continue  # An article headline cannot name its enclosing column.
            result = {k: v for k, v in effective.items() if k != 'list_selector'}
            result.update(heading)
            result.update(list_selector=selector,
                          container_locator=locator(parent), samples=samples[:8],
                          article_urls=[s['url'] for s in samples], item_count=len(samples),
                          article_links=article_links,
                          dated_item_count=sum(bool(s['date']) for s in samples),
                          publication_dates_complete=all(s['date'] for s in samples),
                          latest_publication=latest, verified=False,
                          identity=hashlib.sha256((url + '\0' + name + '\0' + parent_selector).encode()).hexdigest()[:24])
            found.append(result)

    # Multiple profiles commonly describe the same items; retain distinct named official scopes.
    found.sort(key=lambda f: (all(len(s['title']) <= 200 for s in f['samples']),
                             f['method'] == 'profile_match' and f.get('title_selector') not in ('a', 'a[href]', ''),
                             f['item_count'], f['dated_item_count'], f['confidence']), reverse=True)
    result = []
    for feed in found:
        urls = set(feed['article_urls'])
        duplicate = next((f for f in result if f['name'] == feed['name'] and
                          f.get('column_url', '') == feed.get('column_url', '') and
                          f.get('column_group_name') == feed.get('column_group_name') and
                          (f['container_locator'] == feed['container_locator'] or bool(feed['name'])) and
                          urls <= set(f['article_urls'])), None)
        if duplicate:
            duplicate.setdefault('alternative_selectors', []).append(feed['list_selector'])
            positions = {(link['url'], link['locator']): link
                         for link in duplicate['article_links'] + feed['article_links']}
            duplicate['article_links'] = list(positions.values())
            continue
        result.append(feed)
    # A single independently detected list may use the document's exact column
    # title. Never apply a whole-page label across several unrelated widgets.
    if len(result) == 1 and not result[0].get('name') and not result[0].get('heading_ambiguous') and soup.title:
        title = clean(soup.title.get_text(' ', strip=True))
        parts = re.split(r'\s*[|｜_—–-]\s*', title)
        label = parts[0]
        if (2 <= len(label) <= 20 and
                re.search(r'通知|公告|招生|推免|夏令营|新闻|动态|公示|讲座', label)):
            result[0].update(name=label, heading_locator=locator(soup.title),
                             heading_method='single_list_document_title')
    return result
