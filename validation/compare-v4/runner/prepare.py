"""Freeze v3 code and clean task starts; allocate ten rounds per attempt. No API."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from common import digest, dump, manifest

PROJECT = Path(__file__).resolve().parents[2]
OLD = Path('/tmp/corecoder-compare-03')
NEW = Path('/tmp/corecoder-compare-04')


def main():
    NEW.mkdir(exist_ok=False)
    protected = {f'v{i}': manifest(Path(f'/tmp/corecoder-compare-0{i}')) for i in (1, 2, 3)}
    protected.update(baseline=manifest(PROJECT/'baseline'), validation=manifest(PROJECT/'validation'),
                     five={name: manifest(Path('/tmp')/name) for name in (
                         'corecoder-session-baseline-01', 'corecoder-newline-baseline-01',
                         'corecoder-agent-isolation-baseline-01', 'corecoder-bash-cwd-baseline-01',
                         'corecoder-tool-cwd-baseline-01')})
    dump(NEW/'protected-before.json', protected)
    ignore = shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache', '.ruff_cache')
    shutil.copytree(OLD/'original-source', NEW/'original-source', ignore=ignore)
    shutil.copytree(OLD/'runtime-base', NEW/'runtime-base', ignore=ignore)
    assert manifest(NEW/'runtime-base') == manifest(OLD/'task04-generic/runtime')
    assert manifest(NEW/'runtime-base') == manifest(OLD/'task04-staged/runtime')
    shutil.copy2(OLD/'requirements.txt', NEW/'requirements.txt')
    subprocess.run([sys.executable, '-m', 'venv', str(NEW/'venv')], check=True)
    shutil.copytree(OLD/'evaluation/task04', NEW/'evaluation/task04', ignore=ignore)
    for arm in ('generic', 'staged'):
        prior_case = OLD/f'task04-{arm}'
        prior = json.loads((prior_case/'case.json').read_text())
        prior_seal = json.loads((prior_case/'seal.json').read_text())
        case = NEW/f'task04-{arm}'
        case.mkdir()
        shutil.copytree(NEW/'original-source', case/'workspace')
        shutil.copytree(NEW/'runtime-base', case/'runtime')
        assert manifest(case/'workspace') == prior_seal['workspace']
        for cmd in (['git', 'init', '-q', '-b', f'codex/compare-v4-task04-{arm}'], ['git', 'add', '.'],
                    ['git', '-c', 'user.name=Baseline', '-c', 'user.email=baseline@example.invalid',
                     'commit', '-q', '-m', 'Ten-round feedback experiment start']):
            subprocess.run(cmd, cwd=case/'workspace', check=True)
        settings = json.loads(json.dumps(prior).replace(str(OLD), str(NEW)))
        settings['limits']['rounds_per_attempt'] = 10
        settings['start_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=case/'workspace', text=True).strip()
        dump(case/'case.json', settings)
        shutil.copy2(prior_case/'task.md', case/'task.md')
        dump(case/'seal.json', {'workspace': manifest(case/'workspace'), 'runtime': manifest(case/'runtime'),
                               'evaluation': manifest(NEW/'evaluation/task04'), 'settings': digest(settings),
                               'task': digest((case/'task.md').read_text()),
                               'protected_workspace': prior_seal['protected_workspace']})
    plan = json.loads((OLD/'plan.json').read_text())
    plan['limits']['rounds_per_attempt'] = 10
    plan.update(previous_suite=str(OLD), changes_from_previous=['rounds_per_attempt: 30 -> 10',
                'observer-only feedback request/response linkage'],
                feedback_used_definition='Matching feedback hash in a requested model input; response-returned recorded separately. Not proof of comprehension.')
    dump(NEW/'plan.json', plan)
    runner = NEW/'runner'
    runner.mkdir()
    for p in Path(__file__).parent.iterdir():
        if p.suffix == '.py' or p.name == 'README.md':
            shutil.copy2(p, runner/p.name)
    for arm in ('generic', 'staged'):
        path = NEW/f'task04-{arm}/seal.json'
        seal = json.loads(path.read_text())
        seal['harness'] = manifest(runner)
        dump(path, seal)
    print(NEW)


if __name__ == '__main__':
    main()
