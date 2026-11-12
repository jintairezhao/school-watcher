"""Cold event payloads -> published snapshot -> bilingual 3D source replay."""
from pathlib import Path
import json
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tests'), str(Path(__file__).parent)]
from check_model_usage_browser import ModelUsageFixture
from check_source_execution_browser import check
from backend.services.discovery_history import rotate_events
from backend.services.runtime_catalog import RuntimeCatalog

OUTPUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT/'.local/structure-facts-20261021/event-rotation-browser'


class ColdEventFixture(ModelUsageFixture):
    def connect(self):
        method = RuntimeCatalog.publish
        def archive_before_publish(catalog, store, *args, **kwargs):
            rotate_events(store, keep_recent=0, older_than_days=0)
            return method(catalog, store, *args, **kwargs)
        with patch.object(RuntimeCatalog, 'publish', autospec=True, side_effect=archive_before_publish):
            version = super().connect()
        with self.f.store.connect() as connection:
            hot = connection.execute('SELECT count(*) FROM exploration_events').fetchone()[0]
            cold = connection.execute('SELECT count(*) FROM exploration_event_archive_index').fetchone()[0]
        assert hot == 0 and cold > 0
        output = OUTPUT
        output.mkdir(exist_ok=True)
        (output/'fixture.json').write_text(json.dumps(dict(hot_event_payloads=hot, cold_event_payloads=cold,
            actual_website_requests=0, actual_model_calls=0, source_configuration_versions=1,
            scope='Actual local execution paths with provider substitute; not independent website evidence'), indent=2), 'utf-8')
        return version


if __name__ == '__main__':
    check(ColdEventFixture, OUTPUT,
        expected_event_text='Confirmed usage: 70 tokens', expected_zh_event_text='已确认用量：70 Token')
