"""One-shot bounded continuation; preserve old evidence and refuse overwrites."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from common import command, digest, dump, local_env, manifest

SUITE=Path('/tmp/corecoder-resume-01')


def validate(case):
    settings=json.loads((case/'case.json').read_text())
    seal=json.loads((case/'seal.json').read_text())
    assert manifest(case/'workspace')==seal['workspace'], 'workspace changed'
    assert manifest(case/'runtime')==seal['runtime'], 'runtime changed'
    for name, path in [('evaluation',settings['evaluation']),('baseline_workspace',settings['baseline_workspace'])]:
        assert manifest(Path(path))==seal[name], name+' changed'
    assert manifest(Path(__file__).parent)==seal['harness'], 'runner changed'
    assert digest(settings)==seal['settings'], 'settings changed'
    for name, path in [('task',case/'task.md'),('history',Path(settings['history'])),('baseline',Path(settings['baseline_file']))]:
        assert digest(path.read_text())==seal[name], name+' changed'
    return settings,seal


def run_case(case, live, scenario='stay'):
    if live and not os.environ.get('CORECODER_API_KEY'):
        raise SystemExit('Set CORECODER_API_KEY first; the case remains unused')
    settings,seal=validate(case)
    output=case/'run'
    output.mkdir(exist_ok=False)
    dump(output/'settings.json',settings)
    argv=[sys.executable,str(Path(__file__).with_name('worker.py')),str(case)]
    argv+=['--live'] if live else ['--offline',scenario]
    env=local_env(case/'workspace',case/'agent-home')
    if live:env['CORECODER_API_KEY']=os.environ['CORECODER_API_KEY']
    process=command(argv,case/'workspace',output/'terminal.txt',env,settings['limits']['max_seconds'])
    worker=json.loads((output/'worker.json').read_text()) if (output/'worker.json').exists() else {'completed':False,'execution_status':'worker_did_not_finalize'}
    # Final checks are separate from the repair process; they never call a model.
    sys.path.insert(0,str(case/'runtime'))
    from corecoder.repair import RegressionSpec, RepairController, VerificationSpec
    final=output/'final';final.mkdir()
    checker=RepairController(None,case/'workspace',VerificationSpec(tuple(settings['verification_argv'])),final,
                             regression=RegressionSpec(tuple(settings['regression_argv'])))
    workspace_before_final=manifest(case/'workspace')
    acceptance=checker._verify('final',60)
    baseline=json.loads(Path(settings['baseline_file']).read_text())
    regression=checker._regress('final',120,baseline)
    current=manifest(case/'workspace')
    changed=[p for p,h in seal['protected_workspace'].items() if current.get(p)!=h]
    changed += [p for p in settings['absent_protected'] if (case/'workspace'/p).exists()]
    integrity=(not changed and current==workspace_before_final and manifest(case/'runtime')==seal['runtime']
               and manifest(Path(settings['evaluation']))==seal['evaluation']
               and manifest(Path(settings['baseline_workspace']))==seal['baseline_workspace']
               and manifest(Path(__file__).parent)==seal['harness']
               and digest(json.loads((case/'case.json').read_text()))==seal['settings']
               and digest((case/'task.md').read_text())==seal['task']
               and digest(Path(settings['history']).read_text())==seal['history']
               and digest(Path(settings['baseline_file']).read_text())==seal['baseline'])
    passed=(bool(worker.get('completed')) and process['exit_code']==0 and not process['timeout']
            and acceptance['status']=='passed' and regression['status']=='passed' and integrity)
    result={'functional_passed':passed,'live':live,'execution_process':process,'execution':worker,
            'acceptance':acceptance,'regression':regression,'integrity_ok':integrity,'protected_tests_changed':changed}
    dump(output/'summary.json',result)
    (output/'change.patch').write_bytes(subprocess.check_output(['git','diff','--binary',settings['start_commit']],cwd=case/'workspace'))
    (output/'git-status.txt').write_bytes(subprocess.check_output(['git','status','--short'],cwd=case/'workspace'))
    print(json.dumps({'functional_passed':passed,'execution':worker['execution_status'],
                      'new_model_calls':worker.get('model_calls'),'total_calls_with_v4':worker.get('total_calls_with_v4'),
                      'summary':str(output/'summary.json')},indent=2))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',required=True,action='store_true')
    parser.parse_args()
    if Path(sys.prefix).resolve()!=(SUITE/'venv').resolve():
        raise SystemExit('Use /tmp/corecoder-resume-01/venv/bin/python')
    run_case(SUITE/'task04-staged',True)


if __name__=='__main__':main()
