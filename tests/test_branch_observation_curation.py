"""Synthetic integrity checks only; fixtures do not prove website effectiveness."""
from copy import deepcopy
from hashlib import sha256
import unittest

from scripts.checks.curate_branch_observations import curate_logs

ROOT='https://example.edu.cn/'


def fixture():
    bodies=[b'<header><a href="/org/">org</a><a href="/admission/">admission</a></header>',
            b'<header><a href="/admission/">admission</a></header><main><a id="one" href="/one/">one</a><a id="two" href="/two/">two</a></main>',
            b'<p>one</p>',b'<p>two</p>',b'<p>access denied</p>']
    urls=[ROOT,ROOT+'org/',ROOT+'one/',ROOT+'two/',ROOT+'admission/']
    time=lambda step,offset:f'2026-01-01T00:00:{step*10+offset:02}+00:00'
    observations=[];decisions=[];attempts=[];files={}
    for number,(url,body) in enumerate(zip(urls,bodies),1):
        digest=sha256(body).hexdigest();files[digest+'.html.gz']=body
        observations.append(dict(sequence=number,school_id='school',requested_url=url,final_url=url,
            started_at=time(number,1),observed_at=time(number,4),status=401 if number==5 else 200,failure=None,
            body_sha256=digest,body_file=digest+'.html.gz',detected_encoding='utf-8',analysis=dict(links=[])))
        attempts.append(dict(sequence=number,school_id='school',url=url,started_at=time(number,1),
            finished_at=time(number,3),seconds=2,body_bytes=len(body)))
    def candidate(number,parent=None,locator=None):
        value=dict(url=urls[number-1],label=urls[number-1],kind='root' if parent is None else 'directory',source=None)
        if parent is not None:
            old=observations[parent-1]
            value['source']=dict(source_url=old['final_url'],content_hash=old['body_sha256'],observed_at=old['observed_at'],locator=locator)
            old['analysis']['links'].append(dict(url=value['url'],locator=locator,collection_decision='eligible_navigation'))
        return value
    root=candidate(1);org=candidate(2,1,'header a[href="/org/"]')
    direct_admission=candidate(5,1,'header a[href="/admission/"]')
    one=candidate(3,2,'#one');two=candidate(4,2,'#two')
    admission=candidate(5,2,'header a')
    frontiers=[[root],[org,direct_admission],[one,two,direct_admission,admission],
               [two,direct_admission,admission],[admission,direct_admission]]
    for number,frontier in enumerate(frontiers,1):
        selected=frontier[0]
        decisions.append(dict(step=number,school_id='school',selected=deepcopy(selected),known_frontier=deepcopy(frontier),decided_at=time(number,0)))
        observations[number-1]['entry']=deepcopy(selected)
    return dict(plan=dict(schools=[dict(id='school',root_url=ROOT)]),decisions=decisions,
        observations=observations,attempts=attempts,raw_reader=files.__getitem__)


class BranchObservationCurationTests(unittest.TestCase):
    def test_siblings_header_navigation_and_access_failure_remain_distinct(self):
        result=curate_logs(**fixture());steps=result['steps']
        self.assertEqual(len(result['navigation_group_candidates']),2)
        self.assertEqual(steps[1]['later_same_navigation_group_steps'],[3,4])
        self.assertEqual(steps[2]['later_recorded_descendant_steps'],[])
        self.assertNotEqual(steps[4]['navigation_group_id'],steps[1]['navigation_group_id'])
        self.assertEqual(steps[4]['selected_parent_step'],2)
        self.assertEqual(len(steps[4]['known_alternative_entries']),2)
        self.assertEqual(steps[4]['technical_outcome'],'http_access_restricted')
        self.assertIsNone(steps[4]['semantic_value'])
        self.assertEqual(result['summary']['certified_branch_episodes'],0)

    def test_future_or_unknown_material_cannot_become_known_frontier(self):
        data=fixture()
        for item in [data['decisions'][2]['selected'],data['decisions'][2]['known_frontier'][0],data['observations'][2]['entry']]:
            item['source']['content_hash']='f'*64
        with self.assertRaisesRegex(ValueError,'future or unknown'):curate_logs(**data)

    def test_original_response_bytes_are_checked(self):
        data=fixture();data['raw_reader']=lambda name:b'unrelated bytes'
        with self.assertRaisesRegex(ValueError,'fingerprint mismatch'):curate_logs(**data)

    def test_http200_preserves_original_parser_limits_and_unknown_value(self):
        for outcome,code in [('requires_render','access_challenge'),
                             ('needs_manual','manual_challenge'),
                             ('needs_adapter','dynamic_material_incomplete'),('empty','')]:
            with self.subTest(outcome=outcome):
                data=fixture()
                data['observations'][2]['analysis'].update(
                    outcome=outcome,error_code=code,evidence=['recorded_parser_evidence'])
                step=curate_logs(**data)['steps'][2]
                self.assertEqual(step['http_status'],200)
                self.assertEqual(step['technical_outcome'],outcome)
                self.assertEqual(step['original_parser_outcome'],outcome)
                self.assertEqual(step['original_parser_error_code'],code)
                self.assertEqual(step['original_parser_evidence'],['recorded_parser_evidence'])
                self.assertIsNone(step['semantic_value'])

    def test_redirect_attempt_costs_belong_to_one_observation(self):
        data=fixture();data['decisions']=data['decisions'][:1];data['observations']=data['observations'][:1]
        obs=data['observations'][0];obs['final_url']=ROOT+'index.htm'
        original=data['attempts'][0]
        data['attempts']=[dict(original,finished_at='2026-01-01T00:00:12+00:00',seconds=1,body_bytes=9),
            dict(original,sequence=2,url=ROOT+'index.htm',started_at='2026-01-01T00:00:12+00:00',seconds=1)]
        result=curate_logs(**data)
        self.assertEqual(result['steps'][0]['cost']['http_attempts'],2)
        self.assertEqual(result['steps'][0]['cost']['measured_request_seconds'],2)

    def test_same_inputs_produce_the_same_curation(self):
        self.assertEqual(curate_logs(**fixture()),curate_logs(**fixture()))


if __name__=='__main__':unittest.main()
