"""Verify a real official news list -> parsed notice -> stored record in memory."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bs4 import BeautifulSoup
from backend import create_app
from backend.database.db import db
from backend.database.models import School, Department, Announcement
from backend.scraper.engine import _fetch_html, _probe_selectors, _process_announcement_item


def main():
    app = create_app({'TESTING': True, 'SECRET_KEY': 'ingestion-check', 'SQLALCHEMY_DATABASE_URI': 'sqlite://'})
    with app.app_context():
        db.create_all()
        school = School(name='清华大学', url='https://www.tsinghua.edu.cn')
        db.session.add(school)
        db.session.flush()
        dept = Department(school_id=school.id, name='清华新闻', list_url=school.url + '/news.htm')
        db.session.add(dept)
        db.session.commit()
        cache = Path(__file__).resolve().parents[1] / 'data' / 'live-news-check.html'
        html = cache.read_text(encoding='utf-8') if '--cached-list' in sys.argv and cache.exists() else _fetch_html(dept.list_url, allow_browser_fallback=False)
        (Path(__file__).resolve().parents[1] / 'data' / 'live-news-check.html').write_text(html, encoding='utf-8')
        result = _probe_selectors(html, dept)
        if not result:
            raise RuntimeError('No list structure detected')
        profile, permanent = result
        for field in ['list_selector', 'title_selector', 'link_selector', 'date_selector', 'content_selector']:
            setattr(dept, field, profile.get(field))
        soup = BeautifulSoup(html, 'lxml')
        for node in soup.select(dept.list_selector)[:30]:
            if _process_announcement_item(node, dept, school.url):
                db.session.commit()
                ann = Announcement.query.first()
                assert ann.title != 'English Version' and len(ann.title) > 8
                assert len(ann.content_text or '') > 80, 'No article body extracted'
                print(json.dumps({'school': school.name, 'source': dept.name, 'title': ann.title,
                                  'url': ann.url, 'content_characters': len(ann.content_text or ''),
                                  'stored_in': 'isolated in-memory test database'}, ensure_ascii=True))
                return
        raise RuntimeError('List parsed but no valid notice was stored')


if __name__ == '__main__':
    main()
