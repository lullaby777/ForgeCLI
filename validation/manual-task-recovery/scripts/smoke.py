"""Disposable copy: real CLI and task machinery, simulated provider, no API."""
import json
import shutil
import sys
import tempfile
from pathlib import Path

from common import command, digest, dump, local_env, manifest

ROOT=Path(__file__).resolve().parents[1]


def main():
    original=manifest(ROOT/'workspace')
    with tempfile.TemporaryDirectory(prefix='manual-task-smoke-') as temp:
        target=Path(temp)
        for name in ('workspace','runtime','evaluation','scripts','preflight'):
            shutil.copytree(ROOT/name,target/name,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache','.ruff_cache'))
        for name in ('home','records','control'):(target/name).mkdir()
        config=(ROOT/'task.json').read_text().replace(str(ROOT),str(target)).replace(str(ROOT).replace('/private/tmp/','/tmp/'),str(target))
        settings=json.loads(config)
        settings['verification']['argv'][0]=sys.executable
        settings['regression']['argv'][0]=sys.executable
        config=json.dumps(settings,ensure_ascii=False,indent=2)
        (target/'task.json').write_text(config)
        dump(target/'seal.json',{'workspace':manifest(target/'workspace'),'runtime':manifest(target/'runtime'),
                                'evaluation':manifest(target/'evaluation'),'scripts':manifest(target/'scripts'),
                                'config':digest(config)})
        result=command([sys.executable,str(target/'scripts/drive_offline.py'),str(target)],target/'scripts',
                        ROOT/'offline-cli.txt',local_env(target/'workspace',target/'home'),120)
        assert result['exit_code']==0,(ROOT/'offline-cli.txt').read_text()[-6000:]
        report=json.loads((target/'offline-flow.json').read_text())
        dump(ROOT/'offline-flow.json',report)
        shutil.copytree(target/'records',ROOT/'offline-records')
    assert manifest(ROOT/'workspace')==original
    assert not (ROOT/'task').exists() and not (ROOT/'control/safe-pause.json').exists()
    assert not list((ROOT/'home/.corecoder/sessions').glob('*.json'))
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
