"""选择器学习与持久化

将成功匹配的 CSS 选择器持久化到数据库，形成"域名→选择器"知识库。
同一域名的页面可以直接复用已学习的模式，无需重复分析。

🆕 第十七轮：集成 Scrapling 自适应签名
- save_element_signatures(): 保存元素 DOM 签名，用于未来自愈
- auto_heal_selectors(): 选择器失效时自动重新定位元素
- generate_robust_selectors(): 从元素生成更稳健的 CSS 选择器
"""

import json
import logging
import os
from datetime import datetime, timezone

from backend.database.db import db
from backend.database.models import School

logger = logging.getLogger(__name__)

# ============================================================
# Scrapling 自适应集成 — 元素签名存储
# ============================================================

# 签名存储目录
from backend.core.config import DATA_DIR
SIGNATURES_DIR = str(DATA_DIR / 'scrapling_sigs')


def _get_signatures_db_path(school_id):
    """获取学校对应的 Scrapling 签名数据库路径。"""
    os.makedirs(SIGNATURES_DIR, exist_ok=True)
    return os.path.join(SIGNATURES_DIR, f'school_{school_id}_sigs.db')


def save_element_signatures(html, school_id, url, selectors_dict):
    """使用 Scrapling 保存已匹配元素的 DOM 签名。

    当 CSS 选择器因页面改版而失效时，可用保存的签名重新定位
    元素，实现「选择器自愈」。

    Args:
        html: 页面 HTML
        school_id: 学校 ID
        url: 页面 URL
        selectors_dict: {list_selector, title_selector, date_selector, ...}

    Returns:
        bool: 是否成功保存
    """
    if os.environ.get('WATCHER_ADAPTIVE') != '1':
        return False
    try:
        from scrapling.parser import Selector
        from scrapling.core.storage import SQLiteStorageSystem

        db_path = _get_signatures_db_path(school_id)
        page = Selector(html, url=url, adaptive=True, storage=SQLiteStorageSystem,
                        storage_args={'storage_file': db_path, 'url': url})

        list_sel = selectors_dict.get('list_selector', '')
        if not list_sel:
            return False

        items = page.css(list_sel)
        if len(items) < 3:
            logger.debug(f"[签名保存] 选择器 {list_sel} 仅匹配 {len(items)} 项，跳过")
            return False

        # 保存列表容器自身的签名（用于定位列表区域）
        try:
            parent_el = items[0].parent
            if parent_el:
                parent_el.save(parent_el, f'list-container')
        except Exception:
            pass

        # 保存前 5 个列表项的元素签名
        for i, item in enumerate(items[:5]):
            try:
                item.save(item, f'list-item-{i}')
            except Exception as e:
                logger.debug(f"[签名保存] 保存 item-{i} 失败: {e}")

        # 保存标题/日期/链接子元素的签名（用于精确恢复）
        title_sel = selectors_dict.get('title_selector', '')
        date_sel = selectors_dict.get('date_selector', '')
        link_sel = selectors_dict.get('link_selector', '')

        if title_sel:
            try:
                title_els = page.css(title_sel)
                if title_els:
                    title_els[0].save(title_els[0], 'title-sample')
            except Exception:
                pass

        if date_sel:
            try:
                date_els = page.css(date_sel)
                if date_els:
                    date_els[0].save(date_els[0], 'date-sample')
            except Exception:
                pass

        # 🆕 同时保存文本指纹到 school.config（Scrapling 存储外的冗余备份）
        _save_text_fingerprints_to_config(school_id, url, items[:5])

        logger.info(f"[签名保存] school={school_id}, {len(items[:5])} 项签名已保存到 {db_path}")
        return True

    except Exception as e:
        logger.warning(f"[签名保存] 失败: {e}")
        return False


