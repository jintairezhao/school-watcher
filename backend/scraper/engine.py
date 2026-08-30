"""通用爬取引擎 — 支持翻页、增量抓取、智能变更检测、选择器探测"""

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from curl_cffi import requests
from bs4 import BeautifulSoup

from backend.database.db import db
from backend.database.models import School, Department, Announcement, ScrapeLog
from backend.scraper.sanitizer import sanitize_html
from backend.scraper.change_detector import compute_hash, detect_update, parse_date
from backend.scraper.detectors.title_quality import (
    is_junk_title, extract_best_title, date_from_url, clean_title)
from backend.scraper.selector.selector_store import save_element_signatures, auto_heal_selectors
from backend.scraper.cms_registry import load_selector_profiles

logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}

REQUEST_TIMEOUT = 15
MAX_PAGES = 50  # 安全上限
SINCE_YEAR = 2024  # 只抓取2024年至今的通知
INCREMENTAL_THRESHOLD = 3  # 连续N条已存在通知时停止翻页（增量抓取）

# ---- 选择器探测：常见中国高校 CMS 模式 ----
# 模板定义在 cms_profiles.yaml（数据驱动），新增 CMS 无需改此文件
SELECTOR_PROFILES = load_selector_profiles()


def _fetch_html(url: str, follow_js_redirect: bool = True,
                allow_browser_fallback: bool = True,
                _redirect_chain: set | None = None) -> str:
    """获取页面 HTML，支持 JS 重定向检测和浏览器回退。

    当页面仅包含 <script>window.location.href='...'</script> 时，
    自动提取重定向目标并跟踪（最多 3 次跳转，且不重复访问同一URL）。

    当 allow_browser_fallback=True 且 Playwright 可用时，
    若静态 HTML 疑似需要 JS 渲染（SPA 骨架、反爬质询等），
    自动用 Chromium headless 重新获取渲染后的 HTML。
    """
    html = ''
    curl_cffi_failed = False

    # --- Phase 1: curl_cffi 请求 ---
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, impersonate='chrome')
        resp.raise_for_status()
        # curl_cffi doesn't have apparent_encoding; use charset_encoding fallback
        try:
            resp.encoding = resp.apparent_encoding or resp.charset_encoding or 'utf-8'
        except AttributeError:
            resp.encoding = resp.charset_encoding or resp.encoding or 'utf-8'
        html = resp.text
        # 瑞数等 WAF：202 挑战或 $_ts JS 壳页视为失败，交给浏览器回退
        if resp.status_code == 202 or '$_ts' in html[:50000]:
            curl_cffi_failed = True
            html = ''
    except Exception as e:
        curl_cffi_failed = True
        logger.debug(f"curl_cffi 请求失败: {url} - {e}")
        html = ''

    # --- Phase 2: 浏览器回退（curl_cffi 完全失败时） ---
    if curl_cffi_failed and allow_browser_fallback:
        try:
            from backend.scraper.fetchers.playwright_fetcher import (
                fetch_html_with_browser,
                is_playwright_available,
            )
        except ImportError:
            pass  # Playwright 未安装，跳过
        else:
            if is_playwright_available():
                logger.info(f"curl_cffi 请求失败，尝试 Playwright: {url}")
                browser_html = fetch_html_with_browser(url)
                if browser_html:
                    html = browser_html
                    curl_cffi_failed = False
                    logger.info(f"Playwright 成功获取: {url} ({len(html)} bytes)")
                else:
                    logger.warning(f"Playwright 也失败: {url}")

    # 如果 curl_cffi 失败且浏览器回退也失败/不可用，返回空字符串
    if curl_cffi_failed and not html:
        return ''

    # --- Phase 3: JS 重定向检测 ---
    if follow_js_redirect:
        if _redirect_chain is None:
            _redirect_chain = set()
        _redirect_chain.add(url.rstrip('/'))  # 标准化当前URL
        redirects_followed = 0
        while redirects_followed < 3:
            # 先移除 IE 条件注释（<!--[if ...]>...<![endif]-->），避免误匹配其中的 JS 重定向
            html_stripped = re.sub(
                r'<!--\[if\s[^\]]*\]>.*?<!\[endif\]-->',
                '', html, flags=re.IGNORECASE | re.DOTALL
            )
            # 检测 JS 重定向: window.location(.href)? = 'url' 或 "url"
            js_redirect = re.search(
                r'(?:window\.)?location(?:\.href)?\s*=\s*[\'"]([^\'"]+)[\'"]',
                html_stripped, re.IGNORECASE
            )
            if not js_redirect:
                # 也检查 meta refresh
                meta_redirect = re.search(
                    r'<meta\s+http-equiv\s*=\s*["\']refresh["\']\s+content\s*=\s*["\']?\d+\s*;\s*url\s*=\s*([^\s"\'>]+)',
                    html, re.IGNORECASE
                )
                if not meta_redirect:
                    break
                target = meta_redirect.group(1)
            else:
                target = js_redirect.group(1)

            if not target or target.startswith('javascript:'):
                break

            # 解析相对 URL
            target_url = urljoin(url, target)

            # 防止循环重定向：目标URL已访问过 → 停止
            normalized_target = target_url.rstrip('/')
            if normalized_target in _redirect_chain:
                logger.debug(f"检测到循环重定向，停止: {url} → {target_url}")
                break

            logger.info(f"JS/HTML 重定向: {url} → {target_url}")

            # 递归调用自己（传递 redirect_chain 防止循环，不启用浏览器回退）
            html = _fetch_html(target_url, follow_js_redirect=True,
                               allow_browser_fallback=False,
                               _redirect_chain=_redirect_chain)
            url = target_url  # 更新 URL 以便下一轮相对链接解析
            redirects_followed += 1

    # --- Phase 4: JS 必要性检测（静态HTML不完整时用浏览器重试） ---
    if allow_browser_fallback and html:
        try:
            from backend.scraper.fetchers.playwright_fetcher import (
                fetch_html_with_browser,
                is_playwright_available,
                is_js_required,
            )
        except ImportError:
            pass  # Playwright 未安装
        else:
            if is_playwright_available() and is_js_required(html):
                logger.info(f"检测到页面需要 JS 渲染，切换 Playwright: {url}")
                try:
                    browser_html = fetch_html_with_browser(url)
                    if browser_html:
                        logger.info(
                            f"Playwright 获取成功: {url} "
                            f"(静态 {len(html)}→渲染 {len(browser_html)} bytes)"
                        )
                        html = browser_html
                except Exception as be:
                    logger.warning(f"Playwright 回退失败: {be}，使用静态 HTML")

    return html


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


