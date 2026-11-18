"""Prepare a feedback-only ablation; never initialize a real model."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from common import digest, dump, manifest

PROJECT = Path(__file__).resolve().parents[2]
OLD = Path('/tmp/corecoder-compare-02')
NEW = Path('/tmp/corecoder-compare-03')


def freeze_runner():
    runner = NEW/'runner'
    runner.mkdir(exist_ok=False)
    for p in Path(__file__).parent.iterdir():
        if p.suffix=='.py' or p.name=='README.md':
            shutil.copy2(p,runner/p.name)
    for arm in ('generic','staged'):
        path=NEW/f'task04-{arm}/seal.json'
        seal=json.loads(path.read_text());seal['harness']=manifest(runner);dump(path,seal)


def main():
    NEW.mkdir(exist_ok=False)
    dump(NEW/'protected-before.json',{'v1':manifest(Path('/tmp/corecoder-compare-01')),'v2':manifest(OLD),
                                     'baseline':manifest(PROJECT/'baseline'),'validation':manifest(PROJECT/'validation'),
                                     'five':{name:manifest(Path('/tmp')/name) for name in (
                                         'corecoder-session-baseline-01','corecoder-newline-baseline-01',
                                         'corecoder-agent-isolation-baseline-01','corecoder-bash-cwd-baseline-01','corecoder-tool-cwd-baseline-01')}})
    shutil.copytree(OLD/'original-source', NEW/'original-source',ignore=shutil.ignore_patterns(
        '.git','__pycache__','.pytest_cache','.ruff_cache'))
    runtime=NEW/'runtime-base'
    shutil.copytree(OLD/'task04-improved/runtime',runtime,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache'))
    shutil.copy2(PROJECT/'corecoder/repair.py',runtime/'corecoder/repair.py')
    changed=[p for p,h in manifest(runtime).items() if manifest(OLD/'task04-improved/runtime').get(p)!=h]
    assert changed==['corecoder/repair.py'],changed
    shutil.copy2(OLD/'requirements.txt',NEW/'requirements.txt')
    subprocess.run([sys.executable,'-m','venv',str(NEW/'venv')],check=True)
    shutil.copytree(OLD/'evaluation/task04',NEW/'evaluation/task04',ignore=shutil.ignore_patterns('__pycache__'))
    prior_case=OLD/'task04-improved'
    prior=json.loads((prior_case/'case.json').read_text());prior_seal=json.loads((prior_case/'seal.json').read_text())
    for arm in ('generic','staged'):
        case=NEW/f'task04-{arm}';case.mkdir()
        shutil.copytree(NEW/'original-source',case/'workspace')
        shutil.copytree(runtime,case/'runtime')
        assert manifest(case/'workspace')==prior_seal['workspace']
        for cmd in (['git','init','-q','-b',f'codex/compare-v3-task04-{arm}'],['git','add','.'],
                    ['git','-c','user.name=Baseline','-c','user.email=baseline@example.invalid','commit','-q','-m','Feedback-only experiment start']):
            subprocess.run(cmd,cwd=case/'workspace',check=True)
        settings=json.loads(json.dumps(prior).replace(str(prior_case),str(case)).replace(str(OLD),str(NEW)))
        settings.update(arm=arm,recovery_strategy=arm,
                        start_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=case/'workspace',text=True).strip())
        dump(case/'case.json',settings)
        shutil.copy2(prior_case/'task.md',case/'task.md')
        dump(case/'seal.json',{'workspace':manifest(case/'workspace'),'runtime':manifest(case/'runtime'),
                               'evaluation':manifest(NEW/'evaluation/task04'),'settings':digest(settings),
                               'task':digest((case/'task.md').read_text()),'protected_workspace':prior_seal['protected_workspace']})
    plan=json.loads((OLD/'plan.json').read_text())
    plan.update(comparison='generic vs staged recovery feedback; identical instrumented runtime',
                changed_variable='recovery_strategy',arms=['generic','staged'],previous_suite=str(OLD))
    dump(NEW/'plan.json',plan)
    freeze_runner()
    print(NEW)


if __name__=='__main__':
    main()
