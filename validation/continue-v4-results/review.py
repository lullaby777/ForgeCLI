"""Review saved continuation evidence and recheck a disposable copy; no models."""
import json
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

SUITE=Path('/tmp/corecoder-resume-01')
OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(SUITE/'runner'))
from common import command, digest, dump, local_env, manifest


def main():
    snapshot=manifest(SUITE)
    case=SUITE/'task04-staged'
    settings=json.loads((case/'case.json').read_text())
    seal=json.loads((case/'seal.json').read_text())
    record=json.loads((case/'run/summary.json').read_text())
    current=manifest(case/'workspace')
    assert all(current.get(p)==h for p,h in seal['protected_workspace'].items())
    assert not any((case/'workspace'/p).exists() for p in settings['absent_protected'])
    assert manifest(case/'runtime')==seal['runtime']
    assert manifest(SUITE/'runner')==seal['harness']
    assert manifest(Path(settings['evaluation']))==seal['evaluation']
    assert manifest(Path(settings['baseline_workspace']))==seal['baseline_workspace']
    assert digest(settings)==seal['settings']
    for name,path in [('history',Path(settings['history'])),('baseline',Path(settings['baseline_file'])),('task',case/'task.md')]:
        assert digest(path.read_text())==seal[name]
    origin=settings['origin']['workspace_source']
    changed=sorted(p for p in current.keys()|origin.keys() if current.get(p)!=origin.get(p))
    assert changed==['article/02-tools.md','article/02-tools_EN.md','tests/test_bash_cwd_tracking.py']
    assert all(current.get(p)==h for p,h in origin.items() if p.startswith('corecoder/') or p.startswith('tests/'))
    initial=json.loads((case/'run/initial/initial.json').read_text())
    assert initial['acceptance']['status']=='passed'
    assert initial['regression']['new_failures']==['tests.test_core::test_doc_python_line_references_are_current']
    history=json.loads(Path(settings['history']).read_text())
    messages=json.loads((case/'run/messages.json').read_text())
    assert messages[:len(history)]==history
    users=[m for m in messages[len(history):] if m.get('role')=='user']
    assert len(users)==1 and initial['feedback']['message'] in users[0]['content']
    events=[json.loads(line) for line in (case/'run/model-events.jsonl').read_text().splitlines()]
    requests=[e for e in events if e['event']=='requested']
    responses=[e for e in events if e['event']=='response']
    assert len(requests)==len(responses)==10
    assert all(e['contains_initial_feedback'] and e['last_user_message_hash']==digest(users[0]['content']) for e in requests)
    ex=record['execution']
    assert sum(e['prompt_tokens'] for e in responses)==ex['prompt_tokens']
    assert sum(e['completion_tokens'] for e in responses)==ex['completion_tokens']
    assert ex['controller']['attempts'][0]['agent_status']=='round_limit'
    with tempfile.TemporaryDirectory(prefix='resume-result-review-') as tmp:
        workspace=Path(tmp)/'workspace'
        shutil.copytree(case/'workspace',workspace,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache','.ruff_cache'))
        env=local_env(workspace,Path(tmp)/'home')
        acceptance=command(settings['verification_argv'],workspace,OUT/'acceptance-review.txt',env,60)
        regression=command([sys.executable,'-m','pytest','tests/','-q','--ignore=tests/test_live_smoke.py',
                            '--junitxml',str(OUT/'regression-review.xml')],workspace,OUT/'regression-review.txt',env,120)
    assert acceptance['exit_code']==0 and not acceptance['timeout']
    assert regression['exit_code']==1 and not regression['timeout']
    tests={}
    for t in ET.parse(OUT/'regression-review.xml').iter('testcase'):
        key=t.get('classname','')+'::'+t.get('name','')
        tests[key]='failed' if t.find('failure') is not None or t.find('error') is not None else 'skipped' if t.find('skipped') is not None else 'passed'
    assert [key for key,value in tests.items() if value=='failed']==['tests.test_core::test_readme_line_counts_are_current']
    baseline=json.loads(Path(settings['baseline_file']).read_text())
    assert all(key in tests and (outcome=='skipped' or tests[key]!='skipped') for key,outcome in baseline['tests'].items())
    assert tests['tests.test_core::test_doc_python_line_references_are_current']=='passed'
    assert sum(value=='passed' for value in tests.values())==367
    assert manifest(SUITE)==snapshot
    dump(OUT/'metrics.json',{'real_model_calls':0,'review_acceptance':acceptance,'review_regression':regression,
        'functional_passed':record['functional_passed'],'completed':ex['completed'],'existing_tests_unchanged':True,
        'bash_source_unchanged_from_v4':True,'changed_files':changed,'new_test_cases':15,
        'restored_history_messages':len(history),'initial_feedback_used_and_response_returned':True,
        'retry_feedback_count':ex['feedback_usage']['feedback_used_count'],
        'new_calls':10,'v4_calls':16,'total_calls':26,'prompt_tokens':ex['prompt_tokens'],
        'completion_tokens':ex['completion_tokens'],'total_tokens':ex['prompt_tokens']+ex['completion_tokens'],
        'seconds':ex['elapsed_seconds'],'agent_stop':'round_limit','controller_stop':'passed',
        'original_resume_suite_unchanged':True})
    print('Local review passed; no model calls; original continuation files unchanged.')


if __name__=='__main__':main()
