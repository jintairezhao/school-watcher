"""Ensure basic features run with all optional packages unavailable."""
import builtins
import sys
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
original_import = builtins.__import__
optional = {'openai', 'curl_cffi', 'scrapling', 'playwright', 'selenium'}


def without_optional(name, *args, **kwargs):
    if name.split('.')[0] in optional:
        raise ImportError('Optional package intentionally unavailable: ' + name)
    return original_import(name, *args, **kwargs)


builtins.__import__ = without_optional
try:
    from backend import create_app
    from backend.database.db import db
    from backend.database.models import BackgroundTask
    from backend.scraper import engine
    from backend.scraper.discovery.lightweight import discover_columns
    from backend.ai.summarizer import batch_summarize
    app = create_app({'TESTING': True, 'SECRET_KEY': 'minimal-test', 'SQLALCHEMY_DATABASE_URI': 'sqlite://'})
    with app.app_context():
        db.create_all()
        assert app.test_client().get('/explore').status_code == 200
        # Summary jobs now require an explicit selection; basic use must never
        # silently schedule paid work when optional AI integrations are absent.
        with TestCase().assertRaisesRegex(ValueError, '请选择'):
            batch_summarize()
        assert BackgroundTask.query.filter_by(kind='summary').count() == 0
        assert discover_columns('https://www.tsinghua.edu.cn', '<a href="/tzgg">通知公告</a>')
    print('Minimal runtime passes without AI SDK, browser, curl_cffi, Scrapling or Selenium.')
finally:
    builtins.__import__ = original_import
