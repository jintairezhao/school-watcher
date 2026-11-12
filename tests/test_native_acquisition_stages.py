"""Transport defaults must not replace the recorded coordinator verdict."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from scripts.checks.curate_branch_observations import fingerprint, native_acquisition_observations
from scripts.checks.run_native_branch_pilot import Bounds, read
from backend.scraper.acquisition import FetchRequest, FetchResult


class NativeAcquisitionStageTests(unittest.TestCase):
    def setUp(self):
        self.ref = dict(capture_id='capture', action_id='action',
            decision_references=[dict(capture_id='capture', decision_id='decision')])
        self.document = dict(sequence=1, request=dict(url='http://example.edu.cn/college'),
            action_reference=self.ref, result=dict(status=403, outcome='needs_adapter', error_code='',
            final_url='http://example.edu.cn/college', decoded_html_sha256='a'*64))
        payload = dict(status=403, outcome='network_error', transport='http',
            final_url='http://example.edu.cn/college', content_hash='a'*64,
            scope='coordinator_result_not_http_dispatch_or_source_success')
        self.event = dict(event='acquisition_result', action_reference=self.ref, payload=payload,
                          payload_sha256=fingerprint(payload), _ref='event-000001.json')

    def derive(self, document=None, events=None):
        return native_acquisition_observations([document or self.document],
            [self.event] if events is None else events, capture_id='capture')[0]

    def test_transport_default_and_actual_final_verdict_remain_separate(self):
        before = deepcopy((self.document, self.event))
        row = self.derive()
        self.assertEqual(row['transport_observation']['outcome'], 'needs_adapter')
        self.assertEqual(row['coordinator_observation']['outcome'], 'network_error')
        self.assertEqual(row['coordinator_event_ref'], 'event-000001.json')
        self.assertIsNone(row['coordinator_observation']['error_code'])
        self.assertEqual((self.document, self.event), before)

    def test_unfinished_attempt_keeps_final_outcome_unknown(self):
        document = dict(self.document, result=None)
        row = self.derive(document, [])
        self.assertIsNone(row['transport_observation'])
        self.assertIsNone(row['coordinator_observation'])
        self.assertEqual(row['classification_status'], 'not_recorded_unknown')

    def test_matching_url_and_body_cannot_substitute_for_an_action_reference(self):
        for change in (dict(action_id='other'), dict(capture_id='other')):
            event = deepcopy(self.event)
            event['action_reference'] = dict(self.ref, **change)
            with self.subTest(change=change):
                self.assertIsNone(self.derive(events=[event])['coordinator_observation'])

    def test_two_results_for_one_action_are_not_chosen_by_order(self):
        with self.assertRaisesRegex(ValueError, 'Multiple coordinator results'):
            self.derive(events=[self.event, deepcopy(self.event)])

    def test_corrupted_recorded_payload_is_rejected(self):
        event = deepcopy(self.event)
        event['payload']['outcome'] = 'usable'
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            self.derive(events=[event])

    def test_same_action_with_conflicting_decision_context_is_rejected(self):
        event = deepcopy(self.event)
        event['action_reference']['decision_references'][0]['decision_id'] = 'other'
        with self.assertRaisesRegex(ValueError, 'decision context'):
            self.derive(events=[event])

    def test_sampler_marks_the_layer_without_reclassifying_or_dispatching(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            scope = dict(expires_at=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(),
                website=dict(allowed_hosts=['example.edu.cn'], maximum_documents=1,
                    maximum_http_attempts=1, maximum_response_bytes=1048576),
                model=dict(maximum_dispatch_attempts=1))
            bounds = Bounds(scope, root, root/'web', time.monotonic()+60)
            number = bounds.new_document(FetchRequest('http://example.edu.cn/college'))
            bounds.finish_document(number, FetchResult('http://example.edu.cn/college', status=403,
                html='<title>403 Forbidden</title>'))
            ledger = read(bounds.web_file)
            self.assertEqual(ledger['documents'][0]['observation_stage'],
                             'http_transport_before_content_classification')
            self.assertEqual(ledger['documents'][0]['result']['outcome'], 'needs_adapter')
            self.assertEqual(ledger['http_attempts'], [])
