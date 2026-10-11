"""Isolated browser entrypoint with logical navigation/content measurements."""
import hashlib
import json
from pathlib import Path
import sys
import time

def instrument(runtime_class, output):
    original = runtime_class._navigate

    async def measured(self, session, payload, *args, **kwargs):
        started = time.monotonic()
        record = {'url': payload['url'], 'request_id': payload.get('request_id'), 'transport': 'browser',
                  'measurement': 'navigation_operation', 'bytes': 0}
        try:
            result = await original(self, session, payload, *args, **kwargs)
            from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result
            raw = FetchResult.from_dict(result)
            verdict = classify_result(FetchRequest.from_dict(payload), raw)
            encoded = (result.get('html') or '').encode('utf-8')
            record.update(status='returned', final_url=raw.final_url, status_code=raw.status,
                outcome=verdict.outcome, error_code=verdict.error_code, bytes=len(encoded),
                html_sha256=hashlib.sha256(encoded).hexdigest())
            return result
        except Exception as exc:
            code = getattr(exc, 'code', type(exc).__name__)
            record.update(status='failed', error_code=code,
                outcome='denied' if code == 'response_too_large' else
                        'busy' if code in ('origin_busy', 'browser_busy') else 'network_error')
            raise
        finally:
            record['elapsed_seconds'] = round(time.monotonic() - started, 3)
            with Path(output).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
    runtime_class._navigate = measured


if __name__ == '__main__':
    sys.path.insert(0, sys.argv[1])
    from backend.browser_service.runtime import BrowserRuntime
    from backend.browser_service.server import main
    instrument(BrowserRuntime, sys.argv[2])
    main()
