"""Prove detection/retry on disposable copies of the actual v4 failed snapshot."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from common import command, dump, local_env, manifest
from finalize import seal_case
from run import run_case

SUITE=Path('/tmp/corecoder-resume-01')


def main():
    original=manifest(SUITE/'task04-staged')
    results=[]
    for scenario in ('stay','fix'):
        with tempfile.TemporaryDirectory(prefix='v4-continuation-smoke-') as tmp:
            root=Path(tmp);case=root/'task04-staged';case.mkdir()
            for name in ('workspace','runtime'):
                shutil.copytree(SUITE/'task04-staged'/name,case/name,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache'))
            shutil.copytree(SUITE/'original-source',root/'original-source')
            shutil.copytree(SUITE/'evaluation',root/'evaluation')
            for name in ('history.json','task.md'):
                text=(SUITE/'task04-staged'/name).read_text().replace(str(SUITE),str(root)).replace(str(SUITE.resolve()),str(root))
                (case/name).write_text(text)
            settings=json.loads((SUITE/'task04-staged/case.json').read_text().replace(str(SUITE),str(root)))
            # Quick smoke uses the two real doc checks. Formal preflight uses the full suite.
            settings['verification_argv'][0]=sys.executable
            settings['regression_argv']=[sys.executable,'-m','pytest','-q',
                'tests/test_core.py::test_readme_line_counts_are_current',
                'tests/test_core.py::test_doc_python_line_references_are_current']
            dump(case/'case.json',settings)
            probe_result=command([sys.executable,'-c',
                'from pathlib import Path; from probe import probe; '
                'probe(Path('+repr(str(case))+'),Path('+repr(str(root/'preflight'))+'))'],
                Path(__file__).parent,root/'probe.txt',local_env(case/'workspace',root/'probe-home'),60)
            assert probe_result['exit_code']==0, (root/'probe.txt').read_text()
            seal_case(case,Path(__file__).parent)
            result=run_case(case,False,scenario)
            ex=result['execution'];controller=ex['controller']
            assert ex['restored_messages']>0 and ex['initial_feedback_usage']['requested']
            assert ex['initial_feedback_usage']['response_returned']
            assert len(controller['regression_baseline']['failures'])==1
            assert controller['regression_baseline']['failures'][0].endswith('::test_readme_line_counts_are_current')
            assert ex['total_calls_with_v4']<=30 and ex['model_calls']<=14
            assert result['integrity_ok'] and not result['protected_tests_changed']
            if scenario=='stay':
                assert not result['functional_passed'] and not ex['completed']
                assert ex['execution_status']=='no_progress' and ex['model_calls']==2
                assert result['acceptance']['status']=='passed' and result['regression']['status']=='failed'
                assert ex['feedback_usage']['feedback_used_count']==1
            else:
                assert result['functional_passed'] and ex['completed'] and ex['model_calls']==3
                assert [a['regression']['status'] for a in controller['attempts']]==['failed','passed']
                assert ex['feedback_usage']['feedback_used_count']==1
                changes=subprocess.check_output(['git','diff','--name-only',settings['start_commit']],cwd=case/'workspace',text=True).splitlines()
                assert changes==['article/02-tools.md'],changes
            results.append({'scenario':scenario,'real_model_calls':0,'status':ex['execution_status'],
                            'functional_passed':result['functional_passed'],'scripted_calls':ex['model_calls'],
                            'initial_feedback':ex['initial_feedback_usage'],'retry_feedback':ex['feedback_usage']})
    assert manifest(SUITE/'task04-staged')==original
    assert not (SUITE/'task04-staged/run').exists()
    env=os.environ.copy();env.pop('CORECODER_API_KEY',None)
    guards=[]
    for args in (['--live'],[]):
        p=subprocess.run([sys.executable,str(Path(__file__).with_name('run.py')),*args],env=env,capture_output=True,text=True,check=False)
        assert p.returncode!=0
        guards.append({'arguments':args,'exit_code':p.returncode,'output':p.stdout+p.stderr})
    dump(SUITE/'offline-smoke.json',{'real_model_calls':0,'formal_case_unchanged':True,'checks':results,'launch_guards':guards,
                                  'scope':'Actual v4 failed snapshot, two real doc checks for smoke; full suite checked in preflight'})
    print('Offline continuation detection, feedback, retry and completion passed; formal case unused.')


if __name__=='__main__':main()