def _get_page_url(base_url: str, page: int, style: str = 'default') -> str:
    """ZCMS分页: 第1页=base_url, 第N页=base_url/index_N.shtml

    支持多种翻页格式:
    - default: 第1页=base_url, 第2+页=base_url/index_N.shtml
    - catalog: ZCMS catalog端点 (/zcms/catalog/), 所有页都用 index_N.shtml
    - index_htm: 第2+页=base_url/index_N.htm
    - query_page: 第2+页=base_url?page=N
    - query_page_index: 第2+页=base_url/index.htm?page=N
    - index_htm_offset: 零偏移翻页，第2页=index1.htm, 第N页=index{N-1}.htm
    """
    if style == 'catalog' or '/zcms/catalog/' in base_url:
        base = base_url.rstrip('/')
        base = re.sub(r'/index_\d+\.shtml$', '', base)
        return f"{base}/index_{page}.shtml"

    if page <= 1:
        return base_url

    base = base_url.rstrip('/')

    if style == 'index_htm':
        # 若base_url以/index.htm结尾，需剥除文件名再拼接
        base = re.sub(r'/index\.htm$', '', base)
        return f"{base}/index_{page}.htm"
    elif style == 'index_html':
        # 上交 ETUI 等自建站：index_N.html
        base = re.sub(r'/index\.html$', '', base)
        return f"{base}/index_{page}.html"
    elif style == 'index_htm_offset':
        # 零偏移: 第2页=index1.htm, 第N页=index{N-1}.htm
        base = re.sub(r'/index\.htm$', '', base)
        return f"{base}/index{page - 1}.htm"
    elif style == 'query_page':
        sep = '&' if '?' in base_url else '?'
        return f"{base_url}{sep}page={page}"
    elif style == 'query_page_index':
        return f"{base}/index.htm?page={page}"
    else:  # default
        return f"{base}/index_{page}.shtml"