def _save_text_fingerprints_to_config(school_id, url, items):
    """将列表项的文本指纹保存到 school.config JSON（Scrapling 存储外的备份）。

    当 Scrapling 的 SQLite 存储出现编码问题或丢失时，
    可通过这些文本指纹在修改后的页面中重新定位元素。
    """
    try:
        school = School.query.get(school_id)
        if not school:
            return

        config = {}
        if school.config:
            try:
                config = json.loads(school.config)
            except json.JSONDecodeError:
                config = {}

        # 提取每个列表项中前 60 字符的可见文本
        fingerprints = []
        for item in items:
            try:
                text = item.get_all_text() if hasattr(item, 'get_all_text') else item.text
                if text:
                    # 取前60字符作为指纹
                    clean = text.strip()[:60]
                    if len(clean) >= 8:
                        fingerprints.append(clean)
            except Exception:
                pass

        if fingerprints:
            sigs = config.get('element_fingerprints', [])
            # 去重（基于 URL）
            sigs = [s for s in sigs if s.get('url') != url]
            sigs.append({
                'url': url,
                'fingerprints': fingerprints,
                'saved_at': datetime.now(timezone.utc).isoformat(),
            })
            # 限制数量
            config['element_fingerprints'] = sigs[-20:]
            school.config = json.dumps(config, ensure_ascii=False)
            db.session.commit()
            logger.debug(f"[文本指纹] 保存了 {len(fingerprints)} 条指纹到 school.config")

    except Exception as e:
        logger.debug(f"[文本指纹] 保存失败: {e}")


def _get_text_fingerprints(school_id, url=''):
    """从 school.config 读取文本指纹备份。

    当 Scrapling SQLite 存储不可用时，用这些指纹恢复。
    """
    try:
        school = School.query.get(school_id)
        if not school or not school.config:
            return []

        config = json.loads(school.config)
        sigs = config.get('element_fingerprints', [])

        # 优先返回匹配当前 URL 的指纹
        for s in sigs:
            if s.get('url') == url:
                return s.get('fingerprints', [])

        # 回退：返回最近的指纹
        if sigs:
            return sigs[-1].get('fingerprints', [])

        return []
    except Exception:
        return []


