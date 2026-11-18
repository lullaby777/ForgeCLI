"""Check frozen bug instances and existing regression tests, entirely offline."""
import json
import sys
from pathlib import Path

from common import command, dump, local_env, manifest


def main():
    suite = Path('/tmp/corecoder-compare-03')
    output = suite / 'preflight'
    output.mkdir(exist_ok=False)
    results = {}
    baseline = manifest(suite / 'original-source')
    for task, expected in (('task04', 9),):
        for arm in ('generic', 'staged'):
            case = suite / f'{task}-{arm}'
            assert manifest(case / 'workspace') == baseline
            settings = json.loads((case / 'case.json').read_text())
            result = command(settings['verification_argv'], case / 'workspace', output / f'{task}-{arm}.txt',
                             local_env(case / 'workspace', output / 'home'), 60)
            text = (output / f'{task}-{arm}.txt').read_text()
            assert result['exit_code'] == 1 and f'FAILED (failures={expected})' in text
            results[f'{task}-{arm}'] = {'expected_acceptance_failures': expected, 'workspace_matches': True}
    regression = command([sys.executable, '-m', 'pytest', 'tests/', '-q', '--ignore=tests/test_live_smoke.py',
                          '--junitxml', str(output / 'regression.xml')], suite / 'original-source', output / 'regression.txt',
                         local_env(suite / 'original-source', output / 'home'), 120)
    text = (output / 'regression.txt').read_text()
    assert regression['exit_code'] == 1 and '1 failed, 352 passed' in text
    results['regression'] = {'passed': 352, 'known_failed': 1, 'live_smoke_excluded': True}
    results['real_model_calls'] = 0
    dump(output / 'result.json', results)
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