# 翻页格式降级尝试顺序（第2页 404 时依次尝试）
PAGINATION_FALLBACKS = [
    ('index_htm', '/index_2.htm'),
    ('index_html', '/index_2.html'),  # 须在 query_* 之前，避免 ?page= 假阳性
    ('query_page_index', '/index.htm?page=2'),
    ('query_page', '?page=2'),
    ('index_htm_offset', '/index1.htm'),
]


def _detect_pagination_style(department_list_url: str) -> str:
    """探测正确的翻页格式。

    尝试访问第2页的各种 URL 格式，返回第一个成功的格式名称。
    如果所有格式都失败，返回 None（放弃翻页）。
    """
    for style_name, _suffix in PAGINATION_FALLBACKS:
        test_url = _get_page_url(department_list_url, 2, style_name)
        try:
            resp = requests.head(test_url, headers=HEADERS, timeout=REQUEST_TIMEOUT, impersonate='chrome')
            if resp.status_code == 200:
                logger.info(f"[翻页探测] {style_name} 格式有效: {test_url}")
                return style_name
        except Exception:
            pass
        # HEAD 可能不被支持，尝试 GET 但只取前几个字节
        try:
            resp = requests.get(test_url, headers=HEADERS, timeout=REQUEST_TIMEOUT, stream=True, impersonate='chrome')
            if resp.status_code == 200:
                # 读取少量内容确认不是错误页
                chunk = next(resp.iter_content(1024), None)
                resp.close()
                if chunk and len(chunk) > 100:
                    logger.info(f"[翻页探测] {style_name} 格式有效: {test_url}")
                    return style_name
        except Exception:
            continue

    return None  # 所有格式都失败


def _extract_pagination_style_from_html(html: str, base_url: str) -> str:
    """从第1页 HTML 中提取翻页链接格式，避免第2页 404 浪费请求。

    解析页面中的翻页链接（如「下一页」「2」等），推断翻页 URL 格式。
    返回格式名称（index_htm / query_page / query_page_index / default），
    无法确定时返回 None（走原有 fallback 流程）。

    支持的翻页链接模式:
    - href="index_2.htm"           → index_htm
    - href="?page=2"               → query_page
    - href="index.htm?page=2"      → query_page_index
    - href="index_2.shtml"         → default (标准 ZCMS)
    """
    soup = BeautifulSoup(html, 'lxml')
    base = base_url.rstrip('/')

    # 收集所有翻页候选链接
    pagination_hrefs = []

    # 1. 查找翻页区域的链接（常见 class/id）
    page_nav = soup.select(
        '.page, .pagination, .pager, .pagelist, .page-list, .pages, '
        '.gp-page, .zcms_page, div[class*="page"], div[class*="pagin"], '
        'div[class*="pager"], div[id*="page"], div[id*="pagin"]'
    )
    search_roots = page_nav if page_nav else [soup]

    for root in search_roots:
        for a in root.select('a[href]'):
            href = (a.get('href') or '').strip()
            if not href or href.startswith('javascript:') or href.startswith('#'):
                continue
            # 收集翻页链接：含 index_N、indexN、page=N 等模式
            # 跳过外部链接（误匹配友情链接等）
            if (href.startswith('http://') or href.startswith('https://')) and \
               not href.startswith(base) and not href.startswith(base.replace('http://', 'https://')):
                continue
            if re.search(r'(index[_\d]*\d+\.(?:htm|shtml)|page=\d+|p=\d+|pn=\d+)', href, re.IGNORECASE):
                pagination_hrefs.append(href)

    if not pagination_hrefs:
        # 2. 回退：全页搜索带 page= 参数的链接
        for a in soup.select('a[href*="page="]'):
            href = (a.get('href') or '').strip()
            if re.search(r'page=\d+', href):
                pagination_hrefs.append(href)

    if not pagination_hrefs:
        return None  # 无法从 HTML 中推断

    # 分析链接格式（按特异性从高到低）
    for href in pagination_hrefs:
        # 模式: index.htm?page=N
        if re.search(r'index\.htm\?page=\d+', href, re.IGNORECASE):
            logger.info(f"[翻页预判] 从HTML链接 {href} 推断格式: query_page_index")
            return 'query_page_index'

        # 模式: ?page=N 或 &page=N
        if re.search(r'[?&]page=\d+', href):
            logger.info(f"[翻页预判] 从HTML链接 {href} 推断格式: query_page")
            return 'query_page'

        # 模式: index_N.htm (如 index_2.htm, index_66.htm)
        if re.search(r'index_\d+\.htm', href, re.IGNORECASE):
            logger.info(f"[翻页预判] 从HTML链接 {href} 推断格式: index_htm")
            return 'index_htm'

        # 模式: index[N].htm 零偏移翻页 (如 index1.htm=第2页, index2.htm=第3页)
        # 排除 index.htm（第1页本身），只匹配 index1, index2, ... index66 等
        if re.search(r'index\d+\.htm', href, re.IGNORECASE):
            logger.info(f"[翻页预判] 从HTML链接 {href} 推断格式: index_htm_offset（零偏移）")
            return 'index_htm_offset'

        # 模式: index_N.shtml (标准 ZCMS)
        if re.search(r'index_\d+\.shtml', href, re.IGNORECASE):
            logger.info(f"[翻页预判] 从HTML链接 {href} 确认标准ZCMS翻页格式")
            return 'default'

    return None