def auto_heal_selectors(html, school_id, url, department_name=''):
    """尝试自愈失效的选择器。

    当选择器返回 0 结果时，用保存的元素签名在修改后的页面中
    重新定位元素，并生成新的 CSS 选择器。

    Args:
        html: 修改后的页面 HTML
        school_id: 学校 ID
        url: 页面 URL
        department_name: 部门名称（用于日志）

    Returns:
        dict or None: 修复后的 selectors_dict，或 None（无法自愈）
    """
    if os.environ.get('WATCHER_ADAPTIVE') != '1':
        return None
    try:
        from scrapling.parser import Selector
        from scrapling.core.storage import SQLiteStorageSystem

        db_path = _get_signatures_db_path(school_id)
        if not os.path.exists(db_path):
            logger.debug(f"[选择器自愈] 无签名数据库: {db_path}")
            return None

        page = Selector(html, url=url, adaptive=True, storage=SQLiteStorageSystem,
                        storage_args={'storage_file': db_path, 'url': url})

        # 策略 1: 用保存的元素文本特征在内容区域中搜索（排除导航/页脚）
        healed_items = []
        for i in range(5):
            try:
                saved = page.retrieve(f'list-item-{i}')
                if not saved or not isinstance(saved, dict):
                    continue

                text_hint = saved.get('text', '').strip()
                if not text_hint or len(text_hint) < 6:
                    continue

                # 只在内容区域搜索（排除 header/nav/footer）
                found_in_content = []
                for content_sel in ['main', 'article', 'div.content', 'div.main', 'div.c-main__right',
                                     'div[class*=\"content\"]', 'div[class*=\"main\"]']:
                    content_areas = page.css(content_sel)
                    for area in content_areas:
                        found = area.find_by_text(text_hint[:30])
                        if found:
                            found_in_content.extend(found)
                            break
                    if found_in_content:
                        break

                # 如果内容区域没找到，全页搜索
                if not found_in_content:
                    found = page.find_by_text(text_hint[:30])
                    if found:
                        found_in_content = found

                if found_in_content:
                    healed_items.append(found_in_content[0])
                    logger.debug(f"[选择器自愈] item-{i} 通过文本匹配恢复: {text_hint[:30]}")
                    continue

                # 用文本正则匹配（部分匹配）
                import re as re_mod
                try:
                    text_escaped = re_mod.escape(text_hint[6:30])
                    found_re = page.find_by_regex(text_escaped)
                    if found_re:
                        healed_items.append(found_re[0])
                        logger.debug(f"[选择器自愈] item-{i} 通过正则匹配恢复")
                        continue
                except Exception:
                    pass

            except Exception as e:
                logger.debug(f"[选择器自愈] item-{i} 恢复失败: {e}")

        # 策略 2: 如果文本匹配失败，尝试通过已知的 DOM 结构特征搜索
        if not healed_items:
            try:
                saved_container = page.retrieve('list-container')
                if saved_container and isinstance(saved_container, dict):
                    container_tag = saved_container.get('tag', 'div')
                    # 在新页面中找类似标签的容器
                    all_containers = page.css(container_tag)
                    # 找包含多个链接的容器
                    for c in all_containers:
                        try:
                            links = c.css('a[href*="htm"]')
                            if len(links) >= 3:
                                # 可能是通知列表容器
                                items_in = c.css('li') or c.css(container_tag + ' > *')
                                if len(items_in) >= 3:
                                    healed_items = items_in[:5]
                                    logger.debug(f"[选择器自愈] 通过容器结构恢复: {len(healed_items)} 项")
                                    break
                        except Exception:
                            continue
            except Exception as e:
                logger.debug(f"[选择器自愈] 结构恢复失败: {e}")

        # 策略 3: 用 school.config 中保存的文本指纹搜索
        if not healed_items:
            try:
                fingerprints = _get_text_fingerprints(school_id, url)
                if fingerprints:
                    for fp in fingerprints[:5]:
                        text_snippet = fp[:30] if len(fp) >= 8 else fp
                        found = page.find_by_text(text_snippet)
                        if found:
                            healed_items.append(found[0])
                            logger.debug(f"[选择器自愈] 通过文本指纹恢复: {text_snippet[:20]}")
                        if len(healed_items) >= 3:
                            break
            except Exception as e:
                logger.debug(f"[选择器自愈] 指纹恢复失败: {e}")

        if not healed_items:
            logger.info(f"[选择器自愈] [{department_name}] 无法恢复元素，需要重新发现")
            return None

        logger.info(f"[选择器自愈] [{department_name}] 恢复了 {len(healed_items)} 个元素")

        # 从恢复的元素生成新的选择器
        result = _generate_selectors_from_elements(healed_items, page, url)
        if not result:
            return None

        # 验证：生成的选择器确实匹配通知列表（不是导航栏）
        try:
            test_items = page.css(result['list_selector'])
            if len(test_items) < 3:
                logger.warning(f"[选择器自愈] 验证失败：选择器仅匹配 {len(test_items)} 项")
                return None
            if len(test_items) > 200:
                logger.warning(f"[选择器自愈] 验证失败：选择器匹配过多项 ({len(test_items)})，可能是导航栏")
                return None

            # 检查日期比例（通知列表的核心特征，来自 dom_analyzer 的教训 7.46）
            date_count = 0
            link_count = 0
            for item in test_items[:10]:
                text = item.text if hasattr(item, 'text') else ''
                # 用简单正则检测日期
                import re as _re
                if _re.search(r'\d{4}[-/年]\d{1,2}[-/月]\d{1,2}', text) or \
                   _re.search(r'\d{1,2}[-/]\d{1,2}[-/]\d{4}', text) or \
                   _re.search(r'\d{2,4}\.\d{1,2}\.\d{1,2}', text):
                    date_count += 1
                if item.css('a'):
                    link_count += 1

            date_ratio = date_count / len(test_items[:10]) if test_items[:10] else 0
            link_ratio = link_count / len(test_items[:10]) if test_items[:10] else 0

            # 通知列表应有较高的日期比例（≥30%）和链接比例（≥50%）
            if date_ratio < 0.3 and link_ratio < 0.5:
                logger.warning(
                    f"[选择器自愈] 验证失败：日期比例 {date_ratio:.1%}, 链接比例 {link_ratio:.1%} — 可能是导航栏"
                )
                return None

            result['confidence'] = min(0.9, 0.5 + len(test_items) * 0.02 + date_ratio * 0.2)
            logger.info(
                f"[选择器自愈] 验证通过：{len(test_items)} 项, date_ratio={date_ratio:.1%}, "
                f"link_ratio={link_ratio:.1%}, conf={result['confidence']:.2f}"
            )
        except Exception as e:
            logger.warning(f"[选择器自愈] 验证异常: {e}")
            return None

        return result

    except Exception as e:
        logger.warning(f"[选择器自愈] 失败: {e}")
        return None


