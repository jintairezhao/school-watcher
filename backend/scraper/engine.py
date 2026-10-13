"""通用爬取引擎 — 支持翻页、增量抓取、智能变更检测、选择器探测"""

import logging
import re
import os
import json
from datetime import datetime, timezone
from urllib.parse import urljoin

from backend.scraper.http_client import requests, validate_public_url
from bs4 import BeautifulSoup

from backend.database.db import db
from backend.database.models import School, Department, Announcement, ScrapeLog
from backend.scraper.change_detector import parse_date
from backend.scraper.date_elements import publication_date_text
from backend.scraper.detectors.title_quality import (
    is_junk_title, extract_best_title, date_from_url, extract_title_text, observed_title_variants)
from backend.scraper.selector.selector_store import save_element_signatures, auto_heal_selectors
from backend.scraper.cms_registry import load_selector_profiles

logger = logging.getLogger(__name__)

MAX_PAGES = max(1, min(50, int(os.environ.get('WATCHER_MAX_PAGES', '3'))))
# MAX_PAGES is a scheduling slice, never a completeness limit.
MAX_TOTAL_PAGES = 2000
INCREMENTAL_THRESHOLD = 3  # 连续N条已存在通知时停止翻页（增量抓取）

# ---- 选择器探测：常见中国高校 CMS 模式 ----
# 模板定义在 cms_profiles.yaml（数据驱动），新增 CMS 无需改此文件
SELECTOR_PROFILES = load_selector_profiles()


def _fetch_html(url: str, follow_js_redirect: bool = True,
                allow_browser_fallback: bool = True,
                _redirect_chain: set | None = None,
                raise_fetch_errors: bool = False, *, purpose: str = 'directory',
                source_id: str = '', readiness_selector: str = '', policy=None) -> str:
    """Compatibility entrypoint; acquisition is owned by the shared coordinator.

    Real collection callers request typed errors. Legacy diagnostics may still ask
    for an empty string, while successful text retains its final URL as metadata.
    """
    from backend.scraper.acquisition import FetchRequest, FetchedHTML, FetchFailure, fetch_or_raise
    validate_public_url(url, resolve=False)
    settings = dict(policy or {}, follow_document_redirects=follow_js_redirect)
    request = FetchRequest(url=url, purpose=purpose, source_id=str(source_id),
            readiness_selector=readiness_selector, policy=settings,
            browser_allowed=allow_browser_fallback and os.environ.get('WATCHER_BROWSER', '1') != '0')
    try:
        return FetchedHTML(fetch_or_raise(request))
    except FetchFailure:
        if raise_fetch_errors:
            raise
        return ''


def _extract_text(html_content: str) -> str:
    soup = BeautifulSoup(html_content, 'lxml')
    for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
        tag.decompose()
    text = soup.get_text(separator='\n')
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


_ATTACHMENT_EXT = re.compile(
    r'\.(pdf|docx?|xlsx?|pptx?|zip|rar|7z|txt|csv|jpg|jpeg|png|gif)(\?|$)',
    re.IGNORECASE,
)


def _extract_attachment_links(content_elem) -> list:
    """提取正文中的附件链接（文件下载 + 图片），用于去重哈希。

    正文文字相同的两条通知，若附件不同（PDF 版本更新、海报图片更换等），
    应视为两条不同通知。仅按「标题+文字」哈希会把它们误判为重复。
    """
    links = []
    for tag in content_elem.find_all(['a', 'img']):
        val = tag.get('href') or tag.get('src') or ''
        if _ATTACHMENT_EXT.search(val):
            links.append(val)
    return links


def _resolve_url(base_url: str, link: str) -> str:
    if not link:
        return None
    return urljoin(base_url, link)


PAGINATION_SCOPE_VERSION = 1


class PaginationScopeError(ValueError):
    def __init__(self):
        super().__init__('pagination_scope_unconfirmed')


