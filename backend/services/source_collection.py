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
    is_unit = DepartmentDirectoryEntry.query.filter_by(department_id=department.id).first() is not None
    if department.list_selector and not is_unit:
        new, total = scrape_department(department, department.school.url, strict_fetch=True)
        if not total and not getattr(department, '_fetch_confirmed_empty', False):
            raise SourceAccessError('已读取官网，但尚未识别到有效通知列表，需要核对栏目解析规则；已有消息仍保留')
        return {'new_count': new, 'checked': total, 'message': f'更新完成，新增 {new} 条通知'}

    # Unit homepages and sources without a valid configured list enter the
    # directory pipeline. A temporary detector must never ingest homepage/news
    # widgets under the unit identity before their own scopes are verified.
    from backend.services.tasks import enqueue
    from backend.services.source_inventory import site_key
    enqueue('discover', department.school_id, {'school_id': department.school_id,
            'name': department.school.name, 'root_url': department.school.url}, replace_finished=False)
    return {'new_count': 0, 'checked': 0, 'partial': True, 'state': 'discovering',
            'message': '学院栏目正在核实，已确认的栏目会陆续接入；已有通知继续保留'}
