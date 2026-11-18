"""Clone the frozen v1 experiment; change only task04 output allowance."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from common import digest, dump, manifest

OLD = Path('/tmp/corecoder-compare-01')
NEW = Path('/tmp/corecoder-compare-02')


def main():
    NEW.mkdir(exist_ok=False)
    original_protection = json.loads(Path('/tmp/corecoder-issue001/protected-manifest.json').read_text())
    dump(NEW/'protected-before.json', {'original': original_protection, 'v1': manifest(OLD)})
    shutil.copytree(OLD/'original-source', NEW/'original-source', ignore=shutil.ignore_patterns(
        '.git', '__pycache__', '.pytest_cache', '.ruff_cache'))
    shutil.copy2(OLD/'requirements.txt', NEW/'requirements.txt')
    subprocess.run([sys.executable, '-m', 'venv', str(NEW/'venv')], check=True)
    evaluation = NEW/'evaluation/task04'
    shutil.copytree(OLD/'evaluation/task04', evaluation, ignore=shutil.ignore_patterns('__pycache__'))
    for arm in ('original', 'improved'):
        before, case = OLD/f'task04-{arm}', NEW/f'task04-{arm}'
        case.mkdir()
        settings = json.loads((before/'case.json').read_text())
        old_seal = json.loads((before/'seal.json').read_text())
        shutil.copytree(NEW/'original-source', case/'workspace')
        shutil.copytree(before/'runtime', case/'runtime', ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
        assert manifest(case/'workspace') == old_seal['workspace']
        assert manifest(case/'runtime') == old_seal['runtime']
        assert manifest(evaluation) == old_seal['evaluation']
        for cmd in (['git','init','-q','-b',f'codex/compare-v2-task04-{arm}'], ['git','add','.'],
                    ['git','-c','user.name=Baseline','-c','user.email=baseline@example.invalid','commit','-q','-m','V2 identical task start']):
            subprocess.run(cmd,cwd=case/'workspace',check=True)
        shutil.copy2(before/'task.md',case/'task.md')
        settings = json.loads(json.dumps(settings).replace(str(OLD),str(NEW)))
        settings['model']['max_tokens'] = 8192
        settings['start_commit'] = subprocess.check_output(['git','rev-parse','HEAD'],cwd=case/'workspace',text=True).strip()
        dump(case/'case.json',settings)
        dump(case/'seal.json',{'workspace':manifest(case/'workspace'),'runtime':manifest(case/'runtime'),
                               'evaluation':manifest(evaluation),'settings':digest(settings),'task':digest((case/'task.md').read_text()),
                               'protected_workspace':old_seal['protected_workspace']})
    plan = json.loads((OLD/'plan.json').read_text())
    plan['model']['max_tokens'] = 8192
    plan.update(tasks=['task04'], previous_suite=str(OLD), changed_variable='model.max_tokens: 4096 -> 8192')
    dump(NEW/'plan.json',plan)
    print(NEW)


def freeze_runner():
    destination = NEW/'runner'
    destination.mkdir(exist_ok=False)
    for p in Path(__file__).parent.iterdir():
        if p.suffix=='.py' or p.name=='README.md':
            shutil.copy2(p,destination/p.name)
    for arm in ('original','improved'):
        case=NEW/f'task04-{arm}'
        seal=json.loads((case/'seal.json').read_text())
        seal['harness']=manifest(destination)
        dump(case/'seal.json',seal)


if __name__ == '__main__':
    main()
    freeze_runner()