def _pagination_root(items):
    roots = []
    for item in items:
        root = next((node for node in [item, *item.parents]
                     if node.name in ('ul', 'ol', 'table') or node.get('role') == 'list'), None)
        roots.append(root)
    if all(root is not None for root in roots):
        if len({id(root) for root in roots}) != 1:
            raise PaginationScopeError()
        return roots[0]
    # Card lists need not use ul/li. Their selected entries must still share a
    # local parent, rather than joining independent widgets at body level.
    for parent in items[0].parents:
        if parent.name in ('html', 'body', '[document]'):
            break
        if all(any(node is parent for node in item.parents) for item in items):
            headings = [node for node in parent.select('h1,h2,h3,h4,h5,h6')
                        if not any(item is node or any(ancestor is item for ancestor in node.parents) for item in items)]
            if len(headings) > 1:
                raise PaginationScopeError()
            return parent
    raise PaginationScopeError()


def _pagination_owner(soup, anchor, selected_root, scope=None):
    """Bind an observed pager to its nearest actual list and heading region."""
    def paging_or_navigation(node):
        for parent in [node, *node.parents]:
            if parent.name in ('nav', 'header', 'footer'):
                return True
            marker = ' '.join([parent.get('id', ''), *parent.get('class', [])])
            if re.search(r'(?:^|[ _-])(?:pag(?:e|ing|ination)|pager)(?:[ _-]|$)|wp_paging', marker, re.I):
                return True
        links = node.select('a[href]')
        return bool(links and all(re.fullmatch(r'\d+|[‹›«»<>]|首页|末页|上一页|下一页|上页|下页|next|prev',
                                             link.get_text(' ', strip=True), re.I) for link in links))

    roots = [node for node in soup.select('ul,ol,table,[role="list"]')
             if node.select('a[href]') and (node is selected_root or not paging_or_navigation(node))]
    # Neighboring publication cards may use div/article instead of ul. Their
    # repeated linked children are equally real list boundaries.
    for node in soup.select('div,section,article'):
        if node is selected_root or any(parent is selected_root for parent in node.parents) or paging_or_navigation(node):
            continue
        children = [child for child in node.find_all(recursive=False)
                    if child.select('a[href]') and not paging_or_navigation(child)]
        if (node.name == 'article' and node.select('a[href]')) or (len(children) >= 2 and len({child.name for child in children}) == 1):
            roots.append(node)
    if not any(node is selected_root for node in roots):
        roots.append(selected_root)
    roots = [node for node in roots if node is selected_root or not any(
        other is not node and any(parent is node for parent in other.parents) for other in roots)]
    heading = soup.select_one(scope.get('heading_selector', '')) if scope and scope.get('heading_selector') else None
    for region in anchor.parents:
        local = [node for node in roots if node is region or any(parent is region for parent in node.parents)]
        if not local:
            # An unknown one-item card need not resemble a known CMS list.
            # Its own non-control links still bound this pager to that widget;
            # do not cross the wrapper and attach it to an earlier ul.
            foreign_links = [link for link in region.select('a[href]') if link is not anchor
                             and not paging_or_navigation(link)
                             and not set(link.get('rel', [])) & {'next', 'prev'}
                             and not re.fullmatch(r'\d+|[‹›«»<>]|首页|末页|上一页|下一页|上页|下页|next|prev',
                                                  link.get_text(' ', strip=True), re.I)]
            if foreign_links:
                return None
            continue
        tags = list(region.find_all(True))
        positions = {id(node): index for index, node in enumerate(tags)}
        if any(node is region for node in local):
            return region
        index = positions[id(anchor)]
        headings = [node for node in tags if (node.name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')
                    or heading is not None and node.name == heading.name and heading.get('class')
                    and set(node.get('class', [])) == set(heading.get('class', [])))
                    and not any(root is node or any(parent is root for parent in node.parents) for root in local)]
        preceding = [root for root in local if positions[id(root)] < index]
        previous = max(preceding, key=lambda node: positions[id(node)]) if preceding else None
        end = max((positions[id(node)] for node in [previous, *previous.find_all(True)]), default=-1) if previous else -1
        # A new peer heading between a list and this pager starts the next
        # region. This also handles a pager above its own list.
        boundaries = [node for node in headings if end < positions[id(node)] < index]
        if previous is not None and not boundaries:
            return previous
        following = [root for root in local if positions[id(root)] > index
                     and not any(index < positions[id(h)] < positions[id(root)] for h in headings)]
        return min(following, key=lambda node: positions[id(node)]) if following else None
    return None


