"""Seal the prepared case after successful offline baseline checks."""
import json
from pathlib import Path

from common import digest, dump, manifest


def seal_case(case, runner):
    settings=json.loads((case/'case.json').read_text())
    workspace=manifest(case/'workspace')
    protected={p:h for p,h in workspace.items() if p.startswith('tests/') or p=='pyproject.toml'}
    dump(case/'seal.json',{'workspace':workspace,'runtime':manifest(case/'runtime'),
        'evaluation':manifest(Path(settings['evaluation'])),'baseline_workspace':manifest(Path(settings['baseline_workspace'])),
        'harness':manifest(runner),'settings':digest(settings),'task':digest((case/'task.md').read_text()),
        'history':digest(Path(settings['history']).read_text()),'baseline':digest(Path(settings['baseline_file']).read_text()),
        'protected_workspace':protected})


if __name__=='__main__':
    suite=Path('/tmp/corecoder-resume-01')
    case=suite/'task04-staged'
    assert not (case/'seal.json').exists()
    settings=json.loads((case/'case.json').read_text())
    assert manifest(case/'workspace')==settings['origin']['workspace_source']
    initial=json.loads((suite/'preflight/initial.json').read_text())
    assert initial['regression']['status']=='failed' and initial['acceptance']['status']=='passed'
    seal_case(case,suite/'runner')
    print('Sealed failed-delivery case; no source fix applied.')
