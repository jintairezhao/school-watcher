"""Short-lived evidence for public website routes, never provider/API routes."""
import hashlib,json,time
from urllib.parse import urlsplit
from flask import has_app_context
from sqlalchemy import select
from requests.utils import get_environ_proxies,select_proxy
from backend.database.db import db
from backend.database.models import AppConfig

KEY='official_http_route_memory'
TTL_SECONDS=600
MAX_ORIGINS=128


def fingerprint(url):
    proxy=select_proxy(url,get_environ_proxies(url))
    if not proxy:return ''
    # Store no proxy address or credentials. A changed proxy has no old hint.
    host=(urlsplit(url).hostname or '').lower()
    return hashlib.sha256((host+'\0'+proxy).encode()).hexdigest()


def _read():
    if not has_app_context():return {}
    statement=select(AppConfig.value).where(AppConfig.key==KEY)
    session=db.session()
    if session.in_transaction():
        with session.no_autoflush:raw=session.execute(statement).scalar_one_or_none()
    else:
        with db.engine.connect() as connection:raw=connection.execute(statement).scalar_one_or_none()
    try:
        value=json.loads(raw or '{}')
        return value if isinstance(value,dict) and len(raw or '')<65536 else {}
    except (ValueError,TypeError):return {}


def prefer_verified_direct(url):
    key=fingerprint(url)
    record=_read().get(key) if key else None
    return bool(isinstance(record,dict) and isinstance(record.get('expires_at'),(int,float))
                and record['expires_at']>time.time())


def remember_verified_direct(url):
    if not has_app_context():return
    from backend.scraper.http_client import proxy_required
    from backend.services.tasks import current_execution
    if proxy_required():return
    handle=current_execution() or {}
    key=fingerprint(url)
    host=(urlsplit(url).hostname or '').lower()
    observed=(handle.get('checkpoint') or {}).get('http_failed_proxy_fingerprints',{}).get(host)
    if not key or observed!=key:return
    now=time.time()
    hints={k:v for k,v in _read().items() if isinstance(v,dict) and isinstance(v.get('expires_at'),(int,float))
           and v['expires_at']>now}
    hints[key]={'expires_at':now+TTL_SECONDS,'verified_at':now,'basis':'observed_proxy_failure_then_direct_http_success'}
    hints=dict(sorted(hints.items(),key=lambda item:item[1].get('verified_at',0))[-MAX_ORIGINS:])
    AppConfig.set(KEY,json.dumps(hints,separators=(',',':')))