def _next_page_url(html: str, current_url: str, page_number: int, *,
                   list_selector: str, scope: dict | None = None) -> str | None:
    """Require a real list scope and follow only its observed pagination."""
    if not isinstance(list_selector, str) or not list_selector.strip():
        raise PaginationScopeError()
    soup = BeautifulSoup(html, 'lxml')
    items = soup.select(list_selector)
    if not items:
        raise PaginationScopeError()
    root = _pagination_root(items)
    container = None
    if scope and scope.get('container_selector'):
        containers = soup.select(scope['container_selector'])
        if len(containers) != 1 or not all(item is containers[0] or any(parent is containers[0] for parent in item.parents) for item in items):
            raise PaginationScopeError()
        container = containers[0]
    candidates = []
    for anchor in soup.select('a[href]'):
        href = anchor.get('href', '').strip()
        if not href or href.lower().startswith(('javascript:', 'mailto:', 'tel:')):
            continue
        if href.startswith('#') and not href.startswith(('#/', '#!/')):
            continue
        label = anchor.get_text(' ', strip=True)
        rel = anchor.get('rel', [])
        next_label = bool(re.fullmatch(r'(?:下一页|下页|next(?:\s+page)?)[ >»›]*', label, re.I))
        numbered = label == str(page_number + 1)
        if 'next' not in rel and not next_label and not numbered:
            continue
        if root is not None:
            if any(item is anchor or any(parent is item for parent in anchor.parents) for item in items):
                continue
            if container is not None and scope.get('heading_selector'):
                heading = soup.select_one(scope['heading_selector'])
                # Executed heading + entries give an explicit local region;
                # controls outside it belong to another widget. Tight list-only
                # breadcrumb regions still use the structural sibling check.
                if heading is not None and (heading is container or any(parent is container for parent in heading.parents)):
                    from backend.services.source_governance import _heading_region
                    boundary = _heading_region(heading, container, items)
                    if boundary and not (anchor is boundary or any(parent is boundary for parent in anchor.parents)):
                        continue
            if _pagination_owner(soup, anchor, root, scope) is not root:
                continue
        target = urljoin(current_url, href)
        if target == current_url:
            continue
        # Pagination remains within the current official host. External links
        # are not evidence that the current column has another page.
        from urllib.parse import urlsplit
        if urlsplit(target).hostname != urlsplit(current_url).hostname:
            continue
        candidates.append((0 if 'next' in rel or next_label else 1, target))
    return min(candidates, key=lambda entry: entry[0])[1] if candidates else None


def _probe_selectors(html: str, department: Department) -> tuple | None:
    """Use the scored list detector; never treat the entire page as an article list."""
    from backend.scraper.detectors.list_detector import detect_notice_list
    result = detect_notice_list(html, getattr(department, '_fetch_final_url', department.list_url), existing_profiles=SELECTOR_PROFILES)
    if not result or result.get('confidence', 0) < 0.6:
        return None
    result['name'] = result.get('profile_name') or '官网文章列表'
    return result, True


def _save_probed_selectors(department: Department, profile: dict):
    """Compatibility hook: record a proposal; never install guessed selectors."""
    from backend.services.source_governance import propose_source, source_config
    candidate = dict(source_config(department), **{k: profile[k] for k in
        ('list_selector', 'title_selector', 'link_selector', 'date_selector', 'content_selector') if k in profile})
    return propose_source(department.school_id, candidate, department_id=department.id, origin='probe')


