"""智能站点发现引擎 — 自动识别高校网站部门结构和通知列表页

从学校首页出发，自动发现：
1. 首页导航层级结构（顶部菜单 → 子菜单 → 链接）
2. 各部门/院系列表页 URL（含二级列表页枚举，如"院系设置"表格）
3. 部门之间的父子层级关系（导航结构 + URL 路径包含关系）
4. 每个部门页面适用的 CSS 选择器
5. 子部门（如学院下辖的各系）

策略（改进版）：
- 阶段 0: 解析首页导航层级 → 识别分类（院系设置/科学研究/组织机构/招生就业等）
- 阶段 1: 分类处理
    - 子菜单型（科学研究 → 各实验室）→ 子项即部门
    - 列表页型（院系设置 → 表格列出所有学院）→ 访问列表页枚举子实体
    - 单页型 → 旧版扁平链接提取
- 阶段 2: 并行访问各部门 → 探测通知列表 + 发现子部门
- 阶段 3: 构建部门树 → 写入数据库
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from backend.database.db import db
from backend.database.models import Department
from backend.scraper.engine import _fetch_html, SELECTOR_PROFILES

logger = logging.getLogger(__name__)

# 并行抓取线程数
MAX_WORKERS = 6
# 最多发现的顶级部门数（需足够覆盖大学全部院系+研究机构）
MAX_TOP_DEPTS = 30
# 每个部门最多子部门数
MAX_SUB_DEPTS = 8

# 明确的非部门名称（这些是通用导航，不包含通知列表）
GENERIC_NAV_NAMES = {
    '首页', 'Home', '网站首页', '学校首页',
    'English', '英文版', 'EN', 'ENGLISH',
    '旧版', '旧版入口', '回顾旧版',
    '校园地图', '学校地图', '地图',
    '联系我们', '联系方式', 'Contact',
    '登录', 'Login', '统一身份认证', '邮箱', '邮件系统',
    'VPN', '网关', '信息门户', '服务门户', '网上办事',
    '图书馆', '图书', 'Library',
    '校长信箱', '书记信箱', '投诉建议',
    'RSS', '订阅',
    # 非部门服务/入口（通常不包含通知列表）
    '人才招聘', '人才引进', '招聘信息', '招聘',
    '招标', '招标公告', '采购', '招标采购', '招标询价',
    '基金会', '教育基金会', '校友会', '校友',
    '档案馆', '档案', '校史馆',
    '信息公开', '信息公告',
    '新闻网', '新闻中心', '学校新闻',
    '校园文化', '风华石大', '学风建设', '校园风光', '校园全景',
    '网络信息', '网络服务', '信息化', '信息技术',
    '后勤', '后勤服务', '后勤保障',
    '医疗保险', '医疗卫生', '校医院', '计生办', '卫生',
    '网络继续教育', '远程教育', '成人教育', '继续教育',
    '本科生招生', '研究生招生', '招生信息', '招生网', '招生就业',
    '就业', '就业信息', '就业指导', '创业',
    '网上报修', '报修平台', '服务平台',
    '办事大厅', '办事指南', '服务大厅',
    '校园邮箱', '电子邮件', '邮箱登录',
    '一卡通', '校园卡', '统一支付',
}

# URL 路径模式：明确不是部门列表页的
NON_DEPT_URL_PATTERNS = [
    r'/$',                          # 首页
    r'/index\.\w+$',                # 首页变体
    r'/login',                       # 登录
    r'/search',                      # 搜索
    r'/english', r'/en/',           # 英文版
    r'/video\b', r'/photo\b', r'/gallery\b',  # 媒体
    r'/map\b', r'/contact\b',       # 地图、联系
    r'/culture\b',                   # 校园文化
    r'/sitemap\b',                   # 站点地图
    r'/vpn\b', r'/email\b', r'/mail\b',  # 系统服务
    r'\.(pdf|doc|xls|ppt|jpg|png|gif|bmp|css|js|xml|rss)(\?|$)',  # 文件
]

# 极短字符（通常是图标字体等噪声，不是部门名）
NOISE_TEXTS = {'>>', '>', '•', '·', '更多', 'More', 'MORE', 'TOP', 'top', '×', '关闭',
               '|', '::', '...', '…', '返回', '下一页', '上一页',
               '当前页', '当前位置', '您的位置', '面包屑导航'}

# 信息页而非通知列表页的子页面名称模式
INFORMATIONAL_PAGE_NAMES = {
    '简介', '概况', '介绍', '概述', '关于', 'About',
    '历史', '历史沿革', '发展历程',
    '领导', '领导介绍', '领导班子', '现任领导',
    '机构', '机构设置', '组织架构',
    '委员', '委员会', '学术委员会', '学位委员会', '教学委员会',
    '队伍', '队伍介绍', '研究队伍', '师资队伍', '导师队伍',
    '成果', '研究成果', '获奖成果', '科研成果',
    '获奖', '荣誉', '表彰',
    '设备', '设施', '仪器设备', '科研平台',
    '环境', '实验环境',
    '下载', '下载中心', '文件下载', '资料下载', 'Download',
    '链接', '友情链接', '相关链接',
    '留言', '联系我们', '联系方式',
    'English', '英文版', 'русский', 'Русский',
    # 实验室/学院导航标签（非独立通知列表页）
    '学术交流', '交流合作', '学术活动',
    '人才培养', '培养方案', '学生培养',
    '社会服务', '产学研合作',
    '开放课题', '开放基金', '基金申请',
    '新闻动态', '实验室新闻', '综合新闻',
    '安全管理', '实验室安全',
    '仪器平台', '共享平台', '大型仪器',
    '规章制度', '管理办法',
    '访问学者', '客座教授',
    '会议', '学术会议', '学术报告',
    '招生信息', '招生简章',
}

# 非中文语言版本标识
NON_CN_PATTERNS = [
    r'/english', r'/en/', r'/en$', r'/english/',
    r'/russian', r'/ru/', r'/ru$',
    r'/japanese', r'/jp/', r'/jp$',
    r'/korean', r'/kr/', r'/kr$',
    r'/french', r'/fr/', r'/fr$',
    r'/german', r'/de/', r'/de$',
]

# ---- 导航层级解析：分类与部门识别 ----

# 导航顶级分类：明确不是部门容器的（跳过整个分类）
SKIP_CATEGORY_PATTERNS = ['学校概况', '师资队伍', '校园文化', '首页', 'English', 'Русский']

# 导航顶级分类：明确是部门容器（需进入二级枚举的）
# 注意：仅包含「列出下级实体」的容器页（如院系设置列出各学院）
# 「组织机构」类页面包含大量行政单位，很多不产生通知，暂不在此枚举
DEPT_CONTAINER_PATTERNS = [
    '院系设置', '学院设置', '院系', '教学单位', '教学机构',
    '科研机构', '研究机构', '学术单位', '学部',
]

# 部门名称指示词（子串匹配，用于判断子菜单项是否为部门）
DEPT_NAME_INDICATORS = [
    '学院', '实验室', '研究所', '研究院', '中心', '工程系',
    '科学系', '技术系', '教学部', '教研室', '重点实验室',
    '项目', '平台', '基地',
]

# 非部门页面名称（即使出现在子菜单中也跳过）
NON_DEPT_CHILD_PATTERNS = [
    '科技查新', '校历', '校园卡', '图书期刊',
    '地图', '学校地图', '校园地图',
    '校友会', '基金会', '校长信箱', '信息公开',
    '人才招聘', '招聘',
    '出国留学招生', '成人高等教育',
]

# 招生/就业/服务类子项：需更严格判断是否为部门（很多是外部链接或服务门户）
# 这类分类的子项除非名称明显是部门，否则跳过
SERVICE_CATEGORY_PATTERNS = ['招生', '就业', '服务']
# 科研/研究/机构类子项：可宽松判断（子项通常是实验室/项目等）
RESEARCH_CATEGORY_PATTERNS = ['科学', '研究', '机构']

# 首页导航容器选择器（按优先级）
NAV_CONTAINER_SELECTORS = [
    'ul#nav', '#nav ul', '#nav',
    '.nav ul', '.navbar ul', '.menu ul',
    '.wraq_nav ul#nav', '.wraq_nav .nav ul',
    'nav ul', '.main-nav ul', '.site-nav ul',
    '.header-nav ul', '.top-nav ul',
]

# 子菜单容器选择器
SUBMENU_SELECTORS = [
    '.subNav', '.subnav', '.sub-menu', '.submenu',
    '.dropdown-menu', '.dropdown', 'ul.sub', 'ul.child',
    'div > dl', 'div > ul',
]

# 列表页中提取实体的表格选择器
LISTING_TABLE_SELECTORS = [
    'table.gpTable', 'table.wpTable',
    'table[class*="table"]', 'table[class*="list"]',
    '.list-table table', '.content table',
    'table',  # 回退
]

# 列表页实体链接提取的容器（非表格布局）
LISTING_LINK_CONTAINER_SELECTORS = [
    '.subArticleCon a', '.content a', '.main a',
    'ul.list a', 'ul.clearfix a', '.dept-list a',
]

# 列表页中需排除的无关列名（用于跳过表格头）
TABLE_HEADER_TEXTS = {'学院', '系部', '部门', '单位', '机构', '科室', '序号', '名称'}


def _is_specific_article(url: str, text: str) -> bool:
    """判断 URL 是否指向具体文章（非列表/栏目页）"""
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    query = parsed.query

    # 查询参数中包含长哈希或 ID（常见于 CMS 文章详情页 ?id=xxx&hash=xxx）
    if re.search(r'[a-f0-9]{16,}', query, re.IGNORECASE):
        return True
    if re.search(r'\d{6,}', query):
        return True

    # 包含长哈希或数字 ID（常见于文章详情页路径）
    if re.search(r'[a-f0-9]{16,}', path, re.IGNORECASE):
        return True
    if re.search(r'/\d{4,}/\d{4,}', path):  # /2024/12345/
        return True
    if re.search(r'\d{6,}', path):  # 长数字序列
        return True

    # 以 .htm .html .shtml 结尾且路径很深
    if re.search(r'\.\w+$', path) and len(path.split('/')) >= 3:
        # 可能是文章页：/news/sx/xxxxxx.htm
        return True

    # 文本太长（典型导航栏目标题通常较短）
    if len(text) > 24:
        return True

    # URL 包含 Detail、detail、article、Article 等
    if re.search(r'/(detail|article|news)/(?!$)', path, re.IGNORECASE):
        if len(path.split('/')) >= 3:
            return True

    return False


def _is_non_cn_version(url: str) -> bool:
    """判断 URL 是否是非中文版本"""
    path = urlparse(url).path.rstrip('/').lower()
    for pattern in NON_CN_PATTERNS:
        if re.search(pattern, path):
            return True
    return False

# 子部门候选路径模式（在部门页面中寻找）
SUB_DEPT_PATTERNS = [
    r'(tzgg|tongzhi|notice|gonggao|announcement)',  # 通知公告
    r'(xjgl|xueji)',                                  # 学籍管理
    r'(jxyx|jiaoxue)',                                # 教学运行
    r'(sjjx|shijian)',                                # 实践教学
    r'(gjjy|guoji)',                                  # 国际教育
    r'(jxyj|jiaoyan)',                                # 教学研究
    r'(jsfz|jiaoshi)',                                # 教师发展
    r'(zs|zhaosheng)',                                # 招生
    r'(py|peiyang)',                                  # 培养
    r'(xw|xuewei)',                                   # 学位
    r'(jxjy|jixu)',                                   # 继续教育
    r'(xszz|xuesheng)',                               # 学生组织
    r'(zzb|zuzhi)',                                   # 组织
    r'(xcb|xuanchuan)',                               # 宣传
    r'(rsc|renshi)',                                  # 人事
    r'(cwc|caiwu)',                                   # 财务
    r'(hqc|houqin)',                                  # 后勤
    r'(bwc|baowei)',                                  # 保卫
]

# 通知列表页常见的路径后缀
NOTICE_PATH_SUFFIXES = ['tzgg', 'tongzhi', 'notice', 'gonggao', 'announcement', 'news',
                        'xwtz', 'xxgg', 'xygg', 'gg']  # 调研：博达/苏迪校常见栏目路径


def _is_internal_url(url: str, base_domain: str) -> bool:
    """检查 URL 是否属于同一站点"""
    if not url:
        return False
    if url.startswith(('javascript:', 'mailto:', 'tel:', '#', 'data:')):
        return False
    parsed = urlparse(url)
    if not parsed.netloc:
        return True  # 相对路径
    # 同域或同主域名
    base_parts = base_domain.split('.')
    base_main = '.'.join(base_parts[-2:]) if len(base_parts) >= 2 else base_domain
    url_parts = parsed.netloc.split('.')
    url_main = '.'.join(url_parts[-2:]) if len(url_parts) >= 2 else parsed.netloc
    return parsed.netloc == base_domain or url_main == base_main


def _is_likely_section(text: str, url: str) -> bool:
    """判断链接是否可能是一个「栏目/部门」入口（宽松匹配）。"""
    if not text or text in NOISE_TEXTS:
        return False
    if len(text) < 2 or len(text) > 24:
        return False
    # 子串匹配：文本包含任一通用导航关键词
    if any(kw in text for kw in GENERIC_NAV_NAMES if len(kw) >= 2):
        return False
    # 文本包含任一信息页关键词
    if any(kw in text for kw in INFORMATIONAL_PAGE_NAMES if len(kw) >= 2):
        return False

    # 排除非中文版本
    if _is_non_cn_version(url):
        return False

    # 排除具体文章
    if _is_specific_article(url, text):
        return False

    # URL 检查
    path = urlparse(url).path.strip('/')
    if not path:
        return False
    # 排除文件下载
    if re.search(r'\.(pdf|doc|xls|ppt|jpg|png|gif|bmp|css|js|xml|rss)$', path, re.IGNORECASE):
        return False
    # 排除太深的路径（可能是具体文章）
    if len(path.split('/')) > 3:
        return False
    # 排除非部门 URL 模式
    for pattern in NON_DEPT_URL_PATTERNS:
        if re.search(pattern, '/' + path):
            return False

    return True


def _is_non_dept(url: str) -> bool:
    """检查 URL 是否明确不是部门页面（兼容旧接口）"""
    path = urlparse(url).path.rstrip('/')
    for pattern in NON_DEPT_URL_PATTERNS:
        if re.search(pattern, path + '/' if not path.endswith('/') else path):
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# 新增：导航层级解析 + 列表页枚举
# ═══════════════════════════════════════════════════════════════

def _is_dept_like_name(name: str) -> bool:
    """判断名称是否像一个可能有通知公告的部门/院系/实验室"""
    if not name or len(name) < 2:
        return False
    # 包含部门指示词（学院、实验室、研究所等）
    if any(ind in name for ind in DEPT_NAME_INDICATORS):
        return True
    # 排除明确的非部门
    if any(p in name for p in NON_DEPT_CHILD_PATTERNS):
        return False
    return False


def _parse_nav_hierarchy(html: str, base_url: str) -> list:
    """从首页解析导航层级结构

    返回:
        [{name, url, type, children: [{name, url}, ...]}, ...]

        type:
        - 'submenu': 有子菜单的顶级分类（如"科学研究"→ 各实验室）
        - 'single': 单页链接（如"院系设置"→ 列表页）
        - 'external': 外部链接
    """
    soup = BeautifulSoup(html, 'lxml')
    base_domain = urlparse(base_url).netloc

    # 1. 找到主导航容器
    nav_ul = None
    for sel in NAV_CONTAINER_SELECTORS:
        nav_ul = soup.select_one(sel)
        if nav_ul:
            logger.debug(f"  导航容器: {sel}")
            break

    if not nav_ul:
        logger.info("  未找到标准导航容器，尝试通用查找")
        # 回退：找页面中最大的 <ul>（通常是导航）
        uls = soup.select('ul')
        if uls:
            nav_ul = max(uls, key=lambda u: len(u.select('li')))
            if len(nav_ul.select('li')) < 3:
                return []

    if not nav_ul:
        return []

    categories = []
    # 2. 解析顶级 <li>（只处理直接子元素）
    top_lis = nav_ul.select(':scope > li')
    if not top_lis:
        # 有些网站的 ul 中有 div 包裹 li
        top_lis = nav_ul.select('li')
        # 过滤深层嵌套的 li，只保留顶级
        top_lis = [li for li in top_lis
                   if not li.parent or li.parent == nav_ul
                   or (li.parent.name != 'li' and li.parent.parent == nav_ul)]

    for li in top_lis:
        # 获取主链接
        main_a = li.select_one(':scope > a') or li.select_one('a')
        if not main_a:
            continue

        cat_name = main_a.get_text(strip=True)
        cat_href = (main_a.get('href') or '').strip()

        # 跳过空链接和 javascript: 伪链接
        if not cat_name or len(cat_name) < 2:
            continue

        # 跳过明确不相关的分类
        if any(p in cat_name for p in SKIP_CATEGORY_PATTERNS):
            continue

        # 解析 URL
        if cat_href and not cat_href.startswith(('javascript:', '#')):
            cat_url = urljoin(base_url, cat_href)
            if not _is_internal_url(cat_url, base_domain):
                categories.append({
                    'name': cat_name, 'url': cat_url,
                    'type': 'external', 'children': [],
                })
                continue
        else:
            cat_url = base_url  # 无链接，使用首页

        # 3. 查找子菜单
        children = []
        submenu = None
        for sel in SUBMENU_SELECTORS:
            submenu = li.select_one(sel)
            if submenu:
                break

        if submenu:
            for sub_a in submenu.select('a[href]'):
                child_name = sub_a.get_text(strip=True)
                child_href = (sub_a.get('href') or '').strip()

                if not child_name or len(child_name) < 2:
                    continue
                if child_href.startswith(('javascript:', '#', 'tel:', 'mailto:')):
                    if not child_href or child_href in ('javascript:void(0);', 'javascript:;', '#'):
                        continue  # 跳过无链接的占位项

                child_url = urljoin(base_url, child_href)

                # 过滤非中文版本
                if _is_non_cn_version(child_url):
                    continue
                # 过滤文件
                if re.search(r'\.(pdf|doc|xls|ppt|jpg|png)$',
                             urlparse(child_url).path, re.IGNORECASE):
                    continue

                if child_name and child_url:
                    children.append({'name': child_name, 'url': child_url})

        cat_type = 'submenu' if children else 'single'
        categories.append({
            'name': cat_name,
            'url': cat_url,
            'type': cat_type,
            'children': children,
        })

    logger.info(f"  导航解析: {len(categories)} 个顶级分类")
    for cat in categories:
        if cat['children']:
            logger.info(f"    [{cat['type']}] {cat['name']} → {len(cat['children'])} 个子项")
        else:
            logger.info(f"    [{cat['type']}] {cat['name']} → {cat['url']}")

    return categories


def _enumerate_listing_page(listing_url: str, base_url: str, cat_name: str = '') -> list:
    """访问列表页（如院系设置），从表格/列表中提取所有实体链接

    返回: [{'name': str, 'url': str}, ...]
    """
    logger.info(f"  枚举列表页: {cat_name} → {listing_url}")
    try:
        html = _fetch_html(listing_url)
    except Exception as e:
        logger.warning(f"  无法获取列表页: {e}")
        return []

    soup = BeautifulSoup(html, 'lxml')
    base_domain = urlparse(base_url).netloc
    entities = []
    seen_urls = set()

    # 策略 1: 表格提取（院系设置等使用 table 列出所有学院）
    found_in_table = _extract_entities_from_table(soup, listing_url, base_domain, seen_urls)
    entities.extend(found_in_table)

    # 策略 2: 如果没有表格，尝试从列表/链接容器提取
    if not entities:
        for sel in LISTING_LINK_CONTAINER_SELECTORS:
            for a in soup.select(sel):
                name = a.get_text(strip=True)
                href = (a.get('href') or '').strip()
                if not name or not href or len(name) < 4:
                    continue
                if href.startswith(('javascript:', '#', 'mailto:', 'tel:')):
                    continue
                full_url = urljoin(listing_url, href)
                if not _is_internal_url(full_url, base_domain):
                    continue
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)
                if _is_dept_like_name(name):
                    entities.append({'name': name, 'url': full_url})

    # 策略 3: 如果还没找到，提取页面上所有"看起来像部门"的链接
    if not entities:
        for a in soup.select('a[href]'):
            name = a.get_text(strip=True)
            href = (a.get('href') or '').strip()
            if not name or not href or len(name) < 4 or len(name) > 30:
                continue
            if href.startswith(('javascript:', '#', 'mailto:', 'tel:')):
                continue
            full_url = urljoin(listing_url, href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)
            if _is_dept_like_name(name) and _is_internal_url(full_url, base_domain):
                entities.append({'name': name, 'url': full_url})

    logger.info(f"  列表页枚举结果: {len(entities)} 个实体")
    for e in entities:
        logger.info(f"    {e['name']} → {e['url']}")

    return entities


def _extract_entities_from_table(soup, base_url, base_domain, seen_urls):
    """从 HTML 表格中提取实体链接（学院/实验室等）

    只提取第一列（学院/单位名称列）的链接，跳过系部/子部门列。
    支持 rowspan 合并单元格的表格结构。
    """
    entities = []

    for table_sel in LISTING_TABLE_SELECTORS:
        tables = soup.select(table_sel)
        for table in tables:
            rows = table.select('tr')
            if len(rows) < 2:
                continue

            # 检测表头，跳过
            first_row_texts = []
            for th in rows[0].select('th, td'):
                first_row_texts.append(th.get_text(strip=True))
            is_header = any(t in TABLE_HEADER_TEXTS for t in first_row_texts)
            start_row = 1 if is_header else 0

            # 跟踪 rowspan 实体的剩余行数
            # (name, url, remaining_rows)
            rowspan_carrier = None

            for row in rows[start_row:]:
                cells = row.select('td')
                if len(cells) < 1:
                    continue

                # 处理第一列：学院/单位
                if rowspan_carrier is not None:
                    # 上一行的 rowspan 实体延续到本行
                    # 第一列被 rowspan 占据，不处理
                    if rowspan_carrier[2] <= 1:
                        rowspan_carrier = None
                    else:
                        rowspan_carrier = (rowspan_carrier[0], rowspan_carrier[1],
                                           rowspan_carrier[2] - 1)
                else:
                    # 提取第一列的实体链接
                    first_td = cells[0]
                    a_tag = first_td.select_one('a[href]')
                    if a_tag:
                        name = a_tag.get_text(strip=True)
                        href = (a_tag.get('href') or '').strip()

                        if (name and len(name) >= 3
                                and not href.startswith(('javascript:', '#', 'mailto:', 'tel:'))):
                            full_url = urljoin(base_url, href)

                            if (_is_internal_url(full_url, base_domain)
                                    and full_url not in seen_urls
                                    and not _is_non_cn_version(full_url)
                                    and _is_dept_like_name(name)):
                                # 过滤子部门路径（含有 jgsz/xygk/xszx/gzzd 等组织架构子路径）
                                path = urlparse(full_url).path.strip('/')
                                if re.search(r'/(jgsz|xygk|xszx|gzzd|yjszx)/', '/' + path):
                                    continue  # 这是系/子部门详情页，跳过
                                seen_urls.add(full_url)
                                entities.append({'name': name, 'url': full_url})

                    # 检查 rowspan
                    rs = int(first_td.get('rowspan', 1))
                    if rs > 1:
                        rowspan_carrier = (name if a_tag else None,
                                           full_url if a_tag else None,
                                           rs - 1)

            if entities:
                logger.debug(f"  从表格 <{table_sel}> 提取到 {len(entities)} 个实体")
                break  # 只处理第一个有结果的表格

        if entities:
            break

    return entities


def _classify_nav_for_discovery(categories: list, base_url: str) -> dict:
    """将导航分类转化为部门发现计划

    返回:
        {
            'departments': [(name, url, parent_name), ...],  # 待处理的部门
            'listing_pages': [(cat_name, listing_url), ...],  # 需枚举的列表页
        }
    """
    plan = {'departments': [], 'listing_pages': []}

    for cat in categories:
        cat_name = cat['name']
        cat_url = cat['url']

        # 1. 部门容器型（院系设置/组织机构等）→ 需枚举列表页
        if any(p in cat_name for p in DEPT_CONTAINER_PATTERNS):
            if cat['type'] == 'single' and cat_url:
                plan['listing_pages'].append((cat_name, cat_url))
                continue
            elif cat['type'] == 'submenu' and cat['children']:
                # 本身有子菜单的容器（如"科学研究"→ 实验室列表）
                # 子菜单项作为部门
                for child in cat['children']:
                    child_name = child['name']
                    if (not child_name or len(child_name) < 2
                            or any(p in child_name for p in NON_DEPT_CHILD_PATTERNS)):
                        continue
                    # 清空空链接文本
                    if not child_name.strip():
                        continue
                    # 检查是否为有效实体
                    if (_is_dept_like_name(child_name)
                            or any(p in cat_name for p in ['科学', '研究', '招生', '就业', '服务'])):
                        plan['departments'].append(
                            (child_name, child['url'], cat_name))
                continue

        # 2. 子菜单型 → 检查每个子项
        if cat['type'] == 'submenu' and cat['children']:
            is_research = any(p in cat_name for p in RESEARCH_CATEGORY_PATTERNS)
            is_service = any(p in cat_name for p in SERVICE_CATEGORY_PATTERNS)

            for child in cat['children']:
                child_name = child['name']
                if not child_name or len(child_name) < 2:
                    continue
                if any(p in child_name for p in NON_DEPT_CHILD_PATTERNS):
                    continue
                if not child_name.strip():
                    continue

                # 判断子项是否是部门
                if _is_dept_like_name(child_name):
                    # 检查是否为外部链接（不同域名）
                    child_domain = urlparse(child['url']).netloc
                    base_domain = urlparse(base_url).netloc
                    if child_domain and child_domain != base_domain:
                        # 外部链接：只接受子域名
                        base_main = '.'.join(base_domain.split('.')[-2:])
                        child_main = '.'.join(child_domain.split('.')[-2:])
                        if child_main != base_main:
                            logger.debug(f"  跳过外部链接: {child_name} → {child['url']}")
                            continue

                    plan['departments'].append(
                        (child_name, child['url'], cat_name))

                elif is_research:
                    # 科研类分类：子项宽松接受（科研项目、重大平台等）
                    child_domain = urlparse(child['url']).netloc
                    base_domain = urlparse(base_url).netloc
                    if child_domain and child_domain != base_domain:
                        base_main = '.'.join(base_domain.split('.')[-2:])
                        child_main = '.'.join(child_domain.split('.')[-2:])
                        if child_main != base_main:
                            continue
                    plan['departments'].append(
                        (child_name, child['url'], cat_name))

                elif is_service:
                    # 服务类分类：严格判断，只接受有明确部门名称的
                    pass  # 不通过 _is_dept_like_name 的服务类子项，跳过

        # 3. 单页型 → 跳过（会在列表页枚举中处理，或作为回退）

    return plan


# ═══════════════════════════════════════════════════════════════
# 旧版：扁平链接提取（作为回退）
# ═══════════════════════════════════════════════════════════════


def _extract_nav_links(html: str, base_url: str) -> list:
    """从首页提取导航区域中的部门链接

    返回 [(text, url, depth), ...] 列表，depth 为 URL 路径层级
    """
    soup = BeautifulSoup(html, 'lxml')
    base_domain = urlparse(base_url).netloc
    seen_urls = set()
    links = []

    # 常见的导航容器选择器
    nav_selectors = [
        'nav a', '.nav a', '.navbar a', '.c-nav a', '.menu a',
        '.header a', '#header a', '.top a', '#top a',
        '.main-nav a', '.site-nav a', '.navigation a',
        'header a',
        # ZCMS 特有
        '.c-header a', '.c-menu a',
        # 通用：body 下第一个 div 中的链接通常是主导航
    ]

    for selector in nav_selectors:
        try:
            for a in soup.select(selector):
                href = a.get('href', '').strip()
                text = a.get_text(strip=True)

                if not href or not text:
                    continue

                full_url = urljoin(base_url, href)

                if not _is_internal_url(full_url, base_domain):
                    continue
                if _is_non_dept(full_url):
                    continue
                if full_url in seen_urls:
                    continue
                if not _is_likely_section(text, full_url):
                    continue

                seen_urls.add(full_url)

                path = urlparse(full_url).path.strip('/')
                depth = len(path.split('/')) if path else 0

                links.append({
                    'text': text,
                    'url': full_url,
                    'path': path,
                    'depth': depth,
                })
                logger.debug(f"  导航链接: {text} → {full_url} (深度={depth})")
        except Exception:
            continue

    # 如果导航容器没找到足够链接，回退到全页搜索
    if len(links) < 3:
        logger.info("导航容器链接不足，回退到全页搜索")
        # 排除明显的非导航区域
        for tag in soup.select('footer, .footer, .bottom, script, style'):
            tag.decompose()

        for a in soup.select('a[href]'):
            href = a.get('href', '').strip()
            text = a.get_text(strip=True)

            if not href or not text:
                continue
            full_url = urljoin(base_url, href)
            if not _is_internal_url(full_url, base_domain):
                continue
            if not _is_likely_section(text, full_url):
                continue
            if full_url in seen_urls:
                continue

            seen_urls.add(full_url)
            path = urlparse(full_url).path.strip('/')
            depth = len(path.split('/')) if path else 0

            links.append({
                'text': text,
                'url': full_url,
                'path': path,
                'depth': depth,
            })

    return links


def _find_notice_list_url(department_url: str) -> str:
    """在部门页面中寻找通知列表页 URL

    机构主页有时直接就是通知列表，有时通知列表在子路径（如 /tzgg/）下。
    返回最佳的通知列表 URL。

    改进：
    - 优先返回索引页（短路径）而非具体文章页
    - 如果没有找到链接，尝试标准路径（/tzgg/、/notice/ 等）
    - 首页新闻摘要组件（NewsConList等）不能替代完整的 tzgg 列表页
    """
    # 首页摘要型 profile（通常只展示最近几条，非完整通知列表）
    HOMEPAGE_WIDGET_NAMES = {
        'GP CMS 新闻列表 (NewsConList / EventsList)',
        'ZCMS 首页通知 (c-notice)',
    }
    # 通用/回退型 profile（匹配面广但不精确，不应阻止查找专用 tzgg 子页）
    GENERIC_PROFILE_NAMES = {
        '通用列表 (li > a)',
        '通用通知列表 (ul.news-list)',
    }

    try:
        html = _fetch_html(department_url)
    except Exception:
        return department_url  # 回退到部门主页

    soup = BeautifulSoup(html, 'lxml')
    base_path = urlparse(department_url).path.rstrip('/')

    # 先检查当前页面是否有通知列表
    homepage_match_count = 0
    homepage_is_widget = False
    homepage_is_generic = False
    for profile in SELECTOR_PROFILES:
        try:
            items = soup.select(profile['list_selector'])
            if items and len(items) >= 5:
                homepage_match_count = len(items)
                homepage_is_widget = profile['name'] in HOMEPAGE_WIDGET_NAMES
                homepage_is_generic = profile['name'] in GENERIC_PROFILE_NAMES
                break
        except Exception:
            continue

    # 首页有充足列表、非摘要组件、且匹配的是专用选择器 → 直接使用
    # 如果是通用选择器（匹配面广），即使首页列表项多也要检查 tzgg 子页
    # 因为 tzgg 子页可能匹配到更精确的 GP CMS 模板
    if homepage_match_count >= 12 and not homepage_is_widget and not homepage_is_generic:
        logger.debug(f"  当前页面有通知列表 ({homepage_match_count} 项, {profile['name']})")
        return department_url

    # 寻找通知公告子页面链接
    # 收集所有匹配的链接，优先选择索引页
    found_links = []
    for suffix in NOTICE_PATH_SUFFIXES:
        for a in soup.select(f'a[href*="{suffix}"]'):
            href = a.get('href', '').strip()
            if href and not href.startswith(('javascript:', 'mailto:', '#')):
                full_url = urljoin(department_url, href)
                found_links.append(full_url)

    if found_links:
        # 优先选择索引页（短路径、index.*）而非具体文章页
        index_links = [u for u in found_links
                       if re.search(r'(/index\.\w+$|/tzgg/?$|/tzgg\.htm$|/list\.htm$|/notice/?$|/tongzhi/?$)',
                                    urlparse(u).path, re.IGNORECASE)]
        if index_links:
            # 选最短的索引路径
            best = min(index_links, key=lambda u: len(urlparse(u).path))
            logger.debug(f"  发现通知索引页: {best}")
            return best
        # 回退：选最短的链接（避免具体文章页的长路径）
        best = min(found_links, key=lambda u: len(urlparse(u).path))
        logger.debug(f"  发现通知子页(非索引): {best}")
        return best

    # 尝试标准通知路径 — 与首页匹配数比较，选更优的
    # 跟踪每个路径匹配的 profile 类型（专用 vs 通用），专用 profile 优先
    best_tzgg_url = None
    best_tzgg_count = 0
    best_tzgg_is_specific = False  # 是否匹配到 GP CMS / ZCMS 专用选择器
    standard_paths = ['/tzgg/', '/tzgg/index.htm', '/tzgg.htm', '/index/tzgg.htm',
                      '/notice/', '/tongzhi/',
                      '/tzgg/index.shtml', '/notice/index.htm',
                      '/tzgg2/index.htm', '/tzgg3/index.htm',
                      '/news/', '/news/index.htm']
    for sp in standard_paths:
        test_url = base_path.rstrip('/') + sp
        try:
            test_html = _fetch_html(test_url)
            test_soup = BeautifulSoup(test_html, 'lxml')
            for profile in SELECTOR_PROFILES:
                items = test_soup.select(profile['list_selector'])
                if items and len(items) >= 2:
                    is_specific = profile['name'] not in GENERIC_PROFILE_NAMES
                    # 专用选择器优先：即使项数少也优先
                    if is_specific and not best_tzgg_is_specific:
                        best_tzgg_count = len(items)
                        best_tzgg_url = test_url
                        best_tzgg_is_specific = True
                    elif is_specific == best_tzgg_is_specific and len(items) > best_tzgg_count:
                        best_tzgg_count = len(items)
                        best_tzgg_url = test_url
        except Exception:
            continue

    if best_tzgg_url:
        # 决策逻辑：
        # 1. 首页匹配 < 5 → 肯定用 tzgg
        # 2. tzgg 匹配专用选择器 且 首页匹配通用选择器 → 用 tzgg（即使条数少）
        # 3. 两者同类型选择器 → 比条数
        # 4. 首页匹配专用选择器 → 用首页
        if homepage_match_count < 5:
            logger.debug(f"  标准路径更优: {best_tzgg_url} ({best_tzgg_count} 项 vs 首页 {homepage_match_count})")
            return best_tzgg_url
        if best_tzgg_is_specific and homepage_is_generic:
            logger.debug(f"  标准路径选择器更专用: {best_tzgg_url} ({best_tzgg_count} 项 GP CMS vs 首页 {homepage_match_count} 项通用)")
            return best_tzgg_url
        if best_tzgg_is_specific == (not homepage_is_generic) and best_tzgg_count > homepage_match_count:
            logger.debug(f"  标准路径更多: {best_tzgg_url} ({best_tzgg_count} 项 vs 首页 {homepage_match_count})")
            return best_tzgg_url

    # 首页有一定内容 → 使用首页
    if homepage_match_count >= 5:
        logger.debug(f"  使用首页列表 ({homepage_match_count} 项)")
        return department_url

    # 标准路径有一些内容
    if best_tzgg_url:
        logger.debug(f"  标准路径有效: {best_tzgg_url} ({best_tzgg_count} 项)")
        return best_tzgg_url

    return department_url  # 回退


def _discover_sub_departments(dept_url: str, base_url: str) -> list:
    """在部门页面中发现子部门链接

    返回 [(name, url), ...] 列表
    """
    try:
        html = _fetch_html(dept_url)
    except Exception:
        return []

    soup = BeautifulSoup(html, 'lxml')
    base_domain = urlparse(base_url).netloc
    base_path = urlparse(dept_url).path.strip('/')
    sub_depts = []

    # 在侧边栏、子导航中查找
    sidebar_selectors = [
        '.sidebar a', '.side a', '.left-nav a', '.sub-nav a',
        '.c-sidebar a', '.dept-nav a',
        'aside a', '.left-menu a', '.submenu a',
        'ul.sub a', 'ul.child a', '.nav-child a',
    ]

    seen = set()
    for selector in sidebar_selectors:
        try:
            for a in soup.select(selector):
                href = a.get('href', '').strip()
                text = a.get_text(strip=True)

                if not href or not text or len(text) < 2 or len(text) > 20:
                    continue
                if text in NOISE_TEXTS:
                    continue
                if any(kw in text for kw in INFORMATIONAL_PAGE_NAMES if len(kw) >= 2):
                    continue
                if any(kw in text for kw in GENERIC_NAV_NAMES if len(kw) >= 2):
                    continue
                if not _is_internal_url(urljoin(dept_url, href), base_domain):
                    continue

                full_url = urljoin(dept_url, href)
                if full_url in seen:
                    continue
                seen.add(full_url)

                # 确认是子路径（URL 包含父路径）
                child_path = urlparse(full_url).path.strip('/')
                if base_path and child_path.startswith(base_path):
                    sub_depts.append({'text': text, 'url': full_url})
                    logger.debug(f"    子部门: {text} → {full_url}")

        except Exception:
            continue

    # 如果侧边栏没找到，尝试在导航相关容器中找
    if not sub_depts:
        nav_area_selectors = [
            'ul', 'ol', '.nav', '.menu', '.subnav', '.left-menu',
            '.sidebar', '.side', 'aside', 'nav',
        ]
        for area_sel in nav_area_selectors:
            try:
                for area in soup.select(area_sel):
                    for a in area.select('a[href]'):
                        href = a.get('href', '').strip()
                        text = a.get_text(strip=True)
                        if not href or not text or len(text) < 2 or len(text) > 20:
                            continue
                        # 跳过信息页名称和通用导航（子串匹配）
                        if text in NOISE_TEXTS:
                            continue
                        if any(kw in text for kw in INFORMATIONAL_PAGE_NAMES if len(kw) >= 2):
                            continue
                        if any(kw in text for kw in GENERIC_NAV_NAMES if len(kw) >= 2):
                            continue

                        full_url = urljoin(dept_url, href)
                        if full_url in seen:
                            continue
                        if not _is_internal_url(full_url, base_domain):
                            continue

                        child_path = urlparse(full_url).path.strip('/')
                        # 子部门路径应该更深且在父路径下
                        if base_path and child_path.startswith(base_path):
                            child_depth = len(child_path.split('/'))
                            parent_depth = len(base_path.split('/'))
                            if child_depth == parent_depth + 1:
                                seen.add(full_url)
                                sub_depts.append({'text': text, 'url': full_url})
            except Exception:
                continue

    return sub_depts


def _process_one_department(link: dict, school_url: str) -> list:
    """处理单个部门：探测选择器 + 发现子部门（供并行调用）

    返回该部门及其子部门的配置列表
    """
    dept_name = link['text']
    dept_url = link['url']
    group_name = link.get('group_name', '')
    configs = []

    logger.info(f"阶段2: 处理部门 「{dept_name}」 {dept_url}")

    # 找到真正的通知列表页
    list_url = _find_notice_list_url(dept_url)
    if list_url != dept_url:
        logger.info(f"  通知列表页: {list_url}")

    # 探测选择器
    try:
        list_html = _fetch_html(list_url)
    except Exception:
        logger.warning(f"  无法获取列表页: {list_url}，跳过")
        return configs

    soup = BeautifulSoup(list_html, 'lxml')
    matched_profile = None

    for profile in SELECTOR_PROFILES:
        try:
            items = soup.select(profile['list_selector'])
            if items and len(items) >= 2:
                matched_profile = profile
                logger.info(f"  匹配选择器: {profile['name']} ({len(items)} 项)")
                break
        except Exception:
            continue

    if not matched_profile:
        logger.info(f"  未匹配精确选择器，使用回退模式")
        matched_profile = {
            'name': '回退',
            'list_selector': 'a[href]',
            'title_selector': '',
            'link_selector': '',
            'date_selector': '',
            'content_selector': '',
        }

    # 创建部门配置
    dept_config = {
        'name': dept_name,
        'list_url': list_url,
        'list_selector': matched_profile.get('list_selector', ''),
        'title_selector': matched_profile.get('title_selector', ''),
        'link_selector': matched_profile.get('link_selector', ''),
        'date_selector': matched_profile.get('date_selector', ''),
        'content_selector': matched_profile.get('content_selector', ''),
        'parent_name': None,
        'group_name': group_name,  # 导航分类（院系设置/科学研究等）
    }
    configs.append(dept_config)

    # ---- 发现子部门 ----
    sub_depts = _discover_sub_departments(dept_url, school_url)
    if sub_depts:
        logger.info(f"  发现 {len(sub_depts)} 个子部门")

    for sub in sub_depts[:MAX_SUB_DEPTS]:
        sub_url = sub['url']
        sub_name = sub['text']

        # 跳过无意义的子部门名（子串匹配）
        if any(kw in sub_name for kw in GENERIC_NAV_NAMES if len(kw) >= 2):
            continue
        if sub_name in NOISE_TEXTS:
            continue
        if any(kw in sub_name for kw in ('首页', 'Home', 'English', '网站首页')):
            continue
        if _is_non_cn_version(sub_url):
            continue
        if any(kw in sub_name for kw in INFORMATIONAL_PAGE_NAMES if len(kw) >= 2):
            continue

        # 跳过 URL 与 parent 的 list_url 相同的子部门（重复）
        sub_list_url_candidate = _find_notice_list_url(sub_url)
        if sub_list_url_candidate.rstrip('/') == list_url.rstrip('/'):
            continue

        full_sub_name = f"{dept_name}-{sub_name}"
        logger.info(f"  子部门: {full_sub_name} → {sub_url}")

        # 探测子部门选择器
        try:
            sub_html = _fetch_html(sub_list_url_candidate)
        except Exception:
            logger.warning(f"    无法获取子部门列表页: {sub_list_url_candidate}")
            continue

        sub_soup = BeautifulSoup(sub_html, 'lxml')
        sub_profile = None
        sub_item_count = 0

        for profile in SELECTOR_PROFILES:
            try:
                items = sub_soup.select(profile['list_selector'])
                if items and len(items) >= 2:
                    sub_profile = profile
                    sub_item_count = len(items)
                    break
            except Exception:
                continue

        if not sub_profile:
            fallback_items = sub_soup.select('a[href]')
            if len(fallback_items) < 5:
                logger.info(f"    跳过（页面链接太少，可能不是列表页）")
                continue
            sub_profile = {
                'name': '回退',
                'list_selector': 'a[href]',
                'title_selector': '',
                'link_selector': '',
                'date_selector': '',
                'content_selector': '',
            }
        elif sub_item_count < 3:
            logger.info(f"    跳过（仅 {sub_item_count} 项匹配，可能不是通知列表页）")
            continue

        sub_config = {
            'name': full_sub_name,
            'list_url': sub_list_url_candidate,
            'list_selector': sub_profile.get('list_selector', ''),
            'title_selector': sub_profile.get('title_selector', ''),
            'link_selector': sub_profile.get('link_selector', ''),
            'date_selector': sub_profile.get('date_selector', ''),
            'content_selector': sub_profile.get('content_selector', ''),
            'parent_name': dept_name,
            'group_name': group_name,  # 继承父部门的导航分类
        }
        configs.append(sub_config)

    return configs


def discover_school_departments(school_url: str) -> list:
    """从学校首页智能发现所有部门及其子部门（支持导航层级 + 列表页枚举）

    策略（改进版）：
    - 阶段 0: 解析首页导航层级 → 识别分类
    - 阶段 1: 按分类枚举部门
        - 列表页型（院系设置 → 表格）→ 访问列表页提取实体
        - 子菜单型（科学研究 → 实验室）→ 子项即部门
        - 单页型 → 旧版扁平链接提取
    - 阶段 2: 并行访问各部门 → 探测选择器 + 发现子部门

    返回部门配置列表，每项含:
        name, list_url, list_selector, title_selector, link_selector,
        date_selector, content_selector, parent_name (可选)

    parent_name 非空表示该部门是子部门（名称已包含父前缀，如"院系设置-地球科学学院"）
    """
    logger.info(f"开始智能发现学校部门结构: {school_url}")

    try:
        html = _fetch_html(school_url)
    except Exception as e:
        logger.error(f"无法获取首页: {school_url} → {e}")
        return []

    # ---- 阶段 0：解析导航层级 ----
    nav_categories = _parse_nav_hierarchy(html, school_url)

    # ---- 阶段 1：按分类枚举部门 ----
    all_candidates = []  # [(name, url, parent_category_name, group_name), ...]

    if nav_categories:
        # ---- 新策略：基于导航层级 ----
        plan = _classify_nav_for_discovery(nav_categories, school_url)

        # 1a. 列表页枚举（院系设置等）
        for cat_name, listing_url in plan['listing_pages']:
            entities = _enumerate_listing_page(listing_url, school_url, cat_name)
            for ent in entities:
                # group_name = 列表页分类名（如"院系设置"）
                all_candidates.append((ent['name'], ent['url'], cat_name, cat_name))
            if entities:
                logger.info(f"  列表页 [{cat_name}]: 枚举到 {len(entities)} 个部门")

        # 1b. 导航子菜单直接作为部门
        for dept_name, dept_url, parent_name in plan['departments']:
            # group_name = 父级导航分类（如"科学研究"）
            all_candidates.append((dept_name, dept_url, parent_name, parent_name))
        if plan['departments']:
            logger.info(f"  子菜单型: {len(plan['departments'])} 个部门")

        # 1c. 如果新策略没有找到任何部门，回退到旧版扁平链接提取
        if not all_candidates:
            logger.info("  导航层级策略未发现部门，回退到扁平链接提取")
            nav_links = _extract_nav_links(html, school_url)
            # 去重
            seen_paths = set()
            for link in nav_links:
                norm_path = link['path'].rstrip('/').lower()
                if norm_path not in seen_paths:
                    seen_paths.add(norm_path)
                    all_candidates.append((link['text'], link['url'], None, ''))
    else:
        # 无导航层级 → 回退到旧版
        logger.info("  无法解析导航层级，使用扁平链接提取")
        nav_links = _extract_nav_links(html, school_url)
        seen_paths = set()
        for link in nav_links:
            norm_path = link['path'].rstrip('/').lower()
            if norm_path not in seen_paths:
                seen_paths.add(norm_path)
                all_candidates.append((link['text'], link['url'], None, ''))

    if not all_candidates:
        logger.warning("未发现任何部门链接")
        return []

    # 过滤非中文版本 + 限制数量
    filtered = [(name, url, pn, gn) for name, url, pn, gn in all_candidates[:MAX_TOP_DEPTS]
                if not _is_non_cn_version(url)]
    logger.info(f"阶段1: {len(filtered)} 个候选部门待处理")

    # ---- 阶段 2：并行访问各部门 + 探测选择器 + 发现子部门 ----
    # 转换为 _process_one_department 需要的格式
    candidate_links = [
        {'text': name, 'url': url, 'path': urlparse(url).path.strip('/'),
         'parent_category': pn, 'group_name': gn}
        for name, url, pn, gn in filtered
    ]

    # 去重（相同 URL 只保留一个）
    seen_urls = set()
    unique_candidates = []
    for link in candidate_links:
        norm_url = link['url'].rstrip('/').lower()
        if norm_url not in seen_urls:
            seen_urls.add(norm_url)
            unique_candidates.append(link)

    logger.info(f"去重后: {len(unique_candidates)} 个唯一候选")
    for c in unique_candidates:
        logger.info(f"  {c['text']} → {c['url']}")

    discovered = []

    if len(unique_candidates) <= 2:
        for link in unique_candidates:
            configs = _process_one_department(link, school_url)
            discovered.extend(configs)
    else:
        logger.info(f"阶段2: 并行处理 {len(unique_candidates)} 个部门 (MAX_WORKERS={MAX_WORKERS})")
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(unique_candidates))) as executor:
            futures = {executor.submit(_process_one_department, link, school_url): link
                       for link in unique_candidates}
            for future in as_completed(futures):
                try:
                    configs = future.result()
                    discovered.extend(configs)
                except Exception as e:
                    link = futures[future]
                    logger.warning(f"  处理部门失败 [{link['text']}]: {e}")

    logger.info(f"发现完成: 共 {len(discovered)} 个部门（含子部门）")
    return discovered


def apply_discovered_departments(school_id: int, dept_configs: list) -> int:
    """将发现的部门配置写入数据库

    - 跳过已存在的同名部门（by school_id + name）
    - 返回新创建的部门数量
    """
    created = 0
    for cfg in dept_configs:
        existing = Department.query.filter_by(
            school_id=school_id, name=cfg['name']
        ).first()
        if existing:
            logger.debug(f"  部门已存在: {cfg['name']}，跳过")
            continue

        dept = Department(
            school_id=school_id,
            name=cfg['name'],
            list_url=cfg['list_url'],
            list_selector=cfg['list_selector'],
            title_selector=cfg['title_selector'],
            link_selector=cfg['link_selector'],
            date_selector=cfg['date_selector'],
            content_selector=cfg['content_selector'],
            group_name=cfg.get('group_name', ''),
        )
        db.session.add(dept)
        created += 1
        logger.info(f"  创建部门: {cfg['name']} (list_url={cfg['list_url']})")

    if created > 0:
        db.session.commit()
        logger.info(f"已创建 {created} 个新部门")

    return created