def _probe_selectors(html: str, department: Department) -> tuple:
    """探测页面适用的 CSS 选择器配置。

    当部门未配置选择器（或配置不匹配）时，依次尝试常见的高校 CMS 模式。
    返回 (profile_dict, is_permanent) 元组：
    - profile_dict: 匹配到的选择器配置，None 表示未匹配
    - is_permanent: True 表示可永久保存（精确匹配），False 表示临时回退（每次重新探测）
    """
    soup = BeautifulSoup(html, 'lxml')

    for profile in SELECTOR_PROFILES:
        try:
            items = soup.select(profile['list_selector'])
            if items and len(items) >= 3:
                logger.info(
                    f"[{department.name}] 探测到匹配模式: {profile['name']} "
                    f"(list_selector={profile['list_selector']}, 共{len(items)}项)"
                )
                return profile, True
        except Exception:
            continue

    # 回退：尝试只用 "a" 标签在页面主体中寻找链接列表
    # 这种模式不精确，不应永久保存，每次抓取时重新探测
    try:
        # 排除导航区域
        for tag in soup.select('nav, header, footer, .nav, .header, .footer, .menu'):
            tag.decompose()
        links = soup.select('a[href]')
        # 过滤出有实际文本内容的链接
        valid_links = [a for a in links if a.get_text(strip=True) and len(a.get_text(strip=True)) >= 4]
        if len(valid_links) >= 5:
            logger.info(f"[{department.name}] 回退模式: 页面中共找到 {len(valid_links)} 个有效链接（临时，不会保存）")
            return {
                'name': '回退 (全页链接)',
                'list_selector': 'a[href]',
                'title_selector': None,   # 使用元素自身文本
                'link_selector': None,    # 使用元素自身 href
                'date_selector': None,
                'content_selector': None,
            }, False
    except Exception:
        pass

    return None  # 无匹配（独立 None 确保 if probed: 正确判断）


def _save_probed_selectors(department: Department, profile: dict):
    """将探测到的选择器配置保存到部门记录，后续抓取直接使用"""
    department.list_selector = profile.get('list_selector', '')
    department.title_selector = profile.get('title_selector', '')
    department.link_selector = profile.get('link_selector', '')
    department.date_selector = profile.get('date_selector', '')
    department.content_selector = profile.get('content_selector', '')
    db.session.commit()
    logger.info(f"[{department.name}] 选择器已自动保存: {profile.get('name')}")


def _dept_url_affinity(department, url: str) -> bool:
    """文章 URL 是否落在部门站点路径下（如 /zhb/c/... 属于 list_url=/zhb/ 的部门）。

    用于归属优先：同站路径的文章优先归该站部门，而非交叉列出的其他部门。
    """
    from urllib.parse import urlparse
    list_url = department.list_url or ''
    if not list_url or '://' not in list_url:
        return False
    a, b = urlparse(list_url), urlparse(url)
    if a.netloc != b.netloc:
        return False
    base = a.path.rstrip('/')
    return bool(base) and (b.path == base or b.path.startswith(base + '/'))