def _process_announcement_item(item, department, school_base_url: str) -> bool:
    """处理单个列表项，返回 True 表示新增了公告"""
    try:
        from backend.scraper.discovery.publication_lists import select_node
        # 提取链接 — 始终以 department.list_url 为基准解析，确保子站点相对路径正确
        link_elem = select_node(item, department.link_selector or 'a[href]')
        link = link_elem.get('href', '') if link_elem else ''
        if not link or link.startswith(('javascript:', 'mailto:', 'tel:')) or (link.startswith('#') and not link.startswith(('#/', '#!/'))):
            return False
        full_url = _resolve_url(getattr(department, '_fetch_final_url', department.list_url), link)

        # 微信公众号外链抓不到正文，跳过以免无效请求拖慢抓取
        if full_url and 'mp.weixin.qq.com' in full_url:
            return False

        # A dated article address supports short real titles; bare institution
        # introduction links must not gain that exception.
        date_text = ''
        if department.date_selector:
            date_elem = select_node(item, department.date_selector)
            date_text = publication_date_text(date_elem)
        from backend.scraper.date_elements import listing_date_observation
        observation = listing_date_observation(date_elem if department.date_selector else None)
        candidate_date = parse_date(observation['raw'])
        published_at = parse_date(date_text)
        from datetime import timedelta, timezone
        if published_at and published_at.date() > datetime.now(timezone(timedelta(hours=8))).date():
            published_at = None  # Verify future list hints in the actual article first.
        title_context_url = full_url if published_at or date_from_url(full_url or '') else ''

        # 提取标题（通用质量门控：配置选择器结果 → 项内回退链；
        # 垃圾标题——纯日期/MORE/导航文本——回退失败则跳过该条，任何学校不入库垃圾）
        title_elem = select_node(item, department.title_selector) if department.title_selector else item
        title_page_url = getattr(department, '_fetch_final_url', department.list_url)
        title = extract_title_text(title_elem, anchor=link_elem, page_url=title_page_url)
        if is_junk_title(title, article_url=title_context_url):
            title = extract_best_title(item, article_url=full_url, page_url=title_page_url)
        if is_junk_title(title, article_url=title_context_url):
            return False

        # One database identity per school and canonical original address. The
        # helper uses an atomic insert and preserves every observed source edge.
        if not full_url:
            return False
        from backend.services.announcement_identity import upsert_listing
        corrections = observed_title_variants(title_elem, title, anchor=link_elem, page_url=title_page_url)
        _announcement, created = upsert_listing(department, title, full_url,
            published_at=published_at, observed_title_variants=corrections)
        from backend.services.publication_dates import apply_listing_date
        apply_listing_date(_announcement, department, observation, candidate_date)
        from backend.services.announcement_sources import record_source
        record_source(_announcement, department, full_url)
        return created

    except Exception as e:
        logger.error(f"处理公告项失败: {e}")
        return False


