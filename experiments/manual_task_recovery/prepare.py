"""Prepare one isolated manual pause/resume exercise, without model calls."""
import shutil
import subprocess
import sys
from pathlib import Path

from common import digest, dump, manifest

PROJECT=Path(__file__).resolve().parents[2]
ROOT=Path('/tmp/corecoder-task-manual-01')


def main():
    ROOT.mkdir(exist_ok=False)
    protected={f'v{i}':manifest(Path(f'/tmp/corecoder-compare-0{i}')) for i in range(1,5)}
    protected.update(resume=manifest(Path('/tmp/corecoder-resume-01')),baseline=manifest(PROJECT/'baseline'),
                     validation=manifest(PROJECT/'validation'),
                     five={name:manifest(Path('/tmp')/name) for name in (
                         'corecoder-session-baseline-01','corecoder-newline-baseline-01',
                         'corecoder-agent-isolation-baseline-01','corecoder-bash-cwd-baseline-01','corecoder-tool-cwd-baseline-01')})
    dump(ROOT/'protected-before.json',protected)
    ignore=shutil.ignore_patterns('.git','__pycache__','.pytest_cache','.ruff_cache')
    old=Path('/tmp/corecoder-compare-04')
    shutil.copytree(old/'original-source',ROOT/'workspace',ignore=ignore)
    assert manifest(ROOT/'workspace')==manifest(old/'original-source')
    shutil.copytree(PROJECT/'corecoder',ROOT/'runtime/corecoder',ignore=ignore)
    shutil.copytree(old/'evaluation/task04',ROOT/'evaluation',ignore=ignore)
    for name in ('home','control','records'):(ROOT/name).mkdir()
    subprocess.run([sys.executable,'-m','venv',str(ROOT/'venv')],check=True)
    source=Path('/tmp/corecoder-resume-01/venv')
    for p in (source/'lib/python3.14/site-packages').iterdir():
        if p.name.startswith('pip') or p.name=='__pycache__':continue
        dest=ROOT/'venv/lib/python3.14/site-packages'/p.name
        if p.is_dir():shutil.copytree(p,dest,ignore=ignore)
        else:shutil.copy2(p,dest)
    shutil.copy2(source/'bin/ruff',ROOT/'venv/bin/ruff')
    freeze=subprocess.check_output([str(ROOT/'venv/bin/python'),'-m','pip','freeze'],text=True)
    requirements=(old/'requirements.txt').read_text()
    assert set(freeze.splitlines())==set(requirements.splitlines())
    (ROOT/'dependencies.txt').write_text(freeze)
    (ROOT/'requirements.txt').write_text(requirements)
    for cmd in (['git','init','-q','-b','codex/manual-task-recovery-01'],['git','add','.'],
                ['git','-c','user.name=Baseline','-c','user.email=baseline@example.invalid','commit','-q','-m','Unfixed task4 pause-resume start']):
        subprocess.run(cmd,cwd=ROOT/'workspace',check=True)
    protected_tests=sorted(p for p in manifest(ROOT/'workspace') if p.startswith('tests/'))
    task=(old/'task04-staged/task.md').read_text()
    config={'task':task,'workspace':str(ROOT/'workspace'),'checkpoint_dir':str(ROOT/'task'),
        'verification':{'argv':[str(ROOT/'venv/bin/python'),'-m','unittest','discover','-s',str(ROOT/'evaluation'),'-v'],
                        'protected_paths':[*protected_tests,str(ROOT/'evaluation')]},
        'regression':{'argv':[str(ROOT/'venv/bin/python'),'-m','pytest','tests/','-q','--ignore=tests/test_live_smoke.py'],
                      'protected_paths':protected_tests},
        'limits':{'max_model_calls':30,'max_attempts':3,'rounds_per_attempt':10,'max_seconds':900,
                  'verifier_seconds':60,'max_stagnant_attempts':2}}
    dump(ROOT/'task.json',config)
    scripts=ROOT/'scripts';scripts.mkdir()
    for p in Path(__file__).parent.iterdir():
        if p.suffix=='.py' or p.name=='README.md':shutil.copy2(p,scripts/p.name)
    dump(ROOT/'seal.json',{'workspace':manifest(ROOT/'workspace'),'runtime':manifest(ROOT/'runtime'),
                          'scripts':manifest(scripts),'evaluation':manifest(ROOT/'evaluation'),
                          'config':digest((ROOT/'task.json').read_text())})
    print(ROOT)


if __name__=='__main__':main()