def _process_announcement_item(item, department, school_base_url: str) -> bool:
    """处理单个列表项，返回 True 表示新增了公告"""
    try:
        # 提取链接 — 始终以 department.list_url 为基准解析，确保子站点相对路径正确
        link_elem = item.select_one(department.link_selector) if department.link_selector else item
        link = link_elem.get('href', '') if link_elem else ''
        full_url = _resolve_url(department.list_url, link)

        # 微信公众号外链抓不到正文，跳过以免无效请求拖慢抓取
        if full_url and 'mp.weixin.qq.com' in full_url:
            return False

        # 提取标题（通用质量门控：配置选择器结果 → 项内回退链；
        # 垃圾标题——纯日期/MORE/导航文本——回退失败则跳过该条，任何学校不入库垃圾）
        title_elem = item.select_one(department.title_selector) if department.title_selector else item
        title = ''
        if title_elem:
            # 苏迪等 CMS 标题在 a[title]/data-title 属性里
            attr_title = (title_elem.get('title') or title_elem.get('data-title') or '').strip()
            if attr_title:
                title = attr_title
            else:
                title = title_elem.get_text(strip=True)
        title = clean_title(title)  # 剥离前缀日期/浏览数噪声
        if is_junk_title(title):
            title = extract_best_title(item)
        if not title or len(title) < 2:
            return False

        # 提取日期
        date_text = ''
        if department.date_selector:
            date_elem = item.select_one(department.date_selector)
            if date_elem:
                date_text = date_elem.get_text(strip=True)
        published_at = parse_date(date_text)
        if not published_at and full_url:
            published_at = date_from_url(full_url)  # 日期节点缺失时从文章 URL 推断

        # URL去重（归属优先：同一文章被多个部门列出时——
        # 1) 优先归属非聚合的具体部门（否则文章都被校区首页聚合页先抢走）；
        # 2) 站点归属优先：文章 URL 路径属于哪个部门的站点（如 /zhb/ → 综合办公室），
        #    就归哪个部门，避免单位镜头被交叉列出的其他部门饿死）
        if full_url:
            existing, updated = detect_update(full_url, title, '')
            if existing:
                if existing.school_id == department.school_id and existing.department_id != department.id:
                    old_dept = existing.department
                    school_root = (department.school.url or '').strip().rstrip('/')
                    old_is_agg = old_dept is not None and \
                        (old_dept.list_url or '').strip().rstrip('/') == school_root
                    new_is_agg = (department.list_url or '').strip().rstrip('/') == school_root
                    old_aff = old_dept is not None and _dept_url_affinity(old_dept, full_url)
                    new_aff = _dept_url_affinity(department, full_url)
                    if (old_is_agg and not new_is_agg) or (not old_aff and new_aff):
                        existing.department_id = department.id
                if updated:
                    logger.info(f"检测到更新: {title}")
                return False

        # 获取正文
        content_html = ''
        content_text = ''
        attachment_links = []
        try:
            if full_url and department.content_selector:
                detail_html = _fetch_html(full_url)
                detail_soup = BeautifulSoup(detail_html, 'lxml')
                content_elem = detail_soup.select_one(department.content_selector)
                if content_elem:
                    for tag in content_elem.find_all(['a', 'img']):
                        for attr in ['href', 'src']:
                            val = tag.get(attr)
                            if val and not val.startswith(('http://', 'https://', '//', '#', 'javascript:', 'mailto:', 'data:')):
                                tag[attr] = _resolve_url(full_url, val)
                    content_html = sanitize_html(str(content_elem))
                    content_text = _extract_text(str(content_elem))
                    attachment_links = _extract_attachment_links(content_elem)
        except Exception as e:
            logger.warning(f"获取正文失败 [{title}]: {e}")

        if not content_text:
            content_text = title

        # 哈希去重（纳入附件链接，附件不同的通知不再误判为重复）
        content_hash = compute_hash(title, content_text, attachment_links)
        if Announcement.query.filter_by(content_hash=content_hash).first():
            return False

        announcement = Announcement(
            school_id=department.school_id,
            department_id=department.id,
            title=title,
            url=full_url,
            content_html=content_html,
            content_text=content_text,
            published_at=published_at,
            content_hash=content_hash,
        )
        db.session.add(announcement)
        return True

    except Exception as e:
        logger.error(f"处理公告项失败: {e}")
        return False


