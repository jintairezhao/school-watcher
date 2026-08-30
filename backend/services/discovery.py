"""站点发现编排逻辑（从原 app.py 抽出）"""
import logging

from flask import current_app

logger = logging.getLogger(__name__)


def is_entity_likely_dept(name, url=''):
    """判断枚举出的实体是否像一个部门/学院（而非新闻文章或通用页面）。"""
    if not name or len(name) < 2:
        return False

    # 「父-子」形式（如「教务部-国际教育」）：用子名（后缀）判断，长度上限放宽
    check_name = name.split('-', 1)[1] if '-' in name else name
    if len(check_name) > 20 or len(name) > 30:
        return False

    # 排除纯新闻标题特征（长句子、含标点）
    if any(p in name for p in ['：', '？', '！', '——', '…', '"', '"', '（', '）']):
        if len(name) > 10:
            return False
    # 排除明显的非部门页面
    skip_if_exact = {'首页', 'English', '登录', '注册', '更多', '详细', '查看详情',
                     '返回首页', '上一篇', '下一篇', '当前位置', '网站地图'}
    if name.strip() in skip_if_exact:
        return False
    # 必须包含部门/机构指示词或较短（部门名通常 <= 12字）
    from backend.scraper.detectors.nav_parser import _is_dept_like_name
    return _is_dept_like_name(check_name) or len(check_name) <= 12


def _org_name_score(name):
    """名称是否像组织机构单位（导航「本科生」与组织机构表「教务部」可能同站）。"""
    return 1 if name and name[-1] in ('部', '处', '室', '院', '系') else 0


def prioritize_candidates(candidates, max_count=40):
    """对候选部门进行优先级排序和截断。

    优先保留：名称含部门关键词的、URL 含 tzgg/notice 的、名称短的（更像部门名）。
    """
    def priority(c):
        score = 0
        name = c.get('name', '')
        url = c.get('url', '')
        from backend.scraper.detectors.nav_parser import _is_dept_like_name
        if _is_dept_like_name(name):
            score += 10
        if '-' in name:  # 组织机构表的子单位/内部栏目，保住父子层级
            score += 4
        if any(kw in url.lower() for kw in ['tzgg', 'notice', 'tongzhi', 'xytz', 'gg']):
            score += 5
        if len(name) <= 8:
            score += 3
        elif len(name) <= 12:
            score += 1
        if c.get('group_name'):
            score += 2
        return score

    candidates.sort(key=priority, reverse=True)
    return candidates[:max_count]