def _generate_selectors_from_elements(elements, page, url=''):
    """从 Scrapling 元素生成新的 CSS 选择器组合。

    对第一个元素使用 generate_css_selector 生成通用选择器，
    并提取标题、链接、日期子选择器。

    Args:
        elements: Scrapling Selector 元素列表
        page: Scrapling Selector 页面
        url: 页面 URL

    Returns:
        dict: {list_selector, title_selector, link_selector, date_selector, confidence}
    """
    if not elements:
        return None

    first = elements[0]
    tag = first.tag if hasattr(first, 'tag') else 'li'

    # 从第一个恢复的元素生成列表选择器
    try:
        # 尝试找父容器生成更稳健的选择器
        parent = first.parent
        if parent and hasattr(parent, 'tag'):
            parent_css = parent.generate_css_selector if hasattr(parent, 'generate_css_selector') else ''
            # 简化父选择器 + 子元素标签
            list_selector = f'{parent.tag}.{parent.attrib.get("class", [""])[0] if parent.attrib.get("class") else ""} {tag}'.strip()
            if not list_selector or list_selector.endswith(' '):
                list_selector = f'{parent.tag} {tag}'
        else:
            list_selector = f'{tag}'
    except Exception:
        list_selector = f'{tag}'

    # 从列表容器生成更精确的选择器
    try:
        # 优先找最内层的 ul/ol 列表容器（通知列表通常在 <ul> 或 <ol> 中）
        ancestor = first.parent
        list_container = None
        while ancestor and hasattr(ancestor, 'tag'):
            if ancestor.tag in ('ul', 'ol'):
                list_container = ancestor
                # 继续往上找，看有没有更外层的 ul/ol 或带 class/id 的容器
                # 停在第一个带 class/id 的容器
            ancestor = ancestor.parent if hasattr(ancestor, 'parent') else None

        if list_container and hasattr(list_container, 'attrib'):
            classes = list_container.attrib.get('class', [])
            el_id = list_container.attrib.get('id', '')
            cls_str = ' '.join(classes) if isinstance(classes, list) else classes
            if el_id:
                list_selector = f'#{el_id} {tag}'
            elif cls_str:
                list_selector = f'{list_container.tag}.{cls_str.replace(" ", ".")} {tag}'
            else:
                list_selector = f'{list_container.tag} {tag}'
        else:
            # 回退：找带 class/id 的祖先 div
            ancestor = first.parent
            while ancestor and hasattr(ancestor, 'tag'):
                classes = ancestor.attrib.get('class', []) if hasattr(ancestor, 'attrib') else []
                el_id = ancestor.attrib.get('id', '') if hasattr(ancestor, 'attrib') else ''
                if classes or el_id:
                    break
                ancestor = ancestor.parent if hasattr(ancestor, 'parent') else None

            if ancestor and hasattr(ancestor, 'tag'):
                if hasattr(ancestor, 'attrib'):
                    classes = ancestor.attrib.get('class', [])
                    el_id = ancestor.attrib.get('id', '')
                    if el_id:
                        list_selector = f'#{el_id} {tag}'
                    elif classes:
                        cls = classes[0] if isinstance(classes, list) else classes
                        list_selector = f'{ancestor.tag}.{cls} {tag}'
    except Exception:
        pass

    # 检测子选择器：标题（第一个 a 标签或文本最多的子元素）
    title_selector = 'a'
    link_selector = 'a'
    date_selector = 'span'

    try:
        children = first.children if hasattr(first, 'children') else []
        if children:
            # 找链接
            links = first.css('a')
            if links:
                link_href = links[0].attrib.get('href', '') if hasattr(links[0], 'attrib') else ''
                # 生成链接选择器
                for link in links:
                    link_css = link.generate_css_selector if hasattr(link, 'generate_css_selector') else 'a'
                    # 优先用 class 选择器
                    if hasattr(link, 'attrib') and link.attrib.get('class'):
                        cls = link.attrib['class']
                        cls_str = cls[0] if isinstance(cls, list) else cls
                        title_selector = f'a.{cls_str}'
                        link_selector = f'a.{cls_str}'
                        break
                    elif link_css and 'nth' not in link_css:
                        title_selector = link_css
                        link_selector = link_css
                        break

            # 找日期（包含数字最多的 span/div）
            spans = first.css('span, div')
            max_digits = 0
            for sp in spans:
                text = sp.text if hasattr(sp, 'text') else ''
                digits = sum(1 for c in text if c.isdigit())
                if digits > max_digits:
                    max_digits = digits
                    if hasattr(sp, 'attrib') and sp.attrib.get('class'):
                        cls = sp.attrib['class']
                        cls_str = cls[0] if isinstance(cls, list) else cls
                        date_selector = f'span.{cls_str}'
                    else:
                        date_selector = 'span'
    except Exception:
        pass

    result = {
        'list_selector': list_selector,
        'title_selector': title_selector,
        'link_selector': link_selector,
        'date_selector': date_selector,
        'content_selector': 'div.article-content, div.content, div.main, article',
        'confidence': 0.7,  # 自愈的置信度略低于人工确认
        'method': 'auto_heal',
    }

    logger.info(f"[选择器自愈] 生成选择器: list={list_selector}, title={title_selector}, date={date_selector}")
    return result