def scrape_department(department: Department, school_base_url: str,
                      since_year: int = SINCE_YEAR) -> tuple:
    """爬取单个部门（支持翻页），返回 (新增数量, 总数)

    如果部门的选择器为空或未匹配到内容，会自动探测常见的 CMS 模式。
    """
    new_count = 0
    total = 0
    page = 1
    consecutive_empty = 0
    consecutive_existing = 0  # 连续已存在通知计数（增量抓取）
    # 增量模式：上次完整抓取过 → 遇到连续已存在通知时提前停止
    incremental_mode = department.last_scraped_at is not None
    if incremental_mode:
        logger.info(f"[{department.name}] 增量模式：上次抓取 {department.last_scraped_at}, "
                    f"连续{INCREMENTAL_THRESHOLD}条已存在即停止")
    probed_profile = None  # 本次探测到的选择器配置
    pagination_style = None  # 翻页格式（None=默认/未探测, 非None=探测到的格式名）
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

    while page <= MAX_PAGES:
        page_url = _get_page_url(department.list_url, page, pagination_style or 'default')
        logger.info(f"[{department.name}] 第{page}页 {page_url}")

        try:
            html = _fetch_html(page_url)
        except Exception as e:
            # 第2页失败 → 尝试探测翻页格式
            if page == 2 and pagination_style is None:
                logger.info(f"[{department.name}] 默认翻页格式失败，尝试探测...")
                detected = _detect_pagination_style(department.list_url)
                if detected:
                    pagination_style = detected
                    page_url = _get_page_url(department.list_url, 2, pagination_style)
                    logger.info(f"[{department.name}] 使用探测到的翻页格式: {pagination_style}")
                    try:
                        html = _fetch_html(page_url)
                    except Exception:
                        logger.info(f"[{department.name}] 探测到的翻页格式也失败，停止翻页（仅第1页可用）")
                        break
                else:
                    logger.info(f"[{department.name}] 第2页不可用（非标准翻页或无更多内容），停止翻页")
                    break
            elif page > 1:
                logger.error(f"获取列表页失败 [{department.name}] P{page}: {e}")
                break
            else:
                logger.warning(f"第1页获取失败，尝试第2页...")
                page = 2
                continue

        soup = BeautifulSoup(html, 'lxml')
        if page == 1:
            first_page_html = html

        # --- 选择器探测：在 page 1 时执行 ---
        if page == 1:
            items = soup.select(list_sel) if list_sel else []

            if not items or len(items) < 1:
                # 🆕 列表为空可能是 JS 渲染页面 —— 用 Playwright 重试
                try:
                    from backend.scraper.fetchers.playwright_fetcher import (
                        fetch_html_with_browser, is_playwright_available
                    )
                except ImportError:
                    pass
                else:
                    if is_playwright_available():
                        logger.info(f"[{department.name}] curl_cffi 列表为空，尝试 Playwright: {page_url}")
                        browser_html = fetch_html_with_browser(page_url)
                        if browser_html:
                            soup = BeautifulSoup(browser_html, 'lxml')
                            items = soup.select(list_sel) if list_sel else []
                            if items:
                                logger.info(f"[{department.name}] Playwright 渲染后找到 {len(items)} 项")
                                first_page_html = browser_html
                            else:
                                logger.debug(f"[{department.name}] Playwright 渲染后仍无匹配项")

            if not items or len(items) < 1:
                # 尝试探测
                probed = _probe_selectors(html, department)
                if probed:
                    probed_profile, is_permanent = probed
                    list_sel = probed_profile['list_selector']
                    title_sel = probed_profile.get('title_selector', '')
                    link_sel = probed_profile.get('link_selector', '')
                    date_sel = probed_profile.get('date_selector', '')
                    content_sel = probed_profile.get('content_selector', '')
                    # 临时注入选择器到 department 对象，供 _process_announcement_item 使用
                    department.list_selector = list_sel
                    department.title_selector = title_sel
                    department.link_selector = link_sel
                    department.date_selector = date_sel
                    department.content_selector = content_sel
                    items = soup.select(list_sel)
                    logger.info(f"[{department.name}] 探测成功，使用模式: {probed_profile['name']} ({'永久保存' if is_permanent else '临时回退'}), 找到 {len(items)} 项")
                    if not is_permanent:
                        probed_profile = None  # 不保存回退模式，下次重新探测

                    # 🆕 保存元素签名（用于未来选择器自愈）
                    if items and is_permanent and department.school_id:
                        try:
                            selectors_for_sig = {
                                'list_selector': list_sel,
                                'title_selector': title_sel,
                                'link_selector': link_sel,
                                'date_selector': date_sel,
                            }
                            save_element_signatures(html, department.school_id, page_url, selectors_for_sig)
                        except Exception as e:
                            logger.debug(f"[{department.name}] 签名保存跳过: {e}")
                elif need_probe:
                    # 探测也失败了 → 尝试 Scrapling 自适应自愈
                    if department.school_id:
                        logger.info(f"[{department.name}] 探测失败，尝试选择器自愈...")
                        healed = auto_heal_selectors(
                            html, department.school_id, page_url, department.name
                        )
                        if healed:
                            list_sel = healed['list_selector']
                            title_sel = healed.get('title_selector', '')
                            link_sel = healed.get('link_selector', '')
                            date_sel = healed.get('date_selector', '')
                            content_sel = healed.get('content_selector', '')
                            department.list_selector = list_sel
                            department.title_selector = title_sel
                            department.link_selector = link_sel
                            department.date_selector = date_sel
                            department.content_selector = content_sel
                            items = soup.select(list_sel)
                            if items:
                                probed_profile = healed  # 标记为自愈结果，后续保存
                                is_permanent = True
                                logger.info(f"[{department.name}] 选择器自愈成功！新选择器: {list_sel}")
                            else:
                                probed_profile = None

                    if not items:
                        logger.info(f"[{department.name}] 探测失败，第1页无匹配内容，尝试第2页...")
                        page = 2
                        continue
                else:
                    # 选择器已配置但无匹配 → 可能是首页模板不同，或需要自愈
                    if department.school_id and list_sel:
                        logger.info(f"[{department.name}] 已配置选择器无匹配，尝试选择器自愈...")
                        healed = auto_heal_selectors(
                            html, department.school_id, page_url, department.name
                        )
                        if healed:
                            list_sel = healed['list_selector']
                            title_sel = healed.get('title_selector', '')
                            link_sel = healed.get('link_selector', '')
                            date_sel = healed.get('date_selector', '')
                            content_sel = healed.get('content_selector', '')
                            department.list_selector = list_sel
                            department.title_selector = title_sel
                            department.link_selector = link_sel
                            department.date_selector = date_sel
                            department.content_selector = content_sel
                            items = soup.select(list_sel)
                            if items:
                                probed_profile = healed
                                is_permanent = True
                                logger.info(f"[{department.name}] 选择器自愈成功！新选择器: {list_sel}")

                    if not items:
                        logger.info(f"[{department.name}] 第1页无匹配内容（选择器可能不兼容），尝试第2页...")
                        page = 2
                        continue

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
            if page == 1:
                logger.info(f"[{department.name}] 第1页无内容（可能是首页模板），尝试第2页...")
                page = 2
                continue
            else:
                logger.info(f"[{department.name}] 第{page}页无内容，停止翻页")
                break

        total += len(items)
        page_new = 0
        page_old = 0

        for item in items:
            # 先检查日期，早于since_year的跳过（不抓正文，节省请求）
            date_text = ''
            if department.date_selector:
                date_elem = item.select_one(department.date_selector)
                if date_elem:
                    date_text = date_elem.get_text(strip=True)
            pub_date = parse_date(date_text)
            if pub_date and pub_date.year < since_year:
                page_old += 1
                continue

            if _process_announcement_item(item, department, school_base_url):
                new_count += 1
                page_new += 1
                consecutive_existing = 0  # 有新通知 → 重置连续计数
            else:
                # 增量模式：计数连续已存在项，达到阈值则停止翻页
                if incremental_mode:
                    consecutive_existing += 1

        if page_new > 0:
            db.session.commit()
            logger.info(f"[{department.name}] P{page}: 新增{page_new}条")

        # 增量模式：连续N条已存在 → 已追平上次抓取进度，停止翻页
        if incremental_mode and consecutive_existing >= INCREMENTAL_THRESHOLD:
            logger.info(f"[{department.name}] P{page}连续{consecutive_existing}条已存在，增量抓取完成，停止翻页")
            break

        # 翻页策略：如果本页半数以上是旧数据，再翻一页确认后停止
        if page_old > len(items) * 0.6:
            logger.info(f"[{department.name}] P{page}大部分为{SINCE_YEAR}年前数据，停止翻页")
            break

        # 只有页面真正为空（无列表项）才计数；全去重说明后面可能还有新数据
        if len(items) == 0:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                logger.info(f"[{department.name}] 连续{consecutive_empty}页无内容，停止翻页")
                break
        else:
            consecutive_empty = 0

        # 第1页完成后，尝试从HTML推断翻页格式（避免第2页404浪费请求）
        if page == 1 and pagination_style is None:
            detected = _extract_pagination_style_from_html(html, department.list_url)
            if detected:
                pagination_style = detected
                logger.info(f"[{department.name}] 翻页格式预判: {detected}（从HTML链接推断，跳过第2页404探测）")

        page += 1

    logger.info(f"[{department.name}] 完成: {page}页/{total}条, 新增{new_count}条")

    # --- 更新上次抓取时间（用于下次增量抓取）---
    department.last_scraped_at = datetime.utcnow()
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


