"""Verify feedback-only intervention using disposable synthetic cases, no API."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from common import digest, dump, manifest
from run import run_case


def main():
    suite=Path('/tmp/corecoder-compare-03');report=[];hashes=[]
    with tempfile.TemporaryDirectory(prefix='feedback-smoke-') as tmp:
        evaluation=Path(tmp)/'evaluation';evaluation.mkdir()
        (evaluation/'test_acceptance.py').write_text('import unittest\nclass Check(unittest.TestCase):\n def test_answer(self):\n  self.fail("synthetic failure")\n')
        for arm in ('generic','staged'):
            case=Path(tmp)/arm;case.mkdir();workspace=case/'workspace';workspace.mkdir()
            (workspace/'answer.txt').write_text('wrong');(workspace/'tests').mkdir()
            (workspace/'tests/test_local.py').write_text('def test_local():\n assert True\n')
            shutil.copytree(suite/f'task04-{arm}/runtime',case/'runtime')
            for cmd in (['git','init','-q'],['git','add','.'],['git','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-q','-m','smoke']):
                subprocess.run(cmd,cwd=workspace,check=True)
            template=json.loads((suite/f'task04-{arm}/case.json').read_text())
            template.update(task='synthetic',evaluation=str(evaluation),protected_paths=['tests/test_local.py',str(case/'runtime'),str(evaluation)],
                            absent_protected=[],known_regression_failures=[],
                            verification_argv=[sys.executable,'-m','unittest','discover','-s',str(evaluation),'-v'],
                            start_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=workspace,text=True).strip())
            dump(case/'case.json',template);(case/'task.md').write_text('Offline smoke; do not change files.')
            dump(case/'seal.json',{'workspace':manifest(workspace),'runtime':manifest(case/'runtime'),
                                  'evaluation':manifest(evaluation),'settings':digest(template),'task':digest((case/'task.md').read_text()),
                                  'protected_workspace':{'tests/test_local.py':manifest(workspace)['tests/test_local.py']}})
            result=run_case(case,False);execution=result['execution'];attempts=execution['controller']['attempts']
            assert execution['execution_status']=='no_progress' and execution['model_calls']==2
            assert not result['functional_passed'] and not result['new_regression_failures']
            assert (workspace/'answer.txt').read_text()=='wrong'
            assert all('facts' in a for a in attempts)
            feedback=attempts[0]['recovery']
            if arm=='generic':
                expected=('Original task: Offline smoke; do not change files.\nIndependent verification failed. Continue without changing '
                          'acceptance criteria or protected tests.\n'+attempts[0]['verification']['feedback'])
                assert feedback['message']==expected
            else:
                assert feedback['kind']=='truncated_without_changes'
                assert 'one minimal tool action' in feedback['message']
            requests=[json.loads(line) for line in (case/'run/model-events.jsonl').read_text().splitlines() if json.loads(line)['event']=='requested']
            hashes.append([r['request_hash_normalized'] for r in requests])
            report.append({'arm':arm,'status':execution['execution_status'],'calls':execution['model_calls'],
                           'feedback_kind':feedback['kind'],'feedback_chars':feedback['message_chars']})
    assert hashes[0][0]==hashes[1][0]
    assert hashes[0][1]!=hashes[1][1]
    dump(suite/'offline-smoke.json',{'real_model_calls':0,'first_requests_match':True,
                                   'recovery_requests_differ':True,'generic_matches_v2_template':True,'results':report})
    print('Feedback-only smoke passed; real task cases remain unused.')


if __name__=='__main__':
    main()
