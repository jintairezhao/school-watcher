"""Reuse one proven 2.28 unit result only when its inputs are unchanged.

This is an explicit release handoff, not a general cache. Any application,
dependency, migration or unittest change falls back to the complete suite.
The repaired smoke checks and browser checks always execute again.
"""
import json
import hashlib
import os
from pathlib import Path
import subprocess
from urllib.request import Request, urlopen

BASELINE='cf6b7998881e35769eeecd3665d5c83ecf1965e6'
RUN=43199392996
JOB=131118671658
REPO='jintairezhao/school-watcher'
ROOT=Path(__file__).resolve().parents[2]
WORKFLOW_SHA='5c576789dc5f1e3a779b70233e739c68929c0608c426a094e8cfc5bc68883358'
CALLER_SHA='1d1712d6135722c917c38cfb445f7f0665fa796b8217d1f54573d35337a4577e'
ALLOWED={'.github/workflows/runtime.yml', '.github/workflows/desktop.yml', 'scripts/checks/reuse_windows_unit_acceptance.py',
    'tests/smoke/check_migration.py', 'tests/browser/check_storage_file_picker_browser.py'}


def verify(read=None):
    from desktop import VERSION
    if VERSION!='2.28' or os.environ.get('GITHUB_REPOSITORY',REPO)!=REPO:
        raise ValueError('Not the reviewed release/repository')
    workflow=(ROOT/'.github/workflows/runtime.yml').read_bytes().replace(b'\r\n',b'\n')
    if hashlib.sha256(workflow).hexdigest()!=WORKFLOW_SHA:
        raise ValueError('Runner, interpreter or gate definition changed')
    caller=(ROOT/'.github/workflows/desktop.yml').read_bytes().replace(b'\r\n',b'\n')
    if hashlib.sha256(caller).hexdigest()!=CALLER_SHA:
        raise ValueError('Caller workflow changed')
    changed=set(subprocess.check_output(['git','diff','--name-only',BASELINE],cwd=ROOT,text=True).splitlines())
    if changed-ALLOWED:
        raise ValueError('Unit inputs changed: '+', '.join(sorted(changed-ALLOWED)))
    if read is None:
        def read(path):
            headers={'Accept':'application/vnd.github+json','User-Agent':'school-watcher-release-acceptance'}
            if os.environ.get('GH_TOKEN'):
                headers['Authorization']='Bearer '+os.environ['GH_TOKEN']
            with urlopen(Request('https://api.github.com/repos/'+REPO+'/'+path,headers=headers),timeout=20) as response:
                return json.load(response)
    run=read(f'actions/runs/{RUN}')
    job=read(f'actions/jobs/{JOB}')
    if (run.get('head_sha')!=BASELINE or run.get('path')!='.github/workflows/desktop.yml'
            or job.get('run_id')!=RUN or job.get('name')!='acceptance / windows-units'
            or 'windows-latest' not in job.get('labels',[])):
        raise ValueError('Prior execution identity does not match')
    command='Run python -m unittest discover -s tests -p "test_*.py" -v'
    if not any(step.get('name')==command and step.get('status')=='completed'
            and step.get('conclusion')=='success' for step in job.get('steps',[])):
        raise ValueError('No successful matching Windows unit step')
    return dict(reused=True,source_commit=BASELINE,run_url=run['html_url'],job_id=JOB,
        unchanged_unit_inputs=True,reviewed_non_unit_changes=sorted(changed))


def main():
    try:
        result=verify()
    except Exception as error:
        # Inability to verify is a cache miss, never acceptance success.
        result=dict(reused=False,reason=type(error).__name__+': '+str(error))
    print(json.dumps(result,ensure_ascii=False))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as output:
            output.write('reused='+str(result['reused']).lower()+'\n')


if __name__=='__main__':
    import sys
    sys.path.insert(0,str(ROOT))
    main()
