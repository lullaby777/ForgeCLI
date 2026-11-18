"""Verify the unfixed workspace and old regression failures without any model."""
import json
import sys
from pathlib import Path

from common import dump, manifest

ROOT=Path(__file__).resolve().parents[1]


def main():
    settings=json.loads((ROOT/'task.json').read_text())
    before=manifest(ROOT/'workspace')
    sys.path.insert(0,str(ROOT/'runtime'))
    from corecoder.repair import RegressionSpec, RepairController, VerificationSpec
    output=ROOT/'preflight';output.mkdir(exist_ok=False)
    checker=RepairController(None,ROOT/'workspace',VerificationSpec(**settings['verification']),output,
                             regression=RegressionSpec(**settings['regression']))
    target=checker._verify('before',60)
    baseline=checker._regress('baseline',120)
    assert target['status']=='failed'
    text=Path(target['output']).read_text()
    assert 'Ran 14 tests' in text and 'FAILED (failures=9)' in text
    assert baseline['status']=='passed' and baseline['failures']==['tests.test_core::test_readme_line_counts_are_current']
    assert len(baseline['tests'])==353
    assert manifest(ROOT/'workspace')==before
    assert not (ROOT/'task').exists()
    dump(output/'baseline.json',baseline)
    dump(output/'result.json',{'real_model_calls':0,'target_tests':14,'target_failures':9,
                             'regression_passed':352,'regression_known_failures':1,'formal_task_not_created':True})
    print(json.dumps(json.loads((output/'result.json').read_text()),indent=2))


if __name__=='__main__':main()
