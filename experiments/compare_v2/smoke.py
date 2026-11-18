"""Exercise both launch paths on disposable synthetic workspaces, no domain fixes."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from common import digest, dump, manifest
from run import run_case


def main():
    suite = Path('/tmp/corecoder-compare-02')
    report = []
    hashes = []
    with tempfile.TemporaryDirectory(prefix='compare-offline-smoke-') as tmp:
        for arm in ('original', 'improved'):
            case = Path(tmp) / arm
            case.mkdir()
            workspace = case / 'workspace'
            workspace.mkdir()
            (workspace / 'answer.txt').write_text('wrong')
            (workspace / 'tests').mkdir()
            (workspace / 'tests/test_local.py').write_text('def test_local():\n    assert True\n')
            shutil.copytree(suite / f'task04-{arm}/runtime', case / 'runtime')
            evaluation = Path(tmp) / 'evaluation'
            evaluation.mkdir(exist_ok=True)
            (evaluation / 'test_acceptance.py').write_text(
                'import unittest\nfrom pathlib import Path\n'
                'class Check(unittest.TestCase):\n'
                ' def test_answer(self):\n  self.assertEqual(Path("answer.txt").read_text(), "fixed")\n')
            for cmd in (['git','init','-q'],['git','add','.'],
                        ['git','-c','user.name=Test','-c','user.email=test@example.invalid','commit','-q','-m','start']):
                subprocess.run(cmd, cwd=workspace, check=True)
            protected = ['tests/test_local.py', str(case/'runtime'), str(evaluation)]
            template = json.loads((suite / f'task04-{arm}/case.json').read_text())
            template.update(task='synthetic', evaluation=str(evaluation), protected_paths=protected,
                            absent_protected=[], known_regression_failures=[],
                            verification_argv=[sys.executable,'-m','unittest','discover','-s',str(evaluation),'-v'],
                            start_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=workspace,text=True).strip())
            dump(case/'case.json',template)
            (case/'task.md').write_text('Offline smoke only; do not change files.')
            dump(case/'seal.json',{'workspace':manifest(workspace),'runtime':manifest(case/'runtime'),
                                  'evaluation':manifest(evaluation),'settings':digest(template),'task':digest((case/'task.md').read_text()),
                                  'protected_workspace':{'tests/test_local.py':manifest(workspace)['tests/test_local.py']}})
            result = run_case(case, live=False)
            assert not result['functional_passed']
            assert not result['new_regression_failures']
            assert not result['protected_tests_changed']
            assert (workspace/'answer.txt').read_text() == 'wrong'
            event = json.loads((case/'run/model-events.jsonl').read_text().splitlines()[0])
            hashes.append(event['request_hash_normalized'])
            report.append({'arm':arm,'calls':result['execution']['model_calls'],'status':result['execution']['execution_status']})
    assert report[0]['calls'] == 1 and report[0]['status'] == 'returned_empty'
    assert report[1]['calls'] == 3 and report[1]['status'] == 'no_progress'
    assert hashes[0] == hashes[1], 'Initial normalized request differs between arms'
    dump(suite/'offline-smoke.json',{'real_model_calls':0,'initial_requests_match':True,'results':report})
    print('Offline launch/score paths passed; prepared live cases remain unused.')


if __name__ == '__main__':
    main()
