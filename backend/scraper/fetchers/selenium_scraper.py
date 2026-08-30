"""Playwright 全浏览器模式爬虫 — 处理 JS 渲染和 AJAX 翻页的通知页面

替代原 Selenium 实现。当部门页面需要完整的浏览器环境（如 AJAX 翻页、
SPA 渲染）时使用此模块。大多数场景下 engine.py 的智能回退已足够，
此模块保留用于需要持久化浏览器会话的复杂翻页场景。

公共接口:
    scrape_department_playwright(department, school_base_url, since_year) -> (new_count, total)
"""

import logging
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from backend.database.db import db
from backend.database.models import Department, Announcement
from backend.scraper.change_detector import compute_hash, parse_date
from backend.scraper.engine import (
    SELECTOR_PROFILES, _save_probed_selectors, _extract_text, _resolve_url,
)
from backend.scraper.fetchers.playwright_fetcher import (
    is_playwright_available,
    _sync_playwright,
    _BROWSER_LAUNCH_KWARGS,
)

logger = logging.getLogger(__name__)

SINCE_YEAR = 2024
MAX_PAGES = 30


def _create_browser_and_page():
    """创建 Playwright 浏览器实例和一个新页面。

    与 playwright_fetcher.py 的单例不同，这里返回独立的
    (browser, context, page) 三元组，调用者负责清理。

    Returns:
        (browser, context, page) 或 (None, None, None)
    """
    if not is_playwright_available():
        logger.error("Playwright 未安装，无法使用全浏览器模式")
        return None, None, None

    try:
        pw = _sync_playwright().start()
        browser = pw.chromium.launch(**_BROWSER_LAUNCH_KWARGS)
        context = browser.new_context(
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/125.0.0.0 Safari/537.36'
            ),
            viewport={'width': 1920, 'height': 1080},
            locale='zh-CN',
        )
        page = context.new_page()
        logger.info("Playwright 全浏览器爬虫已就绪")
        return browser, context, page
    except Exception as e:
        logger.error(f"无法启动 Playwright 浏览器: {e}")
        logger.error("请运行: python -m playwright install chromium")
        return None, None, None


def _extract_items_from_page(page, department):
    """从当前 Playwright 页面提取通知列表项。"""
    html = page.content()
    soup = BeautifulSoup(html, 'lxml')
    items = soup.select(department.list_selector)
    return items


def _click_next_page(page, department, current_page):
    """尝试点击翻到下一页，返回是否成功。"""
    try:
        # 常见翻页按钮选择器
        next_selectors = [
            "a.next:not(.disabled)",
            ".c-pagination a:last-child",
            ".middleNotice__paging a:last-child",
            f"a[href*='index_{current_page + 1}']",
            ".pagination .next",
            ".page a.next",
            "a:has-text('下一页')",
            "a:has-text('>')",
        ]
        for sel in next_selectors:
            try:
                elem = page.locator(sel).first
                if elem.is_visible():
                    elem.click()
                    page.wait_for_load_state('networkidle', timeout=10000)
                    time.sleep(1)  # 额外等待 AJAX 完成
                    return True
            except Exception:
                continue

        return False
    except Exception as e:
        logger.debug(f"翻页失败: {e}")
        return False


