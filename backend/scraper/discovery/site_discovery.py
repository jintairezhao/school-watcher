"""Compatibility entry points for persistent official-source discovery."""
from urllib.parse import urlsplit

from filelock import Timeout
from flask import current_app, has_app_context


def discover_school_departments(school_url: str) -> list:
    from backend.services.source_inventory import Inventory, DEFAULT_PATH
    from backend.services.source_catalog import publication_candidates
    from backend.scraper.discovery.inventory_crawler import crawl_site

    name = urlsplit(school_url).hostname or school_url
    path = DEFAULT_PATH
    if has_app_context():
        from backend.database.models import School
        school = School.query.filter_by(url=school_url).first()
        if school:
            name = school.name
        path = current_app.config.get('SOURCE_INVENTORY_PATH', DEFAULT_PATH)
    inventory = Inventory(path)
    key = inventory.ensure_site(name, school_url)
    try:
        report = crawl_site(inventory, key, max_pages=250, workers=4, focus='student')
    except Timeout:
        # Another owner is advancing this same durable frontier.
        report = inventory.report(key)
    return publication_candidates(report, inventory.structure(key), focus='student')


def apply_discovered_departments(school_id: int, dept_configs: list) -> int:
    from backend.services.source_catalog import apply_source_configs
    return apply_source_configs(school_id, dept_configs) if dept_configs else 0
