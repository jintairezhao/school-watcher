"""Small read-only source audit. No production database or subscriptions are changed."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def audit(urls, channel=None):
    from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result
    from backend.scraper.acquisition.coordinator import http_fetch
    from backend.browser_service.runtime import BrowserRuntime
    if channel:
        os.environ['WATCHER_BROWSER_CHANNEL'] = channel
    runtime = BrowserRuntime(concurrency=1, max_sessions=2)
    rows = []
    try:
        for index, url in enumerate(urls):
            request = FetchRequest(url=url, purpose='list', source_id=f'audit-{index}',
                request_id=f'audit-{index}', timeout_seconds=45)
            result = classify_result(request, await asyncio.to_thread(http_fetch, request))
            row = {'url': url, 'http_outcome': result.outcome, 'http_status': result.status,
                   'http_error': result.error_code, 'http_ms': result.timings.get('http_ms')}
            if result.outcome == 'requires_render':
                try:
                    await runtime.submit(request.to_dict())
                    await runtime.executions[request.request_id].task
                    envelope = runtime.get_execution(request.request_id)
                    if envelope['state'] == 'done':
                        rendered = classify_result(request, FetchResult.from_dict(envelope['result']))
                        row.update(browser_outcome=rendered.outcome, browser_status=rendered.status,
                            browser_error=rendered.error_code, final_url=rendered.final_url,
                            browser_ms=rendered.timings.get('total_ms'))
                    else:
                        row.update(browser_outcome='runtime_failure', browser_error=envelope.get('error', {}).get('code'))
                except Exception as exc:
                    row.update(browser_outcome='runtime_failure', browser_error=type(exc).__name__)
            else:
                row['browser_outcome'] = 'not_attempted'
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            await asyncio.sleep(1)
    finally:
        await runtime.close()
    return {'checked_at': datetime.now(timezone.utc).isoformat(), 'samples': rows,
        'manual_verification': 'not performed', 'database_modified': False,
        'interpretation': 'Page classification only; not a verified school-wide notice coverage rate'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('urls', nargs='+')
    parser.add_argument('--browser-channel', choices=['msedge', 'chrome'])
    parser.add_argument('--output', type=Path, default=ROOT/'data/source-audits/fetch-runtime.json')
    args = parser.parse_args()
    result = asyncio.run(audit(args.urls, args.browser_channel))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
