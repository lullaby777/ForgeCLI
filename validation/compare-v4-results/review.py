"""Local recheck on disposable workspace copies, never imports a live LLM."""
import json
import shutil
import sys
import tempfile
from pathlib import Path

SUITE=Path('/tmp/corecoder-compare-04')
OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(SUITE/'runner'))
from common import command, dump, local_env, manifest


def main():
    old_snapshot=manifest(SUITE)
    rows=[]
    for arm in ('generic','staged'):
        case=SUITE/f'task04-{arm}'
        result=json.loads((case/'run/summary.json').read_text())
        settings=json.loads((case/'case.json').read_text())
        seal=json.loads((case/'seal.json').read_text())
        current=manifest(case/'workspace')
        assert all(current.get(p)==h for p,h in seal['protected_workspace'].items())
        assert not any((case/'workspace'/p).exists() for p in settings['absent_protected'])
        assert manifest(case/'runtime')==seal['runtime']
        assert manifest(SUITE/'runner')==seal['harness']
        assert manifest(SUITE/'evaluation/task04')==seal['evaluation']
        with tempfile.TemporaryDirectory(prefix=f'v4-review-{arm}-') as tmp:
            workspace=Path(tmp)/'workspace'
            shutil.copytree(case/'workspace',workspace,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache','.ruff_cache'))
            env=local_env(workspace,Path(tmp)/'home')
            acceptance=command(settings['verification_argv'],workspace,OUT/f'{arm}-acceptance-review.txt',env,60)
            regression=command([sys.executable,'-m','pytest','tests/','-q','--ignore=tests/test_live_smoke.py',
                                '--junitxml',str(OUT/f'{arm}-regression-review.xml')],workspace,OUT/f'{arm}-regression-review.txt',env,120)
        assert acceptance['exit_code']==result['acceptance']['exit_code'] and not acceptance['timeout']
        assert regression['exit_code']==result['regression']['exit_code'] and not regression['timeout']
        import xml.etree.ElementTree as ET
        failures=sorted(t.get('classname','').replace('.','/')+'.py::'+t.get('name','')
                        for t in ET.parse(OUT/f'{arm}-regression-review.xml').iter('testcase')
                        if t.find('failure') is not None or t.find('error') is not None)
        assert failures==sorted(result['new_regression_failures']+result['known_regression_failures_present'])
        ex=result['execution'];usage=ex['feedback_usage']
        from activation import feedback_usage
        events=[json.loads(s) for s in (case/'run/model-events.jsonl').read_text().splitlines()]
        assert feedback_usage(ex['controller'],events)==usage
        rows.append({'arm':arm,'functional_passed':result['functional_passed'],'execution':ex['execution_status'],
                     'model_calls':ex['model_calls'],'prompt_tokens':ex['prompt_tokens'],
                     'completion_tokens':ex['completion_tokens'],'total_tokens':ex['prompt_tokens']+ex['completion_tokens'],
                     'seconds':ex['elapsed_seconds'],'feedback_usage':usage,
                     'new_regression_failures':result['new_regression_failures'],
                     'existing_tests_changed':False,'review_acceptance':acceptance,'review_regression':regression})
    assert manifest(SUITE)==old_snapshot
    dump(OUT/'metrics.json',{'rows':rows,'analysis_real_model_calls':0,'v4_all_files_unchanged':True,
                            'review_method':'Acceptance and full regression on disposable copies; original logs not overwritten'})
    print(json.dumps({'review_passed':True,'real_model_calls':0,'original_v4_unchanged':True},ensure_ascii=False))


if __name__=='__main__':main()
