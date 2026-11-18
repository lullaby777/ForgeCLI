"""Public local checker API, legacy adapters and durable-task integration; no API calls."""
import copy
import sys

import pytest

from corecoder.agent import Agent
from corecoder.repair import RegressionSpec, RepairController, VerificationSpec
from corecoder.tasks import RepairTask
from corecoder.verification import VerificationRunner
from tests.test_repair import ScriptedModel


@pytest.fixture
def local_checks(tmp_path):
    workspace = tmp_path / 'repo'
    workspace.mkdir()
    (workspace / 'answer.txt').write_text('wrong')
    (workspace / 'legacy.txt').write_text('ok')
    tests = workspace / 'tests'
    tests.mkdir()
    (tests / 'test_features.py').write_text(
        "from pathlib import Path\ndef test_legacy():\n assert Path('legacy.txt').read_text() == 'ok'\n"
        "def test_known():\n assert False, 'pre-existing failure'\n")
    check = tmp_path / 'accept.py'
    check.write_text("from pathlib import Path\nassert Path('answer.txt').read_text() == 'fixed'\n")
    verification = VerificationSpec((sys.executable, str(check)), (str(check),))
    regression = RegressionSpec((sys.executable, '-m', 'pytest', 'tests', '-q'))
    output = tmp_path / 'reports'
    output.mkdir()
    return workspace, verification, regression, output


def test_public_acceptance_checks_workspace_and_keeps_existing_log_names(local_checks):
    workspace, spec, regression, output = local_checks
    runner = VerificationRunner(workspace, spec, output, regression)
    failed = runner.verify('before', 5)
    assert failed['status'] == 'failed' and failed['exit_code'] == 1
    assert failed['output'] == str(output / 'verification-before.txt')
    assert 'AssertionError' in failed['feedback']
    (workspace / 'answer.txt').write_text('fixed')
    passed = runner.verify('after', 5)
    assert passed['status'] == 'passed' and passed['exit_code'] == 0
    assert passed['output'] == str(output / 'verification-after.txt')


@pytest.mark.parametrize('program,timeout,expected', [('raise SystemExit(1)', 5, 'failed'),
                                                     ('raise SystemExit(5)', 5, 'error'),
                                                     ('import time; time.sleep(5)', .1, 'timeout')])
def test_public_checker_and_legacy_adapter_return_identical_outcomes(local_checks, program, timeout, expected):
    workspace, _, _, output = local_checks
    spec = VerificationSpec((sys.executable, '-c', program))
    direct = VerificationRunner(workspace, spec, output).verify('same', timeout)
    legacy = RepairController(None, workspace, spec, output)._verify('same', timeout)
    assert direct == legacy
    assert direct['status'] == expected


def test_missing_command_is_same_error_in_both_entry_points(local_checks):
    workspace, _, _, output = local_checks
    spec = VerificationSpec((str(output / 'missing'),))
    direct = VerificationRunner(workspace, spec, output).verify(1, 5)
    legacy = RepairController(None, workspace, spec, output)._verify(1, 5)
    assert direct == legacy and direct['status'] == 'error' and direct['exit_code'] is None


def test_public_regression_distinguishes_known_and_new_failures(local_checks):
    workspace, spec, regression, output = local_checks
    runner = VerificationRunner(workspace, spec, output, regression)
    baseline = runner.regress('baseline', 5)
    frozen = copy.deepcopy(baseline)
    assert baseline['status'] == 'passed' and baseline['exit_code'] == 1
    assert len(baseline['failures']) == 1 and baseline['failures'][0].endswith('::test_known')
    (workspace / 'legacy.txt').write_text('broken')
    failed = runner.regress(1, 5, baseline)
    assert failed['status'] == 'failed'
    assert len(failed['new_failures']) == 1 and failed['new_failures'][0].endswith('::test_legacy')
    assert failed['report'] == str(output / 'regression-1.xml')
    assert 'Task acceptance passed' in failed['feedback']
    (workspace / 'legacy.txt').write_text('ok')
    passed = runner.regress(2, 5, baseline)
    assert passed['status'] == 'passed' and not passed['new_failures']
    assert passed['known_failures_present'] == baseline['failures']
    assert baseline == frozen  # checker never changes the supplied baseline


@pytest.mark.parametrize('replacement', ['def test_known():\n assert False\n',
    "import pytest\ndef test_known():\n assert False\n@pytest.mark.skip(reason='hidden')\ndef test_legacy():\n pass\n"])
def test_public_regression_rejects_lost_or_skipped_old_tests(local_checks, replacement):
    workspace, spec, regression, output = local_checks
    runner = VerificationRunner(workspace, spec, output, regression)
    baseline = runner.regress('baseline', 5)
    old_key = next(key for key in baseline['tests'] if key.endswith('::test_legacy'))
    (workspace / 'tests/test_features.py').write_text(replacement)
    current = runner.regress('changed', 5, baseline)
    assert current['status'] == 'failed' and current['lost_tests'] == [old_key]


def test_legacy_command_override_still_observes_acceptance_and_regression(local_checks, monkeypatch):
    workspace, spec, regression, output = local_checks
    controller = RepairController(None, workspace, spec, output, regression=regression)
    original = controller._run_check
    calls = []
    def observe(*args):
        calls.append(args[1].name)
        return original(*args)
    monkeypatch.setattr(controller, '_run_check', observe)
    controller._verify('legacy', 5)
    controller._regress('baseline', 5)
    assert calls == ['verification-legacy.txt', 'regression-baseline.txt']


def test_durable_task_uses_public_runner_without_constructing_another_controller(local_checks, monkeypatch):
    from corecoder.llm import LLMResponse
    workspace, spec, regression, output = local_checks
    calls = []
    original_verify, original_regress = VerificationRunner.verify, VerificationRunner.regress
    def verify(self, *args, **kwargs):
        calls.append('acceptance')
        return original_verify(self, *args, **kwargs)
    def regress(self, *args, **kwargs):
        calls.append('regression')
        return original_regress(self, *args, **kwargs)
    def forbidden_controller(*args, **kwargs):
        raise AssertionError('Durable task must not construct RepairController')
    monkeypatch.setattr(VerificationRunner, 'verify', verify)
    monkeypatch.setattr(VerificationRunner, 'regress', regress)
    monkeypatch.setattr(RepairController, '__init__', forbidden_controller)
    def fix():
        (workspace / 'answer.txt').write_text('fixed')
        return LLMResponse(content='ready')
    agent = Agent(ScriptedModel(fix), tools=[])
    result = RepairTask.create(agent, output.parent / 'task', 'Fix answer', workspace, spec, regression).run()
    assert result['completed'] and result['ledger']['model_calls'] == 1
    assert calls == ['regression', 'acceptance', 'regression']