def scrape_department(department: Department, school_base_url: str,
                      since_year: int | None = None, *, strict_fetch: bool = False) -> tuple:
    """爬取单个部门（支持翻页），返回 (新增数量, 总数)

    如果部门的选择器为空或未匹配到内容，会自动探测常见的 CMS 模式。
    """
    from backend.scraper.discovery.publication_lists import select_node
    from backend.services.collection_settings import since_date, coverage_complete, record_coverage
    from backend.services import tasks
    cutoff = datetime(since_year, 1, 1) if since_year is not None else since_date()
    department._fetch_confirmed_empty = False
    new_count = 0
    total = 0
    page = 1
    consecutive_empty = 0
    consecutive_existing = 0  # 连续已存在通知计数（增量抓取）
    # 增量模式：上次完整抓取过 → 遇到连续已存在通知时提前停止
    incremental_mode = department.last_scraped_at is not None and coverage_complete(department, cutoff)
    old_pages = 0
    processed_this_slice = 0
    if incremental_mode:
        logger.info(f"[{department.name}] 增量模式：上次抓取 {department.last_scraped_at}, "
                    f"连续{INCREMENTAL_THRESHOLD}条已存在即停止")
    probed_profile = None  # 本次探测到的选择器配置
    page_url = department.list_url
    visited_pages = set()
    first_page_html = None  # 第1页 HTML（供选择器健康监督器评估）

    # 获取有效的选择器 — 优先已配置的，否则标记需要探测
    list_sel = (department.list_selector or '').strip()
    title_sel = (department.title_selector or '').strip()
    link_sel = (department.link_selector or '').strip()
    date_sel = (department.date_selector or '').strip()
    content_sel = (department.content_selector or '').strip()

    # 如果关键选择器缺失，标记为需要探测
    need_probe = not list_sel

    # 如果 list_url 为空，无法爬取
    if not (department.list_url or '').strip():
        logger.warning(f"[{department.name}] list_url 为空，无法爬取。请在部门设置中配置通知列表页 URL。")
        return 0, 0

    # 临时保存原始选择器（用于回退）
    _orig_list_sel = department.list_selector
    _orig_title_sel = department.title_selector
    _orig_link_sel = department.link_selector
    _orig_date_sel = department.date_selector
    _orig_content_sel = department.content_selector

    from backend.services.source_collection import collection_progress, save_collection_progress
    from backend.services.source_governance import source_config, _hash, _column_scope
    from backend.database.source_governance_models import SourceValidation
    config = source_config(department)
    config_hash = _hash(config)
    column_scope = None
    for validation in SourceValidation.query.filter_by(department_id=department.id, config_hash=config_hash).order_by(SourceValidation.id.desc()):
        detail = json.loads(validation.validation_json)
        region = detail.get('region') or {}
        if detail.get('status') in ('full', 'limited') and region.get('container') and region.get('heading'):
            column_scope = {'container_selector': region['container'], 'heading_selector': region['heading']}
            break
    scope_hash = _hash([config, column_scope])
    progress_key = f'column:{department.id}:{cutoff:%Y-%m}'
    saved = collection_progress(progress_key)
    if saved:
        new_count, total = saved['new'], saved['total']
        selectors = {name: getattr(department, name) for name in
                     ('list_selector', 'title_selector', 'link_selector', 'date_selector', 'content_selector')}
        trusted_cursor = (saved.get('pagination_scope_version') == PAGINATION_SCOPE_VERSION
                          and saved.get('scope_hash') == scope_hash and saved.get('selectors') == selectors)
        if trusted_cursor:
            page, page_url = saved['page'], saved['page_url']
            visited_pages = set(saved.get('visited', []))
            consecutive_existing = saved.get('consecutive_existing', 0)
            old_pages = saved.get('old_pages', 0)
            incremental_mode = saved.get('incremental_mode', incremental_mode)
            department._fetch_confirmed_empty = saved.get('confirmed_empty', False)
            column_scope = column_scope or saved.get('column_scope')
        else:
            # Old cursors could point at a neighboring widget. Re-read the
            # current entry and rule before ingesting, preserving source/user
            # history, task generation and all model accounting.
            saved['finished'] = False

    def checkpoint_page(next_url, next_page, finished=False):
        save_collection_progress(progress_key, {'new': new_count, 'total': total,
            'page': next_page, 'page_url': next_url, 'visited': sorted(visited_pages),
            'consecutive_existing': consecutive_existing, 'incremental_mode': incremental_mode,
            'old_pages': old_pages,
            'pagination_scope_version': PAGINATION_SCOPE_VERSION,
            'scope_hash': scope_hash, 'column_scope': column_scope,
            'confirmed_empty': department._fetch_confirmed_empty, 'finished': finished,
            'selectors': {name: getattr(department, name) for name in
                ('list_selector', 'title_selector', 'link_selector', 'date_selector', 'content_selector')}})

    if saved and not trusted_cursor:
        checkpoint_page(page_url, page)

    while not saved.get('finished'):
        if page > MAX_TOTAL_PAGES:
            raise RuntimeError('栏目分页超出本轮检查上限，已保存通知；请检查官网分页规则')
        if page_url in visited_pages:
            raise RuntimeError('官网分页重复，已保存通知；需要核对分页规则')
        visited_pages.add(page_url)
        logger.info(f"[{department.name}] 第{page}页 {page_url}")
        db.session.commit()  # Release any list-membership writes before the next network call.

        html = _fetch_html(page_url, raise_fetch_errors=True, purpose='list',
                           source_id=str(department.id), readiness_selector=list_sel)

        page_url = getattr(html, 'final_url', page_url)
        department._fetch_final_url = page_url
        if getattr(getattr(html, 'result', None), 'outcome', '') == 'empty':
            department._fetch_confirmed_empty = True
            checkpoint_page(page_url, page, finished=True)
            break
        soup = BeautifulSoup(html, 'lxml')
        if page == 1:
            first_page_html = html

        # --- 选择器探测：在 page 1 时执行 ---
        if page == 1:
            items = soup.select(list_sel) if list_sel else []

            # Older configurations could name a list after the neighboring
            # featured article. Stop before writing new memberships, keep the
            # channel/user history, and repair only this affected source.
            from backend.scraper.discovery.publication_lists import publication_lists
            from backend.services.source_governance import _normal, _column_label
            selected_ids = {id(node) for node in items}
            source_name = _normal(_column_label(department.name, department.group_name))
            if any(feed.get('heading_article_reference') and selected_ids
                   and any(_normal(heading.get_text(' ', strip=True)) == source_name
                           for heading in soup.select(feed['heading_locator']))
                   and selected_ids == {id(node) for node in soup.select(feed['list_selector'])}
                   for feed in publication_lists(html, page_url)):
                from backend.scraper.selector_monitor import queue_parser_repair, ParserRepairPending
                raise ParserRepairPending(queue_parser_repair(department, html))

            if not items:
                from backend.scraper.selector_monitor import queue_parser_repair, ParserRepairPending
                raise ParserRepairPending(queue_parser_repair(department, html))

            # 🆕 第1页选择器生效时，保存元素签名（用于未来自愈）
            elif items and department.school_id and list_sel and list_sel != 'a[href]':
                try:
                    selectors_for_sig = {
                        'list_selector': list_sel,
                        'title_selector': department.title_selector or '',
                        'link_selector': department.link_selector or '',
                        'date_selector': department.date_selector or '',
                    }
                    save_element_signatures(html, department.school_id, page_url, selectors_for_sig)
                except Exception as e:
                    logger.debug(f"[{department.name}] 签名保存跳过: {e}")
        else:
            items = soup.select(list_sel) if list_sel else []

        if not items:
            from backend.scraper.fetch_errors import SourceAccessError
            raise SourceAccessError('官网返回的页面未识别到通知，不能据此判定为空列表')

        # A later page must still publish this column, even if its old selector
        # happens to match entries in another region after navigation/redirect.
        _, scope_errors, region = _column_scope(config, {'url': page_url}, column_scope, html=html)
        if scope_errors and (column_scope or page > 1):
            from backend.scraper.selector_monitor import queue_parser_repair, ParserRepairPending
            raise ParserRepairPending(queue_parser_repair(department, html))
        if region and not column_scope:
            column_scope = {'container_selector': region['container'], 'heading_selector': region['heading']}

        total += len(items)
        page_new = 0
        page_old = 0

        for item in items:
            # Filter before ingestion; undated entries remain eligible.
            date_text = ''
            if department.date_selector:
                date_elem = select_node(item, department.date_selector)
                date_text = publication_date_text(date_elem)
            pub_date = parse_date(date_text)
            if pub_date and pub_date.replace(tzinfo=None) < cutoff:
                page_old += 1
                continue

            # A rejected title or malformed item is not an existing notice.
            prior = False
            if incremental_mode:
                from backend.services.announcement_identity import article_identity
                link = select_node(item, department.link_selector or 'a[href]')
                _, key = article_identity(_resolve_url(page_url, link.get('href', '')) if link else '')
                prior = bool(key and Announcement.query.filter_by(school_id=department.school_id, url_key=key).first())
            if _process_announcement_item(item, department, school_base_url):
                new_count += 1
                page_new += 1
                consecutive_existing = 0  # 有新通知 → 重置连续计数
            else:
                # 增量模式：计数连续已存在项，达到阈值则停止翻页
                if incremental_mode:
                    consecutive_existing = consecutive_existing + 1 if prior else 0

        if page_new > 0:
            logger.info(f"[{department.name}] P{page}: 新增{page_new}条")

        # 增量模式：连续N条已存在 → 已追平上次抓取进度，停止翻页
        if incremental_mode and consecutive_existing >= INCREMENTAL_THRESHOLD:
            logger.info(f"[{department.name}] P{page}连续{consecutive_existing}条已存在，增量抓取完成，停止翻页")
            checkpoint_page(page_url, page, finished=True)
            break

        # Do not stop on a mixed/pinned page. Confirm two fully dated old pages.
        old_pages = old_pages + 1 if page_old == len(items) else 0
        if old_pages >= 2:
            logger.info(f"[{department.name}] 连续两页早于{cutoff:%Y-%m}，停止翻页")
            checkpoint_page(page_url, page, finished=True)
            break

        # 只有页面真正为空（无列表项）才计数；全去重说明后面可能还有新数据
        if len(items) == 0:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                logger.info(f"[{department.name}] 连续{consecutive_empty}页无内容，停止翻页")
                break
        else:
            consecutive_empty = 0

        try:
            next_page = _next_page_url(html, page_url, page, list_selector=list_sel, scope=column_scope)
        except PaginationScopeError:
            from backend.scraper.selector_monitor import queue_parser_repair, ParserRepairPending
            raise ParserRepairPending(queue_parser_repair(department, html))
        if not next_page:
            checkpoint_page(page_url, page, finished=True)
            break
        checkpoint_page(next_page, page + 1)
        processed_this_slice += 1
        if tasks.current_execution() and processed_this_slice >= MAX_PAGES:
            tasks.defer(capability='http', phase='pagination', delay=1,
                        reason=f'已检查 {page} 页，继续抓取历史通知')
        page_url = next_page

        page += 1

    logger.info(f"[{department.name}] 完成: {page}页/{total}条, 新增{new_count}条")

    # --- 更新上次抓取时间（用于下次增量抓取）---
    if total > 0 or department._fetch_confirmed_empty:
        department.last_scraped_at = datetime.utcnow()
        record_coverage(department, cutoff)
    db.session.commit()

    # --- 探测后处理：保存成功的探测结果，或恢复原始选择器 ---
    if probed_profile:
        _save_probed_selectors(department, probed_profile)
    else:
        # 未探测或探测失败，恢复原始选择器
        department.list_selector = _orig_list_sel
        department.title_selector = _orig_title_sel
        department.link_selector = _orig_link_sel
        department.date_selector = _orig_date_sel
        department.content_selector = _orig_content_sel

    # --- 选择器健康监督（质量触发闭环：结构坏→自动修；抓错→标人工复核） ---
    if first_page_html:
        try:
            from backend.scraper.selector_monitor import evaluate_and_repair
            outcome = evaluate_and_repair(department, first_page_html)
            if outcome.get('action') not in (None, 'skip', 'healthy'):
                logger.info(f"[{department.name}] 选择器监督: {outcome}")
        except Exception as e:
            logger.debug(f"选择器监督异常 [{department.name}]: {e}")

    return new_count, total