def run_discovery_in_background(school, session_obj, app=None):
    """后台执行站点发现流程。"""
    from backend.scraper.engine import _fetch_html, SELECTOR_PROFILES
    from backend.scraper.detectors.nav_parser import (parse_school_navigation,
                                                      enumerate_listing_page,
                                                      enumerate_subsite_columns,
                                                      find_dept_notice_url)
    from backend.scraper.detectors.list_detector import detect_notice_list
    from backend.scraper.detectors.content_extractor import extract_article_content
    from backend.scraper.selector.selector_store import build_profile_from_knowledge

    # 后台线程没有请求上下文，current_app 在新线程里不可用（旧版在这里崩溃，
    # 发现会话永远卡在 started）——app 实例必须由路由传入（同 scrape.py 的做法）
    try:
        if app is None:
            app = current_app._get_current_object()
    except RuntimeError:
        session_obj.fail('无法启动发现：缺少应用上下文')
        return
    app.app_context().push()

    try:
        # Phase 0: 获取首页
        session_obj.set_phase('fetching_homepage', '正在获取学校首页...', 5)
        html = _fetch_html(school.url)
        if not html:
            session_obj.fail('无法访问学校首页，请检查URL')
            return

        # Phase 1: 解析导航
        session_obj.set_phase('parsing_navigation', '正在分析导航结构...', 15)
        nav_result = parse_school_navigation(html, school.url)

        # 收集候选部门
        candidates = []
        enumerated_listing_urls = set()  # 防止同名 listing 页重复枚举

        for cat in nav_result['categories']:
            if cat['type'] == 'listing':
                # 去重：同一 URL（不含 fragment）的汇总页只枚举一次
                listing_base_url = cat['url'].split('#')[0]
                if listing_base_url in enumerated_listing_urls:
                    continue
                enumerated_listing_urls.add(listing_base_url)

                # 访问汇总页枚举
                session_obj.set_phase('enumerating', f'正在访问: {cat["name"]}...', 25)
                try:
                    listing_html = _fetch_html(cat['url'])
                    if listing_html:
                        entities = enumerate_listing_page(listing_html, cat['url'])
                        for entity in entities:
                            name = entity.get('name', '')
                            url = entity.get('url', '')
                            if len(name) <= 20 and is_entity_likely_dept(name, url):
                                entity['group_name'] = cat['name']
                                candidates.append(entity)
                except Exception as e:
                    logger.warning(f"枚举汇总页失败 {cat['url']}: {e}")

            elif cat['type'] == 'submenu':
                group_name = cat['name']
                for child in cat['children']:
                    if child.get('url'):
                        candidates.append({
                            'name': child['name'],
                            'url': child['url'],
                            'group_name': group_name,
                        })

            elif cat['type'] == 'department':
                candidates.append({
                    'name': cat['name'],
                    'url': cat['url'],
                    'group_name': '',
                })

        # 去重（URL 去掉 fragment 后比较）；同 URL 冲突时保留更像组织机构的
        # 名称（导航「本科生」与组织机构表「教务部」同站，应保留后者）
        seen = set()
        unique_candidates = []
        by_key = {}
        for c in candidates:
            dedup_url = c['url'].split('#')[0].rstrip('/')
            if dedup_url not in seen:
                seen.add(dedup_url)
                by_key[dedup_url] = c
                unique_candidates.append(c)
            elif _org_name_score(c['name']) > _org_name_score(by_key[dedup_url]['name']):
                old = by_key[dedup_url]
                unique_candidates[unique_candidates.index(old)] = c
                by_key[dedup_url] = c

        # 限制候选数量，优先保留有意义的部门
        unique_candidates = prioritize_candidates(unique_candidates, max_count=60)

        session_obj.total_departments = len(unique_candidates)
        session_obj.set_phase('discovering_departments',
                              f'发现 {len(unique_candidates)} 个候选部门，正在探测...', 30)

        # Phase 2: 逐部门探测
        # 构建扩展 profiles: 已知 + 已学习
        extended_profiles = list(SELECTOR_PROFILES)

        domain = ''
        try:
            from urllib.parse import urlparse
            domain = urlparse(school.url).netloc
        except Exception:
            pass
        extended_profiles.extend(build_profile_from_knowledge(school.id, domain))

        discovered = []
        seen_list_urls = {}  # 解析后的列表页去重（多栏目同页时只保留首个）
        for i, candidate in enumerate(unique_candidates):
            progress = 30 + int(55 * (i + 1) / max(len(unique_candidates), 1))
            session_obj.set_phase('probing_selectors',
                                  f'正在探测 [{i+1}/{len(unique_candidates)}]: {candidate["name"]}',
                                  progress)

            try:
                dept_html = _fetch_html(candidate['url'])
                if not dept_html:
                    session_obj.add_skipped(candidate['name'], '页面无法访问')
                    continue

                # 找通知列表子页
                notice_url_result = find_dept_notice_url(dept_html, candidate['url'])
                if notice_url_result:
                    list_url = notice_url_result['url']
                    list_html = _fetch_html(list_url)
                else:
                    list_url = candidate['url']
                    list_html = dept_html

                if not list_html:
                    session_obj.add_skipped(candidate['name'], '无法获取列表页')
                    continue

                # 检测通知列表
                list_result = detect_notice_list(list_html, list_url,
                                                 existing_profiles=extended_profiles)
                # 回退：link_scan 指到的子页检测失败时，回部门首页本身再检测
                # （党群工作部这类：link_scan 指向综合新闻页，首页才是合格列表）
                if (not list_result or list_result['confidence'] < 0.4) \
                        and list_url != candidate['url']:
                    home_result = detect_notice_list(dept_html, candidate['url'],
                                                     existing_profiles=extended_profiles)
                    if home_result and home_result['confidence'] >= 0.4:
                        list_url, list_html = candidate['url'], dept_html
                        list_result = home_result
                if not list_result:
                    session_obj.add_skipped(candidate['name'], '未检测到通知列表 (置信度过低)')
                    continue

                if list_result['confidence'] < 0.4:
                    session_obj.add_skipped(candidate['name'],
                                            f'置信度过低 ({list_result["confidence"]})')
                    continue

                # 按解析结果去重：多个栏目/链接解析到同一列表页时只保留第一个
                # （如新能源与材料学院的学生工作/党建工作同指一个 tzgg 页，
                # 候选 URL 不同但解析结果相同，避免冗余子部门）
                list_key = list_url.split('#')[0].rstrip('/')
                if list_key in seen_list_urls:
                    session_obj.add_skipped(candidate['name'],
                                            f'与「{seen_list_urls[list_key]}」列表页相同，去重')
                    continue
                seen_list_urls[list_key] = candidate['name']

                content_selector = list_result.get('content_selector',
                                                   'div.article-content, div.content, div.main, article')
                content_method = 'list_default'
                # 尝试从第一条通知的详情页提取正文选择器
                sample_items = list_result.get('sample_titles', [])
                if sample_items and list_html:
                    from bs4 import BeautifulSoup
                    from urllib.parse import urljoin as _urljoin
                    soup = BeautifulSoup(list_html, 'lxml')
                    first_link = soup.select_one(list_result['list_selector'] + ' a[href]')
                    if first_link:
                        detail_href = first_link.get('href', '')
                        if detail_href:
                            detail_url = _urljoin(list_url, detail_href)
                            try:
                                detail_html = _fetch_html(detail_url)
                                if detail_html:
                                    content_result = extract_article_content(
                                        detail_html, detail_url
                                    )
                                    if content_result['confidence'] > 0.4:
                                        content_selector = content_result['selector_used']
                                        content_method = content_result['method']
                            except Exception:
                                pass

                dept_info = {
                    'name': candidate['name'],
                    'list_url': list_url,
                    'list_selector': list_result['list_selector'],
                    'title_selector': list_result['title_selector'],
                    'link_selector': list_result['link_selector'],
                    'date_selector': list_result['date_selector'],
                    'content_selector': content_selector,
                    'group_name': candidate.get('group_name', ''),
                    'confidence': list_result['confidence'],
                    'item_count': list_result['item_count'],
                    'method': list_result['method'],
                    'sample_titles': sample_items[:3],
                }

                discovered.append(dept_info)
                session_obj.add_found(dept_info)

                # 内部栏目扩展：父部门探测成功后，把其子站首页上的其他栏目
                # 生成「父-子」候选追加进探测队列（for 遍历同一列表，新项
                # 也会被探测）。总量封顶防止失控。
                if '-' not in candidate['name'] and len(unique_candidates) < 90:
                    try:
                        for child in enumerate_subsite_columns(
                                dept_html, candidate['url'], candidate['name']):
                            child_key = child['url'].split('#')[0].rstrip('/')
                            if child_key in seen:
                                continue
                            seen.add(child_key)
                            child['group_name'] = candidate.get('group_name', '')
                            unique_candidates.append(child)
                            session_obj.total_departments = len(unique_candidates)
                    except Exception as e:
                        logger.debug(f"栏目扩展失败 {candidate['name']}: {e}")

            except Exception as e:
                logger.warning(f"探测部门失败 {candidate['name']}: {e}")
                session_obj.add_skipped(candidate['name'], f'探测异常: {str(e)[:50]}')

        # Phase 3: 完成
        session_obj.set_phase('completed',
                              f'发现完成: {len(discovered)} 个候选部门',
                              100)

        session_obj.complete({
            'total_candidates': len(unique_candidates),
            'discovered': len(discovered),
            'skipped': len(session_obj.skipped_departments),
            'departments': discovered,
        })

    except Exception as e:
        logger.error(f"站点发现失败: {e}", exc_info=True)
        session_obj.fail(f'发现失败: {str(e)[:100]}')
