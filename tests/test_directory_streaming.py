"""Interrupted provider responses preserve complete records without live billing."""
import json
import unittest
from unittest.mock import patch

from backend.ai.providers import complete, ProviderError


class StreamResponse:
    status_code = 200
    headers = {'x-request-id': 'stream-fixture'}

    def __init__(self, frames, failure=None):
        self.frames, self.failure = frames, failure

    def __enter__(self): return self
    def __exit__(self, *args): pass

    def iter_content(self, chunk_size):
        for frame in self.frames:
            raw = frame.encode('utf-8')
            for start in range(0, len(raw), 3):
                yield raw[start:start + 3]
        if self.failure:
            raise self.failure


def event(content='', finish=None, usage=None):
    value = {'id': 'stream-fixture', 'choices': [{'delta': {'content': content}, 'finish_reason': finish}]}
    if usage is not None: value['usage'] = usage
    return 'data: ' + json.dumps(value, ensure_ascii=False) + '\n\n'


class DirectoryStreamingTests(unittest.TestCase):
    profile = {'provider': 'deepseek', 'region': 'default', 'model': 'fixture'}

    def test_keep_alive_after_ninety_seconds_and_split_unicode(self):
        frames = [': keep-alive\n\n', event('{"name":"上海大学"}'), event(finish='stop'),
                  event(usage={'prompt_tokens': 4, 'completion_tokens': 5}), 'data: [DONE]\n\n']
        times = iter([0])
        with patch('backend.ai.providers.requests.Session') as factory, \
             patch('backend.ai.providers.time.monotonic', side_effect=lambda: next(times, 150)):
            factory.return_value.post.return_value = StreamResponse(frames)
            result = complete(self.profile, 'fixture-key', [], streaming=True)
        self.assertEqual(json.loads(result.content)['name'], '上海大学')
        self.assertEqual(result.usage['total_tokens'], 9)
        self.assertEqual(factory.return_value.post.call_args.kwargs['timeout'], (8, 120))
        self.assertTrue(factory.return_value.post.call_args.kwargs['json']['stream'])

    def test_disconnect_retains_received_content_and_diagnostics(self):
        import requests
        frame = event('{"results":[{"candidate_id":"a"},')
        with patch('backend.ai.providers.requests.Session') as factory:
            factory.return_value.post.return_value = StreamResponse([frame], requests.ConnectionError('fixture'))
            with self.assertRaises(ProviderError) as caught:
                complete(self.profile, 'fixture-key', [], streaming=True)
        self.assertEqual(caught.exception.code, 'connection_lost')
        self.assertIn('candidate_id', caught.exception.content)
        self.assertGreater(caught.exception.diagnostics['received_bytes'], 0)

    def test_missing_stream_end_is_not_success(self):
        with patch('backend.ai.providers.requests.Session') as factory:
            factory.return_value.post.return_value = StreamResponse([event('{}')])
            with self.assertRaises(ProviderError) as caught:
                complete(self.profile, 'fixture-key', [], streaming=True)
        self.assertEqual(caught.exception.code, 'stream_incomplete')

    def test_total_deadline_interrupts_an_idle_socket_and_keeps_received_results(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        import time
        release = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(event('{"results":[{"candidate_id":"a"},').encode())
                self.wfile.flush()
                release.wait(2)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            started = time.monotonic()
            with patch('backend.ai.providers.endpoint_for', return_value=f'http://127.0.0.1:{server.server_port}/'), \
                 patch('backend.ai.providers.STREAM_TOTAL_SECONDS', 0.2):
                with self.assertRaises(ProviderError) as caught:
                    complete(self.profile, 'fixture-key', [], streaming=True)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(caught.exception.code, 'response_deadline')
            self.assertIn('candidate_id', caught.exception.content)
        finally:
            release.set()
            server.shutdown()
            server.server_close()

    def test_truncated_json_keeps_complete_rows_and_discards_duplicate_ids(self):
        from backend.ai.partial_output import validated_partial
        from backend.ai.skill_loader import load_skill
        evidence = {'school_id': 1, 'candidates': [{'candidate_id': 'a'}, {'candidate_id': 'b'}],
            'entities': [{'id': 'school'}], 'evidence': [{'evidence_id': 'page', 'text': '官方名录'}]}
        row = {'candidate_id': 'a', 'kind': 'unit', 'name': '学院', 'parent_entity_id': 'school',
            'publisher_entity_id': None, 'topics': [], 'decision': 'propose',
            'evidence': {'identity': ['page'], 'name': ['page'], 'parent': ['page'], 'publisher': []},
            'reason': '官方名录', 'needed_evidence': []}
        prefix = '{"school_id":1,"results":['
        skill = load_skill('university-source-onboarding', 'classify')
        content = prefix + json.dumps(row, ensure_ascii=False) + ',{"candidate_id":"b"'
        saved = validated_partial(skill, content, evidence)
        self.assertEqual([r['candidate_id'] for r in saved['results']], ['a'])
        duplicate = prefix + json.dumps(row) + ',' + json.dumps(row) + ',{"candidate_id":"b"'
        self.assertIsNone(validated_partial(skill, duplicate, evidence))

    def test_total_deadline_preserves_content_and_idle_timeout_is_distinct(self):
        import requests
        values = iter([0, 601])
        with patch('backend.ai.providers.requests.Session') as factory, \
             patch('backend.ai.providers.time.monotonic', side_effect=lambda: next(values, 601)):
            factory.return_value.post.return_value = StreamResponse([': keep-alive\n\n'])
            with self.assertRaises(ProviderError) as caught:
                complete(self.profile, 'fixture-key', [], streaming=True)
        self.assertEqual(caught.exception.code, 'response_deadline')
        with patch('backend.ai.providers.requests.Session') as factory:
            factory.return_value.post.return_value = StreamResponse([], requests.ReadTimeout('idle'))
            with self.assertRaises(ProviderError) as caught:
                complete(self.profile, 'fixture-key', [], streaming=True)
        self.assertEqual(caught.exception.code, 'read_timeout')
