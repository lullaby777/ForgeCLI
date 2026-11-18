"""Independent checks of the old clean baseline and the copied failed delivery."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from common import dump, manifest


def probe(case, output, baseline=None):
    case = Path(case).resolve()
    settings = json.loads((case/'case.json').read_text())
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0,str(case/'runtime'))
    from corecoder.repair import (
        RegressionSpec,
        RepairController,
        VerificationSpec,
        _execution_facts,
        _recovery_feedback,
        _workspace_files,
    )
    workspace = case/'workspace'
    regression = RegressionSpec(tuple(settings['regression_argv']), tuple(settings['protected_paths']))
    target = VerificationSpec(tuple(settings['verification_argv']), tuple(settings['protected_paths']))
    if baseline is None:
        clean = Path(settings['baseline_workspace'])
        before = manifest(clean)
        helper = RepairController(None, clean, target, output, regression=regression)
        baseline = helper._regress('clean-baseline', settings['limits']['verifier_seconds'])
        assert baseline['status'] == 'passed'
        assert len(baseline['failures']) == 1 and baseline['failures'][0].endswith('::test_readme_line_counts_are_current')
        assert manifest(clean) == before
    dump(output/'baseline.json',baseline)
    before = manifest(workspace)
    helper = RepairController(None, workspace, target, output, regression=regression)
    acceptance = helper._verify('initial', settings['limits']['verifier_seconds'])
    regression_result = helper._regress('initial', settings['limits']['verifier_seconds'], baseline)
    assert manifest(workspace) == before
    assert acceptance['status'] == 'passed'
    assert regression_result['status'] == 'failed'
    assert len(regression_result['new_failures']) == 1
    assert regression_result['new_failures'][0].endswith('::test_doc_python_line_references_are_current')
    assert not regression_result['lost_tests']
    history = json.loads(Path(settings['history']).read_text())
    recorded = SimpleNamespace(messages=history, last_run=SimpleNamespace(status='round_limit',finish_reason='tool_calls'))
    files = _workspace_files(workspace)
    facts = _execution_facts(recorded,workspace,files,files)
    feedback = _recovery_feedback((case/'task.md').read_text(),facts,regression_result)
    assert feedback['kind'] == 'regression_failed'
    result = {'acceptance':acceptance,'regression':regression_result,'facts':facts,'feedback':feedback}
    dump(output/'initial.json',result)
    return baseline, result


if __name__ == '__main__':
    suite = Path('/tmp/corecoder-resume-01')
    baseline, initial = probe(suite/'task04-staged',suite/'preflight')
    print(json.dumps({'real_model_calls':0,'baseline_known_failures':baseline['failures'],
                      'initial_target':initial['acceptance']['status'],
                      'initial_new_failures':initial['regression']['new_failures']},indent=2))
