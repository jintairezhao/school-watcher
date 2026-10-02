"""通用导航解析器

解析任意大学网站首页的导航结构，识别部门/学院链接。
从 dom_analyzer 的 find_navigation_area 结果中进一步分类和过滤。
"""

import logging
import re
from urllib.parse import urljoin, urlparse

from backend.scraper.http_client import requests
from bs4 import BeautifulSoup

from backend.scraper.detectors.dom_analyzer import find_navigation_area, find_notice_list_page_links

logger = logging.getLogger(__name__)

# ============================================================
# 可配置的关键词集（保留现有 site_discovery.py 中的成熟集合）
# ============================================================

# 部门名称指示词（含这些词的链接大概率是部门页面）
DEPT_NAME_INDICATORS = [
    '学院', '系', '部', '处', '室', '院',
    '研究院', '研究所', '实验室', '中心', '基地',
    '实验教学', '示范中心', '工程中心', '研究中心',
    '实验室', '测试中心', '平台',
    'College', 'School', 'Institute', 'Department',
    'Center', 'Laboratory', 'Lab',
]

# 部门容器页指示词（含这些词的是院系列表汇总页）
DEPT_CONTAINER_PATTERNS = [
    '院系设置', '教学单位', '科研机构', '组织机构',
    '学院设置', '二级学院', '教学院系', '教学部门',
    '院系部门', '机构设置', '直属单位', '研究机构',
    '附属单位', '学术单位', '学部', '学院',
]

# 服务/非部门类（应过滤）
SERVICE_CATEGORY_PATTERNS = [
    '招生', '就业', '人才招聘', '图书馆', '校医院',
    '后勤', '饮食', '宿舍', '保卫', '财务',
    '网络', '信息中心', '出版社', '学报', '档案',
    '校友', '基金', '合作', '交流',
]

# 通用导航名（非部门，应过滤）
GENERIC_NAV_NAMES = {
    '首页', 'Home', 'English', 'Russian', '网站地图', '联系我们',
    '登录', '注册', '邮箱', 'VPN', 'OA', '一网通办', '信息门户',
    '办公系统', '校园卡', '班车', '校历', '地图', '导航',
    '旧版', '手机版', '微信公众号', '微博', 'APP',
    '教师', '学生', '教职工', '校友', '考生', '访客',
    '学校概况', '师资队伍', '校园文化', '新闻中心', '学术活动',
    '校园风光', '校园导览', '走进', '关于',
}

# 非部门子页（信息页，不是通知列表页）
INFORMATIONAL_PAGE_NAMES = {
    '简介', '概况', '历史', '沿革', '历任', '领导', '致辞',
    '机构', '队伍', '师资', '人才', '学科', '专业', '课程',
    '成果', '获奖', '专利', '论文', '项目',
    '招生', '就业', '培养', '学位', '教学',
    '科研', '学术', '交流', '合作', '国际',
    '党建', '工会', '学生', '社团', '活动',
    '设备', '安全', '规章', '制度', '指南',
    '下载', '表格', '链接', '相关', '友情',
    '学术交流', '人才培养', '社会服务', '开放课题',
    '新闻动态', '实验室新闻', '安全管理', '仪器平台',
    '访问学者', '学术会议', '科技服务', '培训',
    '留学生', '孔子学院', '继续教育', '远程教育',
    '校园生活', '文化', '体育', '艺术',
    '招聘', '招标', '采购',
}

# 非部门URL模式
NON_DEPT_URL_PATTERNS = [
    r'/bwc/', r'/hq/', r'/tsg/', r'/wzsy/', r'/yyw/',
    r'/news/', r'/xwzx/', r'/xwdt/', r'/info/', r'/article/',
    r'/video/', r'/photo/', r'/images/', r'/upload/',
    r'/download/', r'/system/', r'/search/', r'/tags/',
    r'/rss/', r'/feed/', r'/sitemap/',
    r'\.pdf$', r'\.doc$', r'\.docx$', r'\.xls$', r'\.xlsx$',
    r'\.zip$', r'\.rar$', r'\.ppt$', r'\.pptx$',
]


