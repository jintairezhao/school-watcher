"""Fingerprint cached parsing separately from the time a website was visited."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FILES = (
    'backend/scraper/discovery/parser_revision.py',
    'backend/scraper/article_urls.py',
    'backend/scraper/date_elements.py',
    'backend/scraper/discovery/structure.py',
    'backend/scraper/discovery/directory_adapters.py',
    'backend/scraper/discovery/academic_directories.py',
    'backend/scraper/discovery/directory_labels.py',
    'backend/scraper/discovery/literal_data.py',
    'backend/scraper/discovery/script_directories.py',
    'backend/scraper/discovery/medical_directories.py',
    'backend/scraper/discovery/major_directories.py',
    'backend/scraper/discovery/programme_catalogs.py',
    'backend/scraper/discovery/listing_headings.py',
    'backend/scraper/discovery/medical_publications.py',
    'backend/scraper/discovery/metinfo_publications.py',
    'backend/scraper/discovery/cup_news_headings.py',
    'backend/scraper/discovery/unit_profiles.py',
    'backend/scraper/discovery/publication_lists.py',
    'backend/scraper/discovery/publication_tabs.py',
    'backend/scraper/discovery/inventory_crawler.py',
    'backend/scraper/detectors/dom_analyzer.py',
    'backend/scraper/detectors/title_quality.py',
    'backend/scraper/cms_registry.py',
    'backend/scraper/change_detector.py',
    'backend/services/source_inventory.py',
    'backend/services/source_ownership.py',
    'cms_profiles.yaml',
)
# Freeze once per process. An old running worker must not label its results with
# the revision of files subsequently edited on disk.
def current_revision():
    return hashlib.sha256(b'\0'.join(
        name.encode() + b'\0' + (ROOT / name).read_bytes().replace(b'\r\n', b'\n') for name in FILES
    )).hexdigest()


PARSER_REVISION = current_revision()


class ParserRevisionChanged(RuntimeError):
    """The worker must be restarted before it may write parsed evidence."""


def assert_current_parser():
    try:
        revision = current_revision()
    except OSError as exc:
        raise ParserRevisionChanged('Source parser files are changing; restart this worker.') from exc
    if revision != PARSER_REVISION:
        raise ParserRevisionChanged('Source parser changed; restart this worker before continuing.')