def _cleanup_noise_departments(school_id: int):
    """清理自动发现产生的噪声部门（非通知列表页被误识别为子部门）

    启发式规则：
    - 子部门（名称含 '-'）且 0 条通知 → 大概率是信息页（简介、队伍等）
    - 子部门使用回退选择器 (a[href]) 且 < 3 条通知 → 内容太少，可能是误识别
    """
    depts = Department.query.filter_by(school_id=school_id).all()
    removed = 0
    for d in depts:
        ann_count = Announcement.query.filter_by(department_id=d.id).count()
        is_sub = '-' in (d.name or '')
        is_fallback = (d.list_selector or '').strip() == 'a[href]'

        # 子部门 + 0 通知 → 几乎肯定是信息页噪声
        if is_sub and ann_count == 0:
            db.session.delete(d)
            removed += 1
            logger.info(f"[清理] 移除噪声子部门: {d.name} (0条通知)")
        # 子部门 + 回退选择器 + ≤ 2 通知 → 内容太少
        elif is_sub and is_fallback and ann_count <= 2:
            db.session.delete(d)
            removed += 1
            logger.info(f"[清理] 移除噪声子部门: {d.name} ({ann_count}条通知, 回退选择器)")
        # 顶级部门 + 0 通知 + 回退选择器 → 非通知列表页
        elif not is_sub and ann_count == 0 and is_fallback:
            db.session.delete(d)
            removed += 1
            logger.info(f"[清理] 移除噪声顶级部门: {d.name} (0条通知, 回退选择器)")

    if removed:
        logger.info(f"[清理] 共移除 {removed} 个噪声部门")