def generate_robust_selectors(html, url, candidate_items):
    """使用 Scrapling 从 DOM 分析发现的候选元素生成稳健的 CSS 选择器。

    这是 list_detector.py 的增强版——用 Scrapling 的
    generate_css_selector 替代我们自己的 CSS 路径生成。

    Args:
        html: 页面 HTML
        url: 页面 URL
        candidate_items: BS4 元素列表（DOM 分析发现的候选项）

    Returns:
        dict: {list_selector, title_selector, ...} 或 None
    """
    try:
        from scrapling.parser import Selector

        page = Selector(html, url=url)

        # 获取候选元素的 CSS 路径，然后在 Scrapling 中找相似元素
        # 通过文本内容匹配
        texts = []
        for item in candidate_items[:5]:
            t = item.get_text(strip=True)
            if t and len(t) >= 6:
                texts.append(t[:40])

        if not texts:
            return None

        # 用 find_by_text 在 Scrapling 中找到元素
        all_found = []
        for t in texts:
            found = page.find_by_text(t)
            if found:
                all_found.extend(found)

        if not all_found:
            return None

        # 用 Scrapling 的元素生成选择器
        return _generate_selectors_from_elements(all_found, page, url)

    except Exception as e:
        logger.warning(f"[选择器生成] Scrapling 增强失败: {e}")
        return None


def save_discovered_selectors(school_id, department_configs):
    """将发现的选择器保存到学校 config JSON 字段。

    Args:
        school_id: 学校 ID
        department_configs: [{name, url, list_selector, ...}, ...]
    """
    school = School.query.get(school_id)
    if not school:
        return

    config = {}
    if school.config:
        try:
            config = json.loads(school.config)
        except json.JSONDecodeError:
            config = {}

    # 更新 selector_history
    history = config.get('selector_history', [])
    for dept in department_configs:
        entry = {
            'domain': _extract_domain(dept.get('url', dept.get('list_url', ''))),
            'list_selector': dept.get('list_selector', ''),
            'title_selector': dept.get('title_selector', ''),
            'link_selector': dept.get('link_selector', ''),
            'date_selector': dept.get('date_selector', ''),
            'content_selector': dept.get('content_selector', ''),
            'confidence': dept.get('confidence', 0.5),
            'verified_at': datetime.now(timezone.utc).isoformat(),
        }

        # 去重
        existing = next(
            (h for h in history
             if h['domain'] == entry['domain']
             and h['list_selector'] == entry['list_selector']),
            None
        )
        if existing:
            existing.update(entry)
        else:
            history.append(entry)

    config['selector_history'] = history

    # 发现元数据
    if 'discovery' not in config:
        config['discovery'] = {}
    config['discovery']['last_run'] = datetime.now(timezone.utc).isoformat()
    config['discovery']['confirmed_departments'] = len(department_configs)

    school.config = json.dumps(config, ensure_ascii=False)
    db.session.commit()
    logger.info(f"Saved {len(department_configs)} department selectors for school {school_id}")


