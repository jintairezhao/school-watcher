"""Judge page content before parsing; transport success is not content success."""
from dataclasses import replace
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..auth_routes import LOGIN_REQUIRED_MESSAGE, is_sign_in_route
from .contracts import FetchRequest, FetchResult

_AUTO_CHALLENGE = ('$_ts', '/cdn-cgi/challenge-platform/', 'cf-chl-',
                   'checking your browser', 'just a moment...', '正在检查您的浏览器')
_MANUAL_CHALLENGE = ('verify you are human', '请完成安全验证', '请完成下方验证',
                     '请输入验证码', '滑动滑块', '人机身份验证')
# How a page names itself as an identity provider. Script-rendered login forms
# ship no password field to detect, so the wording is what identifies the page.
_IDENTITY_PAGE = re.compile(
    r'统一身份|统一认证|统一登录|单点登录|身份认证|single\s*sign[-\s]?on|identity\s+provider'
    r'|usernamepassword|webauthn|\bsso\b', re.I)
# Where a page must say it to be *naming itself* rather than merely mentioning the
# provider: its title, or its opening words. A size limit stood here first, and it
# refused the provider's own page -- the real one carries over 13000 characters of
# visible text -- so the notice reached the reader as a rule problem instead of an
# access limit. What separates the provider's page from a page that merely mentions
# one is that it names itself, not that it is short.
_IDENTITY_OPENING = 200
_EMPTY = re.compile(r'^(?:暂无(?:通知|公告|新闻|内容|信息|数据|记录)|没有(?:相关)?(?:通知|公告|记录|数据)|'
                    r'no (?:results|records|notices|data)(?: found)?)[。.!！\s]*$', re.I)
_DATE = re.compile(r'(?:20\d{2}[-年./]\d{1,2}[-月./]\d{1,2}|\d{1,2}[-/]\d{1,2})')
_ARTICLE = 'article, .v_news_content, #vsb_content, #vsb_content_2, .wp_articlecontent, .article-content, .article_content, .TRS_Editor'


def _decision(raw, outcome, code='', message='', evidence=()):
    return replace(raw, outcome=outcome, error_code=code, message=message,
                   evidence=tuple(raw.evidence) + tuple(evidence))


def _visible(node):
    for current in (node, *node.parents):
        if not hasattr(current, 'get'):
            continue
        style = re.sub(r'\s+', '', current.get('style', '')).lower()
        if current.has_attr('hidden') or current.get('aria-hidden') == 'true' or 'display:none' in style or 'visibility:hidden' in style:
            return False
    return True


def _login_wall(raw, soup, visible, title):
    """True when we landed on an identity provider instead of the content asked for.

    This is an access limit the site itself imposed, not content the program
    failed to recognise: the notice exists and is listed publicly, but reading it
    requires a university account. Every clause is a property of the document
    itself, and all of them must agree, because a wrong answer here would refuse a
    column the public can read:

    * the document *is* the sign-in route, carrying a hand-off that names the
      address to return to -- not merely an address containing an authentication
      word, since real notice lists sit behind portal hops and real publication
      paths are named /news/login.html. The same predicates judge the redirect
      hop in the HTTP client, so both channels agree;
    * it *names itself* as an identity provider, in its title or its opening
      words. A password field is not required: this provider renders its form
      with script and ships none. Self-naming is what separates the provider's
      own page from the pages that merely mention it -- a real notice page
      carries a "统一身份认证" link in its header, and a portal homepage carries
      the words in its navigation. Both are content the public can read.
    * it carries no readable article region, so this is not content we can read.
    """
    if not is_sign_in_route(raw.final_url or ''):
        return False
    if not (_IDENTITY_PAGE.search(title) or _IDENTITY_PAGE.search(visible[:_IDENTITY_OPENING])):
        return False
    return not any(len(node.get_text(' ', strip=True)) >= 8 or node.select_one('img[src]')
                   for node in soup.select(_ARTICLE))