def scrape_department_selenium(department: Department, school_base_url: str,
                               since_year: int = SINCE_YEAR) -> tuple:
    """用 Playwright 全浏览器模式爬取单个部门（处理 AJAX 加载和翻页）。

    与 engine.scrape_department() 的区别：
    - 列表页通过 Playwright 获取（完全 JS 渲染）
    - 翻页使用 DOM 点击而非 URL 拼接
    - 详情页优先用 curl_cffi（快），失败时回退 Playwright
    - 适合 AJAX 翻页、React/Vue SPA 等场景

    注意：此函数保留原名 scrape_department_selenium 以保持
    向后兼容，但内部已完全替换为 Playwright。
    """
    browser = None
    context = None
    page = None
    new_count = 0
    total = 0
    page_num = 1
    probed_profile = None
    is_permanent = False

    # 检查 Playwright 是否可用
    if not is_playwright_available():
        logger.error(
            "Playwright 不可用，无法使用全浏览器模式。"
            "请运行: pip install playwright && python -m playwright install chromium"
        )
        return 0, 0

    # 获取有效选择器
    list_sel = (department.list_selector or '').strip()
    need_probe = not list_sel

    # 保存原始选择器
    _orig_list_sel = department.list_selector
    _orig_title_sel = department.title_selector
    _orig_link_sel = department.link_selector
    _orig_date_sel = department.date_selector
    _orig_content_sel = department.content_selector

    try:
        browser, context, page = _create_browser_and_page()
        if not browser:
            return 0, 0

        list_url = department.list_url
        logger.info(f"[Playwright] [{department.name}] 开始爬取 {list_url}")

        page.goto(list_url, wait_until='networkidle', timeout=30000)
        time.sleep(2)  # 等待 AJAX 加载

        while page_num <= MAX_PAGES:
            logger.info(f"[Playwright] [{department.name}] 第{page_num}页")

            items = _extract_items_from_page(page, department)

            # --- 选择器探测：page 1 ---
            if page_num == 1 and (not items or len(items) < 1) and (need_probe or not list_sel):
                html = page.content()
                soup = BeautifulSoup(html, 'lxml')

                for profile in SELECTOR_PROFILES:
                    try:
                        probe_items = soup.select(profile['list_selector'])
                        if probe_items and len(probe_items) >= 3:
                            probed_profile = profile
                            is_permanent = True
                            department.list_selector = profile['list_selector']
                            department.title_selector = profile.get('title_selector', '')
                            department.link_selector = profile.get('link_selector', '')
                            department.date_selector = profile.get('date_selector', '')
                            department.content_selector = profile.get('content_selector', '')
                            items = probe_items
                            logger.info(
                                f"[Playwright] [{department.name}] 探测到匹配模式: "
                                f"{profile['name']}, 共{len(items)}项"
                            )
                            break
                    except Exception:
                        continue

            if not items:
                logger.info(f"[Playwright] [{department.name}] 第{page_num}页无内容，停止")
                break

            total += len(items)
            page_old = 0

            for item in items:
                try:
                    # 标题
                    title_elem = (
                        item.select_one(department.title_selector)
                        if department.title_selector else item
                    )
                    if not title_elem:
                        continue
                    if title_elem.name == 'a' and title_elem.get('title'):
                        title = title_elem.get('title').strip()
                    else:
                        title = title_elem.get_text(strip=True)
                    if not title or len(title) < 2:
                        continue

                    # 链接
                    link_elem = (
                        item.select_one(department.link_selector)
                        if department.link_selector else item
                    )
                    link = link_elem.get('href', '') if link_elem else ''
                    full_url = urljoin(school_base_url or list_url, link)

                    # 日期
                    date_text = ''
                    if department.date_selector:
                        date_elem = item.select_one(department.date_selector)
                        if date_elem:
                            date_text = date_elem.get_text(strip=True)
                    published_at = parse_date(date_text)

                    # 早于目标年份跳过
                    if published_at and published_at.year < since_year:
                        page_old += 1
                        continue

                    # URL去重
                    if full_url and Announcement.query.filter_by(url=full_url).first():
                        continue

                    # 获取正文（优先 curl_cffi，失败时 Playwright）
                    content_html = ''
                    content_text = title
                    if full_url and department.content_selector:
                        try:
                            # 先用 curl_cffi 快速获取
                            from curl_cffi import requests as req
                            resp = req.get(full_url, headers={
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                              'AppleWebKit/537.36'
                            }, timeout=15, impersonate='chrome')
                            detail_soup = BeautifulSoup(resp.text, 'lxml')
                            content_elem = detail_soup.select_one(department.content_selector)
                            if content_elem:
                                for tag in content_elem.find_all(['a', 'img']):
                                    for attr in ['href', 'src']:
                                        val = tag.get(attr)
                                        if val and not val.startswith(
                                            ('http://', 'https://', '//', '#',
                                             'javascript:', 'mailto:', 'data:')
                                        ):
                                            tag[attr] = _resolve_url(full_url, val)
                                content_html = str(content_elem)
                                content_text = _extract_text(str(content_elem))
                        except Exception:
                            # curl_cffi 失败，尝试 Playwright
                            logger.debug(
                                f"[Playwright] curl_cffi 详情页失败，Playwright 重试: {full_url}"
                            )
                            try:
                                detail_page = context.new_page()
                                detail_page.goto(
                                    full_url, wait_until='networkidle', timeout=20000
                                )
                                detail_soup = BeautifulSoup(detail_page.content(), 'lxml')
                                detail_page.close()
                                content_elem = detail_soup.select_one(
                                    department.content_selector
                                )
                                if content_elem:
                                    for tag in content_elem.find_all(['a', 'img']):
                                        for attr in ['href', 'src']:
                                            val = tag.get(attr)
                                            if val and not val.startswith(
                                                ('http://', 'https://', '//', '#',
                                                 'javascript:', 'mailto:', 'data:')
                                            ):
                                                tag[attr] = _resolve_url(full_url, val)
                                    content_html = str(content_elem)
                                    content_text = _extract_text(str(content_elem))
                            except Exception as be:
                                logger.debug(f"[Playwright] 详情页也失败: {be}")

                    content_hash = compute_hash(title, content_text)
                    if Announcement.query.filter_by(content_hash=content_hash).first():
                        continue

                    ann = Announcement(
                        school_id=department.school_id,
                        department_id=department.id,
                        title=title,
                        url=full_url,
                        content_html=content_html,
                        content_text=content_text,
                        published_at=published_at,
                        content_hash=content_hash,
                    )
                    db.session.add(ann)
                    new_count += 1

                except Exception as e:
                    logger.error(f"[Playwright] 处理失败: {e}")
                    continue

            if new_count > 0 and new_count % 10 == 0:
                db.session.commit()

            # 翻页判断
            if page_old > len(items) * 0.6:
                logger.info(
                    f"[Playwright] [{department.name}] 大部分为{since_year}年前数据，停止"
                )
                break

            # 尝试翻页
            if not _click_next_page(page, department, page_num):
                # 也尝试直接URL翻页
                next_url = department.list_url.rstrip('/') + f"/index_{page_num + 1}.shtml"
                try:
                    page.goto(next_url, wait_until='networkidle', timeout=20000)
                    time.sleep(2)
                    # 检查是否404
                    current_html = page.content()
                    if "404" in page.title() or "Not Found" in current_html[:500]:
                        logger.info(
                            f"[Playwright] [{department.name}] 第{page_num+1}页404，停止"
                        )
                        break
                    page_num += 1
                    continue
                except Exception:
                    logger.info(
                        f"[Playwright] [{department.name}] 无法翻到第{page_num+1}页，停止"
                    )
                    break

            page_num += 1

        if new_count > 0:
            db.session.commit()

        logger.info(
            f"[Playwright] [{department.name}] 完成: {page_num}页/{total}条, 新增{new_count}条"
        )

        # --- 探测后处理 ---
        if probed_profile and is_permanent:
            _save_probed_selectors(department, probed_profile)
        else:
            department.list_selector = _orig_list_sel
            department.title_selector = _orig_title_sel
            department.link_selector = _orig_link_sel
            department.date_selector = _orig_date_sel
            department.content_selector = _orig_content_sel

    except Exception as e:
        logger.error(f"[Playwright] [{department.name}] 异常: {e}")
        db.session.rollback()
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass
        if context:
            try:
                context.close()
            except Exception:
                pass
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    return new_count, total