def parse_school_navigation(html, base_url):
    """解析学校首页导航，提取部门/学院结构。

    整合了现有 site_discovery.py 的 Phase 0/1 逻辑，
    但使用通用 DOM 分析代替硬编码 CSS 选择器。

    Args:
        html: 首页 HTML
        base_url: 学校首页 URL

    Returns:
        dict: {
            'categories': [{name, url, type, children: [{name, url}]}],
            'total_candidates': int,
            'confidence': float,
        }
    """
    soup = BeautifulSoup(html, 'lxml')

    # 步骤 1: 找到导航区域
    nav_result = find_navigation_area(soup)
    if not nav_result or not nav_result.get('categories'):
        if nav_result:
            logger.info(f"导航区域已找到但分类为空 ({nav_result['link_count']} 链接)，回退到全页提取")
        else:
            logger.warning("Could not find navigation area, trying flat link extraction")
        return _fallback_flat_extraction(soup, base_url)

    # 步骤 2: 过滤并分类导航项
    categories = []
    for cat in nav_result.get('categories', []):
        name = cat['name']
        url = cat['url']
        children = cat.get('children', [])

        # 跳过通用导航
        if _should_skip_category(name):
            continue

        # 分类
        cat_type = _classify_category(name, url, children)

        if cat_type == 'skip':
            continue

        # 解析完整 URL
        full_url = urljoin(base_url, url) if url else ''
        for child in children:
            child['url'] = urljoin(base_url, child['url']) if child.get('url') else ''

        categories.append({
            'name': name,
            'url': full_url,
            'type': cat_type,
            'children': children,
        })

    # 补充扫描：顶级导航常漏掉「组织机构」这类容器页（藏在页脚/侧栏），
    # 全页扫汇总页命名链接补为 listing 分类，否则组织机构表永远发现不了
    covered = {c['url'].split('#')[0].rstrip('/') for c in categories if c.get('url')}
    for extra in find_listing_pages_on_page(soup, base_url, covered):
        categories.append(extra)

    # 计算候选部门数
    total_candidates = sum(
        len(c['children']) if c['type'] == 'submenu' and c['children'] else 1
        for c in categories
    )

    return {
        'categories': categories,
        'total_candidates': total_candidates,
        'confidence': nav_result['confidence'],
    }


def find_listing_pages_on_page(soup, base_url, covered_urls):
    """全页扫描汇总页链接（如页脚「组织机构」），返回未覆盖的 listing 分类。"""
    extras = []
    seen = set()
    for a in soup.find_all('a', href=True):
        text = ' '.join(a.get_text(strip=True).split())
        if not _is_listing_page_name(text):
            continue
        full = urljoin(base_url, a['href'])
        key = full.split('#')[0].rstrip('/')
        if key in covered_urls or key in seen:
            continue
        seen.add(key)
        extras.append({'name': text, 'url': full, 'type': 'listing', 'children': []})
    return extras


def _should_skip_category(name):
    """判断导航分类是否应跳过。"""
    name_lower = name.lower().strip()
    for skip_name in GENERIC_NAV_NAMES:
        if skip_name.lower() in name_lower:
            return True
    return False


def _classify_category(name, url, children):
    """分类导航项为 department / listing / service / submenu / skip。

    使用子串匹配（与现有 site_discovery.py 一致）。
    """
    name_lower = name.lower().strip()

    # 服务类
    for kw in SERVICE_CATEGORY_PATTERNS:
        if kw in name:
            return 'skip'

    # 部门容器类（汇总页）
    for kw in DEPT_CONTAINER_PATTERNS:
        if kw in name:
            if children and len(children) >= 3:
                return 'submenu'  # 有子菜单→直接从children提取
            return 'listing'  # 需要访问汇总页枚举

    # 含子菜单
    if children and len(children) >= 2:
        # 检查子项是否有部门指标
        dept_children = sum(
            1 for c in children
            if _is_dept_like_name(c.get('name', ''))
        )
        if dept_children >= 2:
            return 'submenu'
        # 子项不够 → 可能是服务类
        return 'skip'

    # 单个链接
    if _is_dept_like_name(name):
        return 'department'

    # URL 路径特征
    if url:
        url_lower = url.lower()
        for pattern in NON_DEPT_URL_PATTERNS:
            if re.search(pattern, url_lower):
                return 'skip'
        if any(kw in url_lower for kw in ['tzgg', 'notice', 'tongzhi', 'xytz']):
            return 'department'

    return 'skip'


