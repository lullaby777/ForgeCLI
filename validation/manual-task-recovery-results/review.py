"""Read saved recovery evidence and recheck a disposable copy; no model calls."""
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT=Path('/tmp/corecoder-task-manual-01')
OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'scripts'))
from common import command,digest,dump,local_env,manifest
sys.path.insert(0,str(ROOT/'runtime'))
from corecoder.task_store import checksum,read_record


def main():
    before=manifest(ROOT)
    state=read_record(ROOT/'task/state.json')
    baseline=read_record(ROOT/'task/baseline.json')
    paused=json.loads((ROOT/'records/paused.json').read_text())
    restored=json.loads((ROOT/'records/restored.json').read_text())
    seal=json.loads((ROOT/'seal.json').read_text())
    assert paused['task_id']==restored['task_id']==state['id']
    assert paused['ledger']==restored['ledger']
    assert state['ledger']['model_calls']==11 and paused['ledger']['model_calls']==7
    assert state['ledger']['attempts']==2 and state['ledger']['rounds']==3
    assert state['ledger']['active_seconds']>=paused['ledger']['active_seconds']
    assert state['ledger']['stagnant']==2 and state['status']=='no_progress'
    assert state['pending'] is None and not state['completed']
    assert checksum(state['config'])==state['config_hash']==baseline['config_hash']
    assert checksum(baseline)==state['baseline_hash']==paused['baseline_hash']==restored['baseline_hash']
    sha=hashlib.sha256((ROOT/'task/baseline.json').read_bytes()).hexdigest()
    assert sha==paused['baseline_file_sha256']==restored['baseline_file_sha256']
    assert baseline['result']['failures']==paused['original_failures']==restored['original_failures']
    assert manifest(ROOT/'workspace')==seal['workspace']
    assert manifest(ROOT/'runtime')==seal['runtime']
    assert manifest(ROOT/'scripts')==seal['scripts']
    assert manifest(ROOT/'evaluation')==seal['evaluation']
    assert digest((ROOT/'task.json').read_text())==seal['config']
    assert all(item['phase']=='acceptance' for item in state['history'])
    with tempfile.TemporaryDirectory(prefix='manual-recovery-review-') as temp:
        workspace=Path(temp)/'workspace'
        shutil.copytree(ROOT/'workspace',workspace,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache','.ruff_cache'))
        env=local_env(workspace,Path(temp)/'home')
        acceptance=command(state['config']['verification']['argv'],workspace,OUT/'acceptance-review.txt',env,60)
        regression=command([sys.executable,'-m','pytest','tests/','-q','--ignore=tests/test_live_smoke.py',
                            '--junitxml',str(OUT/'regression-review.xml')],workspace,OUT/'regression-review.txt',env,120)
    assert acceptance['exit_code']==1 and not acceptance['timeout']
    assert 'FAILED (failures=9)' in (OUT/'acceptance-review.txt').read_text()
    assert regression['exit_code']==1 and not regression['timeout']
    import xml.etree.ElementTree as ET
    failures=[t.get('classname','')+'::'+t.get('name','') for t in ET.parse(OUT/'regression-review.xml').iter('testcase')
              if t.find('failure') is not None or t.find('error') is not None]
    assert failures==baseline['result']['failures']
    assert '1 failed, 352 passed' in (OUT/'regression-review.txt').read_text()
    assert manifest(ROOT)==before
    end=state['ledger'];start=paused['ledger']
    dump(OUT/'metrics.json',{'real_model_calls':0,'task_identity_preserved':True,'restored_ledger_exactly_preserved':True,
      'original_baseline_bytes_preserved':True,'workspace_has_no_changes':True,
      'task_id':state['id'],'paused_ledger':start,'final_ledger':end,
      'resumed_model_calls':end['model_calls']-start['model_calls'],
      'resumed_active_seconds':end['active_seconds']-start['active_seconds'],
      'known_total_tokens':end['prompt_tokens']+end['completion_tokens'],
      'known_resume_tokens':end['prompt_tokens']+end['completion_tokens']-start['prompt_tokens']-start['completion_tokens'],
      'status':state['status'],'completed':state['completed'],'acceptance_failures':9,'acceptance_tests':14,
      'post_edit_regression_in_controller':'not_run: acceptance never passed',
      'independent_regression':{'passed':352,'known_failures':1,'new_failures':[]},
      'original_manual_suite_unchanged':True})
    print('Review passed: recovery state retained, repair incomplete; original evidence unchanged.')


if __name__=='__main__':main()
