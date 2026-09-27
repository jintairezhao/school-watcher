"""Discovery UI orchestration over the persistent official source inventory."""
import logging

from flask import current_app
from filelock import Timeout

logger = logging.getLogger(__name__)


def run_discovery_in_background(school, session_obj, app=None):
    from backend.database.db import db
    from backend.database.models import School
    from backend.services.source_inventory import Inventory, DEFAULT_PATH
    from backend.services.source_catalog import publication_candidates
    from backend.scraper.discovery.inventory_crawler import crawl_site
    school_id = school.id
    if app is None:
        app = current_app._get_current_object()
    with app.app_context():
        try:
            school = db.session.get(School, school_id)
            if not school or not school.enabled:
                session_obj.fail('学校不存在或已下架')
                return
            inventory = Inventory(app.config.get('SOURCE_INVENTORY_PATH', DEFAULT_PATH))
            key = inventory.ensure_site(school.name, school.url)
            session_obj.set_phase('enumerating', '优先检查院系、专业与教务通知，保留官网上下级及待复查入口', 5)
            def progress(count, report):
                if count % 10 == 0:
                    pending = report['states'].get('pending', 0)
                    session_obj.set_phase('enumerating', f'本轮已检查 {count} 页，仍有 {pending} 个入口待核对',
                                          min(95, 5 + int(count * 90 / 250)))
            result = crawl_site(inventory, key, max_pages=250, workers=4, progress=progress, focus='student')
            candidates = publication_candidates(result, inventory.structure(key), focus='student')
            session_obj.total_departments = len(result['pages'])
            for candidate in candidates:
                session_obj.add_found(candidate)
            for page in result['pages']:
                if page['state'] in ('failed', 'blocked'):
                    session_obj.add_skipped(page['label'], '访问未完成，已保留原始入口与证据')
                elif 'external_ownership_requires_review' in (page['notes_json'] or ''):
                    from backend.services.source_ownership import domain_evidence
                    if not domain_evidence(result['pages'], key, page['final_url'] or page['url']):
                        session_obj.add_skipped(page['label'], '外部站点的学校归属待核实，已保留原始入口与证据')
            pending = result['states'].get('pending', 0)
            session_obj.complete({
                'departments': candidates, 'pending_pages': pending,
                'coverage_verified': result['accepted'],
                'message': f'本轮检查结束：{len(candidates)} 个可适配栏目，{pending} 个入口待继续核对。尚未完成 {result["match_threshold"]}% 验收。',
            })
        except Timeout:
            session_obj.fail('这所学校已有来源检查正在运行，可在官网来源结构页查看进度')
        except Exception as exc:
            logger.exception('Source discovery failed for school %s', school_id)
            session_obj.fail(f'本轮检查中断，已保存待处理进度：{str(exc)[:160]}')
        finally:
            db.session.remove()