def _is_dept_like_name(name):
    """判断名称是否像一个部门名。"""
    if not name or len(name) < 2:
        return False

    # 排除信息页面名
    for kw in INFORMATIONAL_PAGE_NAMES:
        if kw in name and len(name) < 8:
            return False

    # 排除通用导航
    for kw in GENERIC_NAV_NAMES:
        if kw in name:
            return False

    # 检查部门指示词
    for kw in DEPT_NAME_INDICATORS:
        if kw in name:
            return True

    # 长度 + 无负面信号 → 可能是一个部门
    if len(name) >= 3 and not any(
        kw in name for kw in ['首页', '搜索', '登录']
    ):
        return True

    return False


def _fallback_flat_extraction(soup, base_url):
    """当导航解析失败时，从全页提取所有可能的部门链接。

    返回结果会经过 _classify_category 再分类，正确区分
    department / listing / submenu / skip 类型。
    """
    links = []
    seen = set()

    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href'].strip()
        text = a_tag.get_text(strip=True)

        if not href or not text or len(text) < 2:
            continue
        if href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue

        if _is_dept_like_name(text) or _is_listing_page_name(text):
            full_url = urljoin(base_url, href)
            if full_url not in seen:
                seen.add(full_url)
                links.append({
                    'name': text,
                    'url': full_url,
                    'type': 'department',  # 临时标签，parse_school_navigation 会重新分类
                    'children': [],
                })

    # 去重并限制数量
    links = links[:30]

    # 🆕 重新分类：对 fallback 提取的链接也要区分 department/listing
    classified = []
    for link in links:
        cat_type = _classify_category(link['name'], link['url'], link.get('children', []))
        if cat_type == 'skip':
            continue
        link['type'] = cat_type
        classified.append(link)

    return {
        'categories': classified,
        'total_candidates': len(classified),
        'confidence': 0.3,
    }


def _is_listing_page_name(name):
    """判断名称是否是一个汇总页名称（如「院系设置」「学部与院系」）。

    这类页面需要被标记为 listing 类型以便枚举其子实体。
    注意：单个学院/系（如「数学系」「物理学院」）不应标记为 listing。
    """
    if not name:
        return False
    # 严格匹配：必须是公认的汇总页关键词
    strict_listing_keywords = [
        '院系设置', '教学单位', '科研机构', '组织机构',
        '学院设置', '二级学院', '教学院系', '教学部门',
        '院系部门', '机构设置', '直属单位', '研究机构',
        '附属单位', '学术单位',
    ]
    for kw in strict_listing_keywords:
        if kw in name:
            return True
    # "学部"独立出现且不是"X学部"（如"工学部"中的学部是实体，不是汇总页）
    if name in ('学部与院系', '学部', '院系') or name.startswith('学部') and '与' in name:
        return True
    # 包含"与"且两端都是组织类型词（如"学部与院系"、"学院与研究院"）
    if '与' in name and len(name) >= 5:
        parts = name.split('与')
        org_count = sum(1 for p in parts if any(
            kw in p for kw in ['学部', '学院', '系', '所', '中心', '研究院', '基地', '实验室']
        ))
        if org_count >= 2:
            return True
    return False


_NEWS_ARTICLE_URL_RE = re.compile(r'/c/\d{4}-\d{2}-\d{2}/')


def _is_org_structure_table(table):
    """判断表格是否为「组织机构」三列表格（单位名称 / 挂靠单位 / 备注）。"""
    first_row = table.find('tr')
    if not first_row:
        return False
    header_text = re.sub(r'\s+', '', first_row.get_text())
    return '挂靠单位' in header_text or '备注' in header_text


