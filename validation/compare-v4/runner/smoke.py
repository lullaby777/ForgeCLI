"""Exercise feedback linkage, reserved retries and no-progress stops without API."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from activation import feedback_usage
from common import digest, dump, manifest
from run import run_case


def check_observer():
    controller = {'attempts': [{'number': 1, 'recovery': {'kind': 'generic', 'message': 'feedback'},
                                'feedback_submitted_to_agent': True},
                               {'number': 2, 'model_calls_before': 10, 'model_calls_after': 11}]}
    usage = feedback_usage(controller, [])
    assert usage['feedback_submitted_count'] == 1 and usage['feedback_used_count'] == 0
    event = {'event': 'requested', 'index': 11, 'last_user_message_hash': digest('feedback')}
    usage = feedback_usage(controller, [event])
    assert usage['feedback_used_count'] == 1 and usage['feedback_response_count'] == 0
    usage = feedback_usage(controller, [event, {'event': 'response', 'index': 11}])
    assert usage['feedback_response_count'] == 1


def main():
    check_observer()
    suite = Path('/tmp/corecoder-compare-04')
    report = []
    request_hashes = {}
    with tempfile.TemporaryDirectory(prefix='feedback-v4-smoke-') as tmp:
        evaluation = Path(tmp)/'evaluation'
        evaluation.mkdir()
        (evaluation/'test_acceptance.py').write_text(
            'import os, unittest\nfrom pathlib import Path\nclass Check(unittest.TestCase):\n'
            ' def test_answer(self):\n'
            '  self.assertEqual((Path(os.environ["CORECODER_TARGET"])/"answer.txt").read_text(), "fixed")\n')
        for scenario in ('length', 'budget', 'recover'):
            for arm in ('generic', 'staged'):
                case = Path(tmp)/f'{scenario}-{arm}'
                case.mkdir()
                workspace = case/'workspace'
                workspace.mkdir()
                (workspace/'answer.txt').write_text('wrong')
                (workspace/'tests').mkdir()
                (workspace/'tests/test_local.py').write_text('def test_local():\n assert True\n')
                shutil.copytree(suite/f'task04-{arm}/runtime', case/'runtime')
                for cmd in (['git', 'init', '-q'], ['git', 'add', '.'],
                            ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-q', '-m', 'smoke']):
                    subprocess.run(cmd, cwd=workspace, check=True)
                template = json.loads((suite/f'task04-{arm}/case.json').read_text())
                template.update(task='synthetic', evaluation=str(evaluation),
                                protected_paths=['tests/test_local.py', str(case/'runtime'), str(evaluation)],
                                absent_protected=[], known_regression_failures=[],
                                verification_argv=[sys.executable, '-m', 'unittest', 'discover', '-s', str(evaluation), '-v'],
                                start_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=workspace, text=True).strip())
                dump(case/'case.json', template)
                (case/'task.md').write_text('Offline synthetic task. Update answer.txt only.')
                dump(case/'seal.json', {'workspace': manifest(workspace), 'runtime': manifest(case/'runtime'),
                                       'evaluation': manifest(evaluation), 'settings': digest(template),
                                       'task': digest((case/'task.md').read_text()),
                                       'protected_workspace': {'tests/test_local.py': manifest(workspace)['tests/test_local.py']}})
                result = run_case(case, False, offline_scenario=scenario)
                execution = result['execution']
                attempts = execution['controller']['attempts']
                usage = execution['feedback_usage']
                assert not result['new_regression_failures'] and not result['protected_tests_changed']
                assert execution['model_calls'] <= 30 and len(attempts) <= 3
                assert all(a['agent_run']['rounds'] <= 10 for a in attempts)
                if scenario == 'length':
                    assert execution['execution_status'] == 'no_progress' and execution['model_calls'] == 2
                    assert usage['feedback_used_count'] == 1 and usage['feedback_response_count'] == 1, (usage, (case/'run/model-events.jsonl').read_text())
                    assert (workspace/'answer.txt').read_text() == 'wrong'
                    assert not result['functional_passed']
                    assert attempts[0]['recovery']['kind'] == ('generic' if arm == 'generic' else 'truncated_without_changes')
                elif scenario == 'budget':
                    assert execution['model_calls'] == 30 and len(attempts) == 3
                    assert execution['execution_status'] == 'attempt_limit'
                    assert usage['feedback_used_count'] == 2 and usage['feedback_response_count'] == 2
                    assert [r['request_indexes'][0] for r in usage['records'] if r['model_requested_with_feedback']] == [11, 21]
                    assert not result['functional_passed']
                else:
                    assert execution['execution_status'] == 'passed' and result['functional_passed']
                    assert execution['model_calls'] == 12 and len(attempts) == 2
                    assert usage['feedback_used_count'] == 1 and usage['feedback_response_count'] == 1, (usage, (case/'run/model-events.jsonl').read_text())
                    assert usage['records'][0]['next_verification'] == 'passed'
                    assert usage['records'][0]['next_files_changed']
                    assert usage['records'][0]['request_indexes'] == [11, 12]
                if arm == 'generic':
                    expected = ('Original task: Offline synthetic task. Update answer.txt only.\nIndependent verification failed. Continue without changing '
                                'acceptance criteria or protected tests.\n'+attempts[0]['verification']['feedback'])
                    assert attempts[0]['recovery']['message'] == expected
                elif scenario != 'length':
                    assert attempts[0]['recovery']['kind'] == 'modified_verification_failed'
                events = [json.loads(line) for line in (case/'run/model-events.jsonl').read_text().splitlines()]
                hashes = [e['request_hash_normalized'] for e in events if e['event'] == 'requested']
                request_hashes[(scenario, arm)] = hashes
                report.append({'scenario': scenario, 'arm': arm, 'status': execution['execution_status'],
                               'calls': execution['model_calls'], 'attempts': len(attempts),
                               'functional_passed': result['functional_passed'], 'feedback_usage': usage})
    for scenario in ('length', 'budget', 'recover'):
        generic, staged = [request_hashes[(scenario, arm)] for arm in ('generic', 'staged')]
        # Identical fake responses before intervention; normalize synthetic case prefix.
        assert generic[0] == staged[0]
        feedback_index = 1 if scenario == 'length' else 10
        assert generic[feedback_index] != staged[feedback_index]
    dump(suite/'offline-smoke.json', {'real_model_calls': 0, 'observer_edge_cases_passed': True,
                                    'first_requests_match': True, 'recovery_requests_differ': True,
                                    'results': report})
    assert all(not (suite/f'task04-{arm}/run').exists() for arm in ('generic', 'staged'))
    print('Six offline synthetic cases passed; real task cases remain unused.')


if __name__ == '__main__':
    main()
