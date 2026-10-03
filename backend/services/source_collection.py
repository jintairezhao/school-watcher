"""Bounded list collection for an official unit; bodies are still loaded on demand."""
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from backend.database.db import db
from backend.database.models import DepartmentDirectoryEntry
from backend.services.source_inventory import canonical_url
from backend.scraper.fetch_errors import SourceAccessError, describe_fetch_error, http_failure


def collection_progress(key):
    """Small progress values share the task transaction; DOM lives in evidence files."""
    from backend.services.tasks import current_execution
    handle = current_execution()
    return dict((handle.get('checkpoint', {}).get('collection_progress') or {}).get(key) or {}) if handle else {}


def save_collection_progress(key, value):
    from backend.services.tasks import current_execution, checkpoint
    handle = current_execution()
    if handle:
        data = dict(handle.get('checkpoint') or {})
        progress = dict(data.get('collection_progress') or {})
        progress[key] = value
        data['collection_progress'] = progress
        # The newly ingested notices and this recovery point commit together.
        checkpoint(data)
    else:
        db.session.commit()


def fetch_source_page(url, *, purpose='directory', source_id=''):
    from backend.scraper.discovery.inventory_crawler import fetch_page
    try:
        page = fetch_page(url, purpose=purpose, source_id=source_id)
    except Exception as exc:
        raise describe_fetch_error(exc) from exc
    if page.get('result') is not None:
        from backend.scraper.acquisition import FetchedHTML, FetchFailure
        result = page['result']
        if not result.ok:
            raise FetchFailure(result)
        return FetchedHTML(result)
    # Compatibility for integrations that supply the historical page dictionary.
    html = page.get('html') or ''
    if page.get('status', 200) >= 400:
        raise http_failure(page['status'])
    from backend.scraper.acquisition import FetchRequest, FetchResult, FetchFailure, FetchedHTML, classify_result
    # The caller's purpose decides which content rule applies; defaulting here
    # would let an article page pass as a list merely because it has links.
    result = classify_result(FetchRequest(url, purpose=purpose), FetchResult(page.get('url') or url,
                             status=page.get('status', 200), html=html))
    if not result.ok:
        raise FetchFailure(result)
    return FetchedHTML(result)


def collect_source(department):
    from backend.scraper.engine import scrape_department, _process_announcement_item
    from backend.scraper.discovery.publication_lists import publication_lists
    from backend.scraper.discovery.lightweight import discover_columns
    is_unit = department.kind in ('unit', 'group') or DepartmentDirectoryEntry.query.filter_by(parent_id=department.id).first() is not None
    if department.list_selector and not is_unit:
        from backend.scraper.selector_monitor import queue_parser_repair, ParserRepairPending
        from backend.scraper.acquisition import FetchFailure, FetchedHTML
        department._parser_repair_job = None
        try:
            new, total = scrape_department(department, department.school.url, strict_fetch=True)
        except ParserRepairPending as exc:
            return _repair_progress(exc.job)
        except FetchFailure as exc:
            if exc.result.outcome != 'needs_adapter' or not exc.result.html:
                raise
            job = queue_parser_repair(department, FetchedHTML(exc.result))
            if job is None:
                raise
            return _repair_progress(job)
        if not total and not getattr(department, '_fetch_confirmed_empty', False):
            return _repair_progress(queue_parser_repair(department))
        if department._parser_repair_job:
            return _repair_progress(department._parser_repair_job, new, total)
        if total:
            from backend.services.student_information import queue_source_assessment, queue_listing_assessment
            from backend.services.tasks import current_execution
            handle = current_execution() or {}
            assistance = handle.get('payload', {}).get('ai_assist') is not False
            from backend.database.models import BackgroundTask
            discovery = BackgroundTask.query.filter_by(identity=f'discover:{department.school_id}').first()
            if discovery and discovery.payload.get('ai_assist') is False:
                assistance = False
            queue_source_assessment(department.id, ai_assist=assistance)
            queue_listing_assessment(department.id, ai_assist=assistance)
        return {'new_count': new, 'checked': total, 'message': f'更新完成，新增 {new} 条通知'}

    # Unit homepages and sources without a valid configured list enter the
    # directory pipeline. A temporary detector must never ingest homepage/news
    # widgets under the unit identity before their own scopes are verified.
    from backend.services.discovery_changes import ensure_initial
    job = ensure_initial(department.school)
    if not job or job.state in ('done', 'failed'):
        return {'new_count': 0, 'checked': 0, 'partial': True, 'state': 'needs_check',
                'message': '暂未找到可读取的通知，已有通知仍保留'}
    return {'new_count': 0, 'checked': 0, 'partial': True, 'state': 'discovering',
            'message': '正在自动查找公开通知，找到后会陆续显示；已有通知继续保留'}


def _repair_progress(job, new=0, total=0):
    pending = job and job.state in ('pending', 'running', 'waiting')
    return {'new_count': new, 'checked': total, 'partial': True,
            'state': 'repairing' if pending else 'waiting',
            'repair_task_id': job.id if job else None,
            'message': ('官网页面已变化，正在自动恢复读取；已有消息仍保留' if pending
                        else '官网读取暂未恢复，系统稍后会自动重试；已有消息仍保留')}