def _extract_org_structure_entities(table, base_url):
    """从组织机构三列表格提取父单位（列0）+ 子单位（列1/2，含链接）。

    子单位名以「父名-子名」拼接，保留官网中的隶属关系。
    跳过新闻文章页（/c/YYYY-MM-DD/）以及与父 URL 相同的链接；不在此处按
    _is_dept_like_name 强过滤子单位，交给下游探针判断是否真发通知。
    """
    entities = []
    for row in table.find_all('tr'):
        cells = row.find_all(['td', 'th'])
        if not cells or len(cells) < 2:
            continue

        # 列 0：单位名称（父）
        parent_cell = cells[0]
        parent_links = parent_cell.find_all('a', href=True)
        if parent_links:
            parent_name = parent_links[0].get_text(strip=True)
            parent_url = urljoin(base_url, parent_links[0]['href'])
        else:
            parent_name = parent_cell.get_text(strip=True)
            parent_url = ''

        if parent_name and parent_url:
            entities.append({'name': parent_name, 'url': parent_url})

        parent_key = parent_url.rstrip('/')

        # 列 1/2：挂靠单位、备注（子）
        for cell in cells[1:]:
            for a in cell.find_all('a', href=True):
                child_name = a.get_text(strip=True)
                child_url = urljoin(base_url, a['href'])
                if not child_name or not child_url:
                    continue
                if _NEWS_ARTICLE_URL_RE.search(child_url):
                    continue
                if child_url.rstrip('/') == parent_key:
                    continue
                name = f"{parent_name}-{child_name}" if parent_name else child_name
                entities.append({'name': name, 'url': child_url})

    return entities


def enumerate_listing_page(html, base_url):
    """从部门汇总页（如「院系设置」）枚举子实体。

    处理 HTML 表格和链接列表两种常见格式；若检测到「组织机构」三列表格
    （单位名称 / 挂靠单位 / 备注），则提取完整父子层级。

    Args:
        html: 汇总页 HTML
        base_url: 汇总页 URL

    Returns:
        list: [{name, url}] 子实体列表
    """
    soup = BeautifulSoup(html, 'lxml')
    entities = []

    # 策略 0: 组织机构三列表格（优先，提取父子层级）
    for table in soup.find_all('table'):
        if _is_org_structure_table(table):
            org_entities = _extract_org_structure_entities(table, base_url)
            if org_entities:
                return org_entities
            break

    # 策略 1: 表格提取
    tables = soup.find_all('table')
    for table in tables:
        rows = table.find_all('tr')
        for row in rows:
            cells = row.find_all(['td', 'th'])
            if not cells:
                continue
            # 取第一个含链接的单元格
            for cell in cells:
                a_tag = cell.find('a', href=True)
                if a_tag:
                    name = a_tag.get_text(strip=True)
                    href = a_tag['href']
                    if name and len(name) >= 2 and _is_dept_like_name(name):
                        entities.append({
                            'name': name,
                            'url': urljoin(base_url, href),
                        })
                        break

    # 策略 2: 链接块提取
    if not entities:
        # 找链接密度最高的区域
        containers = soup.find_all(['div', 'ul', 'section', 'article'])
        best = None
        best_count = 0
        for container in containers:
            links = container.find_all('a', href=True)
            dept_links = [
                a for a in links
                if _is_dept_like_name(a.get_text(strip=True))
            ]
            if len(dept_links) > best_count:
                best_count = len(dept_links)
                best = dept_links

        if best:
            for a_tag in best:
                name = a_tag.get_text(strip=True)
                href = a_tag['href']
                entities.append({
                    'name': name,
                    'url': urljoin(base_url, href),
                })

    return entities


_COLUMN_HINT_WORDS = ['通知', '公告', '新闻', '动态', '工作', '项目', '招标', '询价',
                      '制度', '规章', '文档', '服务', '公示', '科研', '活动', '党建']