def scrape_school(school: School, since_year: int = SINCE_YEAR,
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

    log = ScrapeLog(school_id=school.id, status='running')
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
                configs = discover_school_departments(school.url)
                if configs:
                    # 删除旧的默认部门（如果存在）
                    if depts and depts[0].name == '通知公告':
                        db.session.delete(depts[0])
                        db.session.commit()
                        depts = []

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

        # 通知进度会话：部门列表已确定
        if progress_session:
            progress_session.set_departments(depts)

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
                logger.error(f"[{school.name}] 爬取部门 [{dept.name}] 失败: {e}", exc_info=True)
            finally:
                if progress_session:
                    progress_session.dept_done(dept.name, new, total)

        # 后处理：清理自动发现中的噪声子部门（0 通知 + 回退选择器 → 非通知列表页）
        _cleanup_noise_departments(school.id)
        db.session.commit()

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


def scrape_all_schools(since_year: int = SINCE_YEAR) -> list:
    """爬取所有活跃学校（上架且有订阅）"""
    schools = active_schools_query().all()
    results = []
    for school in schools:
        logger.info(f"开始爬取: {school.name}")
        log = scrape_school(school, since_year)
        results.append(log)
    return results


def manual_scrape(school_id: int, since_year: int = SINCE_YEAR) -> ScrapeLog:
    """手动触发单个学校的爬取"""
    school = db.session.get(School, school_id)
    if not school:
        raise ValueError(f"学校不存在: {school_id}")
    return scrape_school(school, since_year)