def classify_result(request: FetchRequest, raw: FetchResult) -> FetchResult:
    """Reclassify raw DOM after HTTP, browser rendering, or manual verification."""
    headers = {str(k).lower(): str(v).lower() for k, v in raw.headers.items()}
    html = raw.html or ''
    lower = html[:300000].lower()
    soup = BeautifulSoup(html, 'lxml')
    for node in soup.select('script, style, template, noscript'):
        node.decompose()
    visible = soup.get_text(' ', strip=True)
    title = soup.title.get_text(' ', strip=True).lower() if soup.title else ''
    automatic = (headers.get('cf-mitigated') == 'challenge' or
                 any(marker in lower for marker in _AUTO_CHALLENGE))
    manual = any(marker in visible.lower() for marker in _MANUAL_CHALLENGE)
    # Embedded reCAPTCHA on an ordinary contact page is not an access challenge.
    if manual and (automatic or len(visible) < 1200 or raw.status in (403, 412)):
        return _decision(raw, 'needs_manual', 'human_verification',
                         '官网需要人工完成访问验证；已有通知仍保留', ('manual_challenge',))
    if automatic:
        rendered = raw.transport == 'browser'
        return _decision(raw, 'needs_manual' if rendered else 'requires_render',
                         'access_challenge', '官网当前返回访问校验页面，需要浏览器或管理员验证',
                         ('automatic_challenge',))
    if raw.status >= 400:
        from backend.scraper.fetch_errors import http_failure
        failure = http_failure(raw.status)
        return _decision(raw, 'network_error' if failure.retryable else 'denied',
                         'http_' + str(raw.status), str(failure))
    if raw.outcome in ('unavailable', 'busy', 'network_error', 'denied') and raw.error_code:
        return raw
    if not html.strip():
        return _decision(raw, 'needs_adapter', 'empty_response',
                         '官网未返回可读取的网页内容；已有通知仍保留')
    if title in ('access denied', '403 forbidden', 'forbidden', 'error', '访问被拒绝'):
        return _decision(raw, 'denied', 'access_denied_page', '官网拒绝本次访问；已有通知仍保留')
    # Checked before any content rule: a login page can contain an article-shaped
    # region or a script shell, and misreading it as unparsed content would send
    # the user to fix parsers for a page that is simply not public.
    if _login_wall(raw, soup, visible, title):
        return _decision(raw, 'denied', 'source_login_required', LOGIN_REQUIRED_MESSAGE)
    fragment = urlsplit(request.url).fragment
    if raw.transport != 'browser' and fragment.startswith(('/', '!/')):
        return _decision(raw, 'requires_render', 'fragment_route',
                         '该官网路由需要浏览器渲染', ('fragment_route_requires_browser',))
    empty_evidence = ()
    empty_selector = request.policy.get('empty_selector', '')
    if empty_selector:
        try:
            nodes = soup.select(empty_selector)
        except Exception:
            return _decision(raw, 'needs_adapter', 'invalid_empty_selector', '来源的空列表规则需要核对')
        if any(node.get_text(' ', strip=True) and _visible(node) for node in nodes):
            empty_evidence = ('verified_empty_selector',)
    if request.purpose != 'article' and any(_EMPTY.fullmatch(str(text).strip()) and _visible(text.parent)
            for text in soup.find_all(string=True)):
        empty_evidence = empty_evidence or ('explicit_empty_message',)
    from backend.scraper.article_resources import has_public_body_resource
    if request.readiness_selector:
        try:
            ready = soup.select(request.readiness_selector)
        except Exception:
            return _decision(raw, 'needs_adapter', 'invalid_readiness_selector', '来源的内容识别规则需要核对')
        if any(_visible(node) and (node.get_text(' ', strip=True) or node.select_one('img[src], a[href]')
                or request.purpose == 'article' and has_public_body_resource([node], raw.final_url)) for node in ready):
            return _decision(raw, 'usable', evidence=('readiness_selector',))
        if empty_evidence:
            return _decision(raw, 'empty', evidence=empty_evidence)
        # A configured region is a contract, not a hint. A static news widget
        # must not satisfy a request for a script-injected directory or list.
        if raw.transport != 'browser' and '<script' in lower:
            return _decision(raw, 'requires_render', 'readiness_missing',
                             '目标内容尚未出现，需要浏览器渲染', ('readiness_selector_missing',))
        return _decision(raw, 'needs_adapter', 'readiness_missing', '未找到配置要求的目标内容，需要核对来源规则')
    if request.purpose == 'article':
        if any(len(node.get_text(' ', strip=True)) >= 8 or node.select_one('img[src]')
               or has_public_body_resource([node], raw.final_url) for node in soup.select(_ARTICLE)):
            return _decision(raw, 'usable', evidence=('article_region',))
    elif request.purpose == 'directory':
        anchors = [a for a in soup.select('a[href]') if a.get_text(' ', strip=True)
                   and not a.get('href', '').lower().startswith(('javascript:', 'mailto:', 'tel:'))]
        if not empty_evidence and (anchors or len(visible) >= 60):
            return _decision(raw, 'usable', evidence=('visible_directory_content',))
    else:
        # List extraction remains the parser's job. Require a dated, descriptive
        # link or a recognizable article address, not a navigation-only header.
        for anchor in soup.select('a[href]'):
            label = anchor.get_text(' ', strip=True) or anchor.get('title', '')
            href = anchor.get('href', '')
            surrounding = anchor.parent.get_text(' ', strip=True) if anchor.parent else label
            if len(label) >= 4 and (_DATE.search(surrounding) or
                    re.search(r'/(?:info|article|news|content)/|(?:newsDetail|ArticleID)[?=/]', href, re.I)):
                return _decision(raw, 'usable', evidence=('publication_link',))
    if empty_evidence:
        return _decision(raw, 'empty', evidence=empty_evidence)
    scripts = '<script' in lower
    js_shell = scripts and (not visible or len(visible) < 120 or
                           bool(re.search(r'id\s*=\s*[\"\'](?:app|root|__next)[\"\']', lower)))
    if js_shell and raw.transport != 'browser':
        return _decision(raw, 'requires_render', 'javascript_shell', '官网内容需要 JavaScript 渲染', ('javascript_shell',))
    return _decision(raw, 'needs_adapter', 'content_not_recognized',
                     '已读取官网，但尚未识别到所需内容，需要核对来源规则；已有通知仍保留')