def get_learned_selectors(school_id):
    """获取学校已学习的选择器历史。

    Returns:
        list: [{domain, list_selector, ...}, ...]
    """
    school = School.query.get(school_id)
    if not school or not school.config:
        return []

    try:
        config = json.loads(school.config)
        return config.get('selector_history', [])
    except json.JSONDecodeError:
        return []


def get_global_knowledge():
    """聚合所有学校的成功选择器模式。

    返回按成功率加权的全局选择器知识库。
    新学校发现时可优先尝试这些模式。

    Returns:
        list: [{domain, list_selector, ..., confidence, school_count}]
        按置信度降序
    """
    all_patterns = {}
    schools = School.query.all()

    for school in schools:
        if not school.config:
            continue
        try:
            config = json.loads(school.config)
            history = config.get('selector_history', [])
        except json.JSONDecodeError:
            continue

        for entry in history:
            key = (entry.get('domain', ''), entry.get('list_selector', ''))
            if key not in all_patterns:
                all_patterns[key] = {
                    **entry,
                    'school_count': 0,
                    'total_confidence': 0,
                }
            all_patterns[key]['school_count'] += 1
            all_patterns[key]['total_confidence'] += entry.get('confidence', 0.5)

    # 计算平均置信度
    result = []
    for pattern in all_patterns.values():
        pattern['avg_confidence'] = round(
            pattern['total_confidence'] / pattern['school_count'], 2
        )
        result.append(pattern)

    result.sort(key=lambda p: (p['school_count'], p['avg_confidence']), reverse=True)
    return result


def build_profile_from_knowledge(school_id, domain=''):
    """从全球知识库构建该学校的推荐 SELECTOR_PROFILES。

    返回该域名下成功率最高的选择器组合，按优先级排列。
    可作为现有 SELECTOR_PROFILES 的补充。

    Args:
        school_id: 学校 ID
        domain: 学校域名（用于过滤相关模式）

    Returns:
        list: 推荐的 profile 列表（与 SELECTOR_PROFILES 格式兼容）
    """
    knowledge = get_global_knowledge()

    # 过滤：同域名或通用模式
    relevant = []
    for entry in knowledge:
        # 晋升门槛：单校学到的模式只允许同域复用，≥2 校验证过才可跨校晋升，
        # 防止一次误检被推广到其他学校（知识库投毒防护）
        if entry.get('school_count', 1) < 2 and entry.get('domain', '') != domain:
            continue
        if not domain or entry.get('domain', '') in (domain, ''):
            if entry.get('list_selector'):
                relevant.append({
                    'name': f'Learned: {entry.get("domain", "unknown")} ({entry["avg_confidence"]})',
                    'list_selector': entry['list_selector'],
                    'title_selector': entry.get('title_selector', 'a'),
                    'link_selector': entry.get('link_selector', 'a'),
                    'date_selector': entry.get('date_selector', 'span'),
                    'content_selector': entry.get('content_selector',
                                                  'div.article-content, div.content, div.main, article'),
                })

    return relevant[:10]


def record_failed_selector(school_id, domain, list_selector, reason=''):
    """记录失败的选择器模式（用于未来避免）。"""
    school = School.query.get(school_id)
    if not school:
        return

    config = {}
    if school.config:
        try:
            config = json.loads(school.config)
        except json.JSONDecodeError:
            config = {}

    blacklist = config.get('selector_blacklist', [])
    blacklist.append({
        'domain': domain,
        'list_selector': list_selector,
        'reason': reason,
        'recorded_at': datetime.now(timezone.utc).isoformat(),
    })

    # 限制黑名单长度
    config['selector_blacklist'] = blacklist[-50:]
    school.config = json.dumps(config, ensure_ascii=False)
    db.session.commit()


def _extract_domain(url):
    """从 URL 提取域名。"""
    from urllib.parse import urlparse
    try:
        return urlparse(url).netloc or ''
    except Exception:
        return ''