def scrape_school(school: School, since_year: int | None = None,
                  progress_session=None) -> ScrapeLog:
    """爬取一个学校的所有部门

    如果学校没有任何部门（或只有自动创建的默认"通知公告"），
    会先运行智能站点发现来识别部门结构和子部门。

    Args:
        school: 学校 ORM 对象
        since_year: 只抓取该年及以后的通知
        progress_session: 可选的 ScrapeSession 实例，用于 SSE 实时进度推送
    """
    from backend.scraper.discovery.site_discovery import discover_school_departments, apply_discovered_departments

    log = ScrapeLog(school_id=school.id, source_name='全校订阅范围', status='running')
    db.session.add(log)
    db.session.commit()

    total_new = 0
    total_all = 0

    try:
        depts = school.departments.all()

        # 智能站点发现：无部门 或 仅有一个未配置选择器的默认部门
        is_bare = (
            not depts or
            (len(depts) == 1 and
             depts[0].name == '通知公告' and
             not (depts[0].list_selector or '').strip())
        )

        if is_bare and school.url and school.url.strip():
            logger.info(f"[{school.name}] 部门为空/仅有默认，启动智能站点发现...")
            try:
                if os.environ.get('WATCHER_DEEP_DISCOVERY') == '1':
                    configs = discover_school_departments(school.url)
                else:
                    from backend.scraper.discovery.lightweight import discover_columns
                    configs = discover_columns(school.url)
                if configs:
                    # Preserve existing source IDs and readers' column subscriptions.

                    created = apply_discovered_departments(school.id, configs)
                    logger.info(f"[{school.name}] 站点发现完成：创建了 {created} 个部门")
                    depts = school.departments.all()
                else:
                    # 发现失败 → 创建默认部门作为回退
                    logger.info(f"[{school.name}] 站点发现未找到部门，使用默认部门")
                    if not depts:
                        dept = Department(
                            school_id=school.id,
                            name='通知公告',
                            list_url=school.url,
                            list_selector='',
                            title_selector='',
                            link_selector='',
                            date_selector='',
                            content_selector='',
                        )
                        db.session.add(dept)
                        db.session.commit()
                        depts = [dept]
            except Exception as e:
                logger.warning(f"[{school.name}] 站点发现失败: {e}，回退到默认部门")
                if not depts:
                    dept = Department(
                        school_id=school.id,
                        name='通知公告',
                        list_url=school.url,
                        list_selector='',
                        title_selector='',
                        link_selector='',
                        date_selector='',
                        content_selector='',
                    )
                    db.session.add(dept)
                    db.session.commit()
                    depts = [dept]
        elif not depts:
            # 无 URL 的新学校
            logger.info(f"[{school.name}] 没有部门且 URL 为空，创建默认部门")
            dept = Department(
                school_id=school.id,
                name='通知公告',
                list_url=school.url if school.url else '',
                list_selector='',
                title_selector='',
                link_selector='',
                date_selector='',
                content_selector='',
            )
            db.session.add(dept)
            db.session.commit()
            depts = [dept]

        # DAILY NEWS was a synthetic date filter, never an official source.
        depts = [d for d in depts if d.name.upper() != 'DAILY NEWS']
        # Fetch only the union of subscribed columns; an all-column subscription includes new sources.
        from backend.database.models import Subscription
        from backend.services.directory_options import directory_entries_for, expand_directory_ids
        entries = directory_entries_for(school.id)
        directory_ids = {e.parent_id for e in entries}
        depts = [d for d in depts if d.id not in directory_ids and d.list_url and d.kind not in ('unit','group')]
        subs = Subscription.query.filter_by(school_id=school.id).all()
        if subs and all(sub.department_ids is not None for sub in subs):
            wanted = expand_directory_ids(school.id, {dept_id for sub in subs for dept_id in sub.department_ids}, entries)
            depts = [d for d in depts if d.id in wanted]

        # 通知进度会话：部门列表已确定
        if progress_session:
            progress_session.set_departments(depts)

        failures = 0
        for i, dept in enumerate(depts):
            new = 0
            total = 0
            if progress_session:
                progress_session.dept_start(dept.name, i + 1)
            try:
                new, total = scrape_department(dept, school.url, since_year)
                total_new += new
                total_all += total
            except Exception as e:
                failures += 1
                logger.error(f"[{school.name}] 爬取部门 [{dept.name}] 失败: {e}", exc_info=True)
            finally:
                if progress_session:
                    progress_session.dept_done(dept.name, new, total)

        # 后处理：清理自动发现中的噪声子部门（0 通知 + 回退选择器 → 非通知列表页）
        # Keep original source columns even when temporarily empty; readers may subscribe to them.
        db.session.commit()

        if not total_all or failures:
            log.status = 'failed'
            log.error_message = '未获取到可用通知，请检查官网栏目地址' if not total_all else f'{failures} 个栏目同步失败，其余栏目已保存'
        else:
            log.status = 'success'
    except Exception as e:
        logger.error(f"爬取学校 [{school.name}] 失败: {e}")
        log.status = 'failed'
        log.error_message = str(e)

    log.new_count = total_new
    log.total_count = total_all
    log.finished_at = datetime.now(timezone.utc)
    db.session.commit()
    return log


def active_schools_query():
    """订阅驱动抓取：上架且有人订阅的学校才进入抓取范围"""
    return School.query.filter(School.enabled.is_(True),
                               School.subscriber_count > 0)


def scrape_all_schools(since_year: int | None = None) -> list:
    """爬取所有活跃学校（上架且有订阅）"""
    schools = active_schools_query().all()
    results = []
    for school in schools:
        logger.info(f"开始爬取: {school.name}")
        log = scrape_school(school, since_year)
        results.append(log)
    return results


def manual_scrape(school_id: int, since_year: int | None = None) -> ScrapeLog:
    """手动触发单个学校的爬取"""
    school = db.session.get(School, school_id)
    if not school:
        raise ValueError(f"学校不存在: {school_id}")
    return scrape_school(school, since_year)