def enumerate_subsite_columns(html, base_url, parent_name, max_count=8):
    """枚举父部门子站首页上的其他栏目链接，生成「父-子」候选。

    只取同站、非文章页、栏目名短小的链接；是否真为通知列表交给下游
    探测（detect_notice_list）把关。用于让发现结果覆盖子站内部栏目，
    而不只是子站首页一个入口。
    """
    soup = BeautifulSoup(html, 'lxml')
    base = urlparse(base_url)
    base_path = base.path.rstrip('/')
    cols = []
    seen = set()
    for a in soup.find_all('a', href=True):
        text = ' '.join(a.get_text(strip=True).split())
        if not (2 <= len(text) <= 8):
            continue
        if text in GENERIC_NAV_NAMES or '更多' in text or text.lower() in ('more', 'more+', '>>', 'more>>'):
            continue
        full = urljoin(base_url, a['href'])
        p = urlparse(full)
        if p.netloc != base.netloc:
            continue
        if _NEWS_ARTICLE_URL_RE.search(full):
            continue
        key = full.split('#')[0].rstrip('/')
        if not key or key == base_path or key in seen:
            continue
        # 只要子站内部路径（深度 ≥ 1 段），首页本身不算栏目
        rel = p.path.rstrip('/')
        if not rel or rel == base_path:
            continue
        seen.add(key)
        score = 2 if any(w in text for w in _COLUMN_HINT_WORDS) else 0
        if any(k in key.lower() for k in ('tzgg', 'notice', 'news', 'xwdt')):
            score += 2
        cols.append({'name': f"{parent_name}-{text}", 'url': full, 'score': score})
    cols.sort(key=lambda c: c['score'], reverse=True)
    return [{'name': c['name'], 'url': c['url']} for c in cols[:max_count]]


def find_dept_notice_url(html, dept_base_url):
    """从部门首页找到通知列表子页面的 URL。

    结合 find_notice_list_page_links 和标准路径探测。

    Args:
        html: 部门首页 HTML
        dept_base_url: 部门首页 URL

    Returns:
        dict: {url, method, confidence} 或 None
    """
    soup = BeautifulSoup(html, 'lxml')

    # 策略 1: 页面链接扫描
    candidates = find_notice_list_page_links(soup, dept_base_url)
    if candidates:
        best = candidates[0]
        if best['score'] > 3:
            return {
                'url': best['url'],
                'method': 'link_scan',
                'confidence': min(0.9, best['score'] / 10),
            }

    # 策略 2: 标准路径探测
    standard_paths = [
        'tzgg/', 'tzgg/index.htm', 'tzgg/index.html',
        'notice/', 'notice/index.htm',
        'tongzhi/', 'tongzhigonggao/',
        'xytzgg/', 'xytzgg/index.htm',
        'tzgg2/', 'tzgg2/index.htm', 'tzgg3/', 'tzgg3/index.htm',
        'news/', 'news/index.htm',
        'xwdt/', 'xwzx/',
        'ggl/', 'tzg/',
    ]

    from backend.scraper.engine import _fetch_html
    for path in standard_paths:
        test_url = urljoin(dept_base_url.rstrip('/') + '/', path)
        try:
            # Permissive purpose on purpose: this is a probe that only wants the
            # bytes. The list contract is enforced below by find_repeating_blocks,
            # which is stricter here than the transport's list heuristic.
            resp_html = _fetch_html(test_url, allow_browser_fallback=False)
            if resp_html:
                soup_test = BeautifulSoup(resp_html, 'lxml')
                # 检查是否有通知列表特征
                from backend.scraper.detectors.dom_analyzer import find_repeating_blocks
                blocks = find_repeating_blocks(soup_test, min_repeat=3)
                if blocks and any(
                    b['count'] >= 3 and b['date_ratio'] > 0.3
                    for b in blocks[:3]
                ):
                    return {
                        'url': test_url,
                        'method': 'standard_path',
                        'confidence': 0.7,
                    }
        except Exception:
            continue

    return None
