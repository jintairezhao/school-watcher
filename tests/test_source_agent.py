"""The source agent learns from executed extraction, with bounded durable calls."""
import unittest
from unittest.mock import patch


class SourceAgentTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_body_repair_receives_the_observed_article_dom_and_failed_execution(self):
        from backend.services.source_agent import recognize_column, validation_feedback
        from backend.services.source_validation import article_samples
        from backend.ai.skill_loader import canonical
        from tests.test_direct_onboarding import HTML, URL
        title = '学校关于国庆节放假及调课安排的通知'
        article = '<h2>' + title + '</h2><div class="Article_content"><p>' + '请各教学单位按要求落实教学和调课安排。' * 700 + '</p></div>'
        config = {'name': '通知公告', 'list_url': URL, 'content_selector': '#missing',
                  'container_selector': '#notices', 'heading_selector': '#notices h2', 'list_selector': '#notices li'}
        result = article_samples(config, [{'title': title, 'url': URL + '/one.htm'}], lambda *_: article)
        self.assertFalse(result['passed'])
        feedback = validation_feedback(config, ['article_sample_mismatch'], articles=result)
        response = {'status': 'succeeded', 'output': {'status': 'not_column', 'columns': []}}
        with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', return_value=response) as model:
            recognize_column(self.fixture.school_id, URL, HTML, feedback=[feedback])
        inputs = [call.args[2] for call in model.call_args_list]
        bodies = [proof for value in inputs for proof in value['evidence'] if proof.get('role') == 'article_sample']
        self.assertTrue(bodies)
        self.assertTrue(any('Article_content' in canonical(proof)
                            for proof in bodies))
        self.assertTrue(all(proof['url'] == URL + '/one.htm' for proof in bodies))
        self.assertTrue(all(len(canonical(value).encode()) <= 24 * 1024 for value in inputs))
        trial = inputs[0]['tool_feedback'][0]['article_trials'][0]
        self.assertTrue(trial['title_matched'])
        self.assertEqual(trial['content_selector_count'], 0)

    def test_failed_selector_is_sent_with_page_evidence_and_changes_execution_identity(self):
        from backend.services.source_agent import recognize_column, validation_feedback
        from tests.test_direct_onboarding import HTML, URL
        feedback = validation_feedback({'name': '通知公告', 'list_selector': '#missing li',
            'heading_selector': '#notices h2', 'container_selector': '#notices'}, ['column_list_empty'])
        response = {'status': 'succeeded', 'output': {'status': 'not_column', 'columns': []}}
        with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', return_value=response) as model:
            recognize_column(self.fixture.school_id, URL, HTML)
            recognize_column(self.fixture.school_id, URL, HTML, feedback=[feedback])
            recognize_column(self.fixture.school_id, URL, HTML, feedback=[feedback])
        first, repair, repeat = model.call_args_list
        self.assertNotEqual(first.args[4], repair.args[4])
        self.assertEqual(repair.args[4], repeat.args[4])
        material = repair.args[2]
        self.assertEqual(material['tool_feedback'][0]['list_selector'], '#missing li')
        self.assertEqual(material['tool_feedback'][0]['codes'], ['column_list_empty'])
        self.assertIn('notices', material['evidence'][0]['html'])

    def test_feedback_is_bounded_to_executed_fields_and_small_observed_samples(self):
        from backend.services.source_agent import validation_feedback
        observation = validation_feedback({'name': '栏目' * 500, 'list_selector': '#items li',
            'api_key': 'not-for-model', 'action': 'run arbitrary code'}, ['scope_mismatch'] * 20,
            [{'title': '真实标题' * 100, 'url': f'https://example.edu.cn/{i}', 'content': 'huge'} for i in range(20)])
        self.assertNotIn('api_key', observation)
        self.assertNotIn('action', observation)
        self.assertLessEqual(len(observation['name']), 120)
        self.assertLessEqual(len(observation['codes']), 10)
        self.assertLessEqual(len(observation['sample_records']), 3)
        self.assertNotIn('content', observation['sample_records'][0])

    def test_feedback_reports_ambiguous_article_cards_and_invalid_selectors(self):
        from backend.services.source_agent import selector_observation, validation_feedback
        html = ''.join(f'<article class="card"><h3>真实通知{i}</h3><a href="/{i}">正文</a></article>' for i in range(10))
        column = {'container_selector': '.card', 'heading_selector': '.card h3', 'list_selector': ':bad-selector('}
        measured = selector_observation(column, html)
        self.assertEqual(measured['container_count'], 10)
        self.assertEqual(measured['heading_count'], 10)
        self.assertEqual(measured['heading_texts'], ['真实通知0', '真实通知1', '真实通知2'])
        self.assertEqual(measured['selector_errors'], ['list_selector'])
        feedback = validation_feedback(column, ['column_identity_or_scope_unconfirmed'], execution=measured)
        self.assertEqual(feedback['execution'], measured)

    def test_a_feedback_round_does_not_reuse_the_prior_region_checkpoint(self):
        from backend.services import tasks
        from backend.services.source_agent import recognize_column, validation_feedback
        from tests.test_direct_onboarding import URL, HTML
        from backend.database.models import BackgroundTask
        from backend.database.db import db
        job = tasks.enqueue('onboard', 'feedback', {'school_id': self.fixture.school_id, 'url': URL})
        handle = tasks.claim(capabilities=['http'])
        wrong = {'name': '通知公告', 'list_selector': '#missing li'}
        correct = {'name': '通知公告', 'list_selector': '#notices li'}
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=[
                 {'status': 'succeeded', 'output': {'status': 'ready', 'columns': [wrong]}},
                 {'status': 'succeeded', 'output': {'status': 'ready', 'columns': [correct]}}]) as model:
            self.assertEqual(recognize_column(self.fixture.school_id, URL, HTML)['columns'], [wrong])
            feedback = [validation_feedback(wrong, ['column_list_empty'])]
            self.assertEqual(recognize_column(self.fixture.school_id, URL, HTML, feedback=feedback)['columns'], [correct])
            self.assertEqual(recognize_column(self.fixture.school_id, URL, HTML, feedback=feedback)['columns'], [correct])
        self.assertEqual(model.call_count, 2)
        self.assertEqual(db.session.get(BackgroundTask, job.id).checkpoint['column_regions']['columns'], [correct])

    def test_first_verified_candidate_can_be_installed_before_remaining_material(self):
        from backend.services import tasks
        from backend.services.source_agent import recognize_column
        from tests.test_direct_onboarding import URL
        pieces = [{'school_id': self.fixture.school_id, 'candidates': [{'candidate_id': 'page', 'url': URL}],
                   'evidence': [{'evidence_id': str(i), 'html': f'<section id="s{i}"></section>'}]} for i in range(2)]
        tasks.enqueue('onboard', 'regions', {'school_id': self.fixture.school_id, 'url': URL})
        handle = tasks.claim(capabilities=['http'])
        first_column = {'name': '通知', 'list_selector': '#one li'}
        second_column = {'name': '公告', 'list_selector': '#two li'}
        with tasks.execution_scope(handle), \
             patch('backend.scraper.discovery.column_regions.materials', return_value=pieces), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=[
                 {'status': 'succeeded', 'output': {'status': 'ready', 'columns': [first_column]}},
                 {'status': 'succeeded', 'output': {'status': 'ready', 'columns': [second_column]}}]) as model:
            first = recognize_column(self.fixture.school_id, URL, 'large-page')
            self.assertFalse(first['material_complete'])
            self.assertEqual(first['columns'], [first_column])
            self.assertEqual(model.call_count, 1)
            second = recognize_column(self.fixture.school_id, URL, 'large-page')
            self.assertTrue(second['material_complete'])
            self.assertEqual(second['columns'], [first_column, second_column])
            self.assertEqual(recognize_column(self.fixture.school_id, URL, 'large-page'), second)
            self.assertEqual(model.call_count, 2)


if __name__ == '__main__':
    unittest.main()
