"""Copy v4 staged failure and history without fixing it or contacting models."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from common import digest, dump, manifest

PROJECT = Path(__file__).resolve().parents[2]
OLD = Path('/tmp/corecoder-compare-04')
NEW = Path('/tmp/corecoder-resume-01')


def main():
    NEW.mkdir(exist_ok=False)
    protected = {f'v{i}': manifest(Path(f'/tmp/corecoder-compare-0{i}')) for i in (1, 2, 3, 4)}
    protected.update(baseline=manifest(PROJECT/'baseline'), validation=manifest(PROJECT/'validation'),
                     five={name: manifest(Path('/tmp')/name) for name in (
                         'corecoder-session-baseline-01', 'corecoder-newline-baseline-01',
                         'corecoder-agent-isolation-baseline-01', 'corecoder-bash-cwd-baseline-01',
                         'corecoder-tool-cwd-baseline-01')})
    dump(NEW/'protected-before.json', protected)
    ignore = shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache', '.ruff_cache')
    shutil.copytree(OLD/'original-source', NEW/'original-source', ignore=ignore)
    shutil.copytree(OLD/'evaluation', NEW/'evaluation', ignore=ignore)
    case = NEW/'task04-staged'
    case.mkdir()
    prior = OLD/'task04-staged'
    shutil.copytree(prior/'workspace', case/'workspace', ignore=ignore)
    assert manifest(case/'workspace') == manifest(prior/'workspace')
    shutil.copytree(prior/'runtime', case/'runtime', ignore=ignore)
    for name in ('repair.py', 'cli.py'):
        shutil.copy2(PROJECT/'corecoder'/name, case/'runtime/corecoder'/name)
    changed = [p for p,h in manifest(case/'runtime').items() if manifest(prior/'runtime').get(p) != h]
    assert changed == ['corecoder/cli.py', 'corecoder/repair.py'], changed
    raw = (prior/'run/messages.json').read_text()
    history = raw.replace(str(OLD), str(NEW)).replace(str(OLD).replace('/tmp/','/private/tmp/'), str(NEW))
    assert str(OLD) not in history
    dump(case/'history.json', json.loads(history))
    shutil.copy2(prior/'task.md', case/'original-task.md')
    task = ((prior/'task.md').read_text() + '\n\nContinuation: preserve the existing target fix. '
            'Repair remaining new regression failures and synchronize affected documentation. '
            'Do not change tests or the frozen baseline. Use only the new workspace; historical tool calls are already executed, do not replay them.')
    (case/'task.md').write_text(task)
    for cmd in (['git','init','-q','-b','codex/resume-v4-staged'],['git','add','.'],
                ['git','-c','user.name=Baseline','-c','user.email=baseline@example.invalid','commit','-q','-m','Unmodified v4 staged failed delivery snapshot']):
        subprocess.run(cmd,cwd=case/'workspace',check=True)
    subprocess.run([sys.executable,'-m','venv',str(NEW/'venv')],check=True)
    old_site = OLD/'venv/lib/python3.14/site-packages'
    new_site = NEW/'venv/lib/python3.14/site-packages'
    for p in old_site.iterdir():
        if p.name.startswith('pip') or p.name == '__pycache__':
            continue
        if p.is_dir(): shutil.copytree(p,new_site/p.name,ignore=shutil.ignore_patterns('__pycache__'))
        else: shutil.copy2(p,new_site/p.name)
    shutil.copy2(OLD/'requirements.txt',NEW/'requirements.txt')
    freeze = subprocess.check_output([str(NEW/'venv/bin/python'),'-m','pip','freeze'],text=True)
    assert set(freeze.splitlines()) == set((NEW/'requirements.txt').read_text().splitlines())
    (NEW/'dependencies.txt').write_text(freeze)
    ruff = Path('/tmp/corecoder-issue001/venv/bin/ruff')
    assert subprocess.check_output([str(ruff), '--version'], text=True).strip() == 'ruff 0.17.5'
    shutil.copy2(ruff, NEW/'venv/bin/ruff')
    settings = json.loads((prior/'case.json').read_text().replace(str(OLD),str(NEW)))
    used = json.loads((prior/'run/summary.json').read_text())['execution']
    settings['limits']['max_model_calls'] = 30-used['model_calls']
    assert settings['limits']['max_model_calls'] == 14
    settings['regression_argv'] = [str(NEW/'venv/bin/python'),'-m','pytest','tests/','-q','--ignore=tests/test_live_smoke.py']
    settings['baseline_file'] = str(NEW/'preflight/baseline.json')
    settings['baseline_workspace'] = str(NEW/'original-source')
    settings['history'] = str(case/'history.json')
    settings['protected_paths'] += [str(case/'history.json'),str(NEW/'original-source'),str(NEW/'preflight')]
    settings['start_commit'] = subprocess.check_output(['git','rev-parse','HEAD'],cwd=case/'workspace',text=True).strip()
    settings['origin'] = {'suite':str(OLD),'arm':'staged','model_calls':used['model_calls'],
                          'prompt_tokens':used['prompt_tokens'],'completion_tokens':used['completion_tokens'],
                          'seconds':used['elapsed_seconds'],'new_stage_attempt_limit':3,
                          'history_source_sha256':digest(json.loads(raw)),
                          'workspace_source':manifest(prior/'workspace')}
    dump(case/'case.json',settings)
    runner = NEW/'runner'
    runner.mkdir()
    for p in Path(__file__).parent.iterdir():
        if p.suffix == '.py' or p.name == 'README.md': shutil.copy2(p,runner/p.name)
    print(NEW)


if __name__ == '__main__':main()
