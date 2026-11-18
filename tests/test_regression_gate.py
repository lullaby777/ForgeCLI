"""Regression completion gate: scripted responses and real local pytest processes."""
import json
import sys

import pytest

from corecoder.agent import Agent
from corecoder.llm import LLMResponse
from corecoder.repair import RegressionSpec, RepairController, RepairLimits, VerificationSpec
from tests.test_repair import ScriptedModel


@pytest.fixture
def scenario(tmp_path):
    workspace = tmp_path / 'repo'
    workspace.mkdir()
    (workspace / 'answer.txt').write_text('wrong')
    (workspace / 'legacy.txt').write_text('ok')
    tests = workspace / 'tests'
    tests.mkdir()
    (tests / 'test_old.py').write_text(
        "from pathlib import Path\ndef test_legacy():\n assert Path('legacy.txt').read_text() == 'ok', 'legacy broke'\n")
    verifier = tmp_path / 'accept.py'
    verifier.write_text("from pathlib import Path\nassert Path('answer.txt').read_text() == 'fixed'\n")
    spec = VerificationSpec((sys.executable, str(verifier)), (str(verifier),))
    regression = RegressionSpec((sys.executable, '-m', 'pytest', 'tests', '-q'))
    return workspace, verifier, spec, regression, tmp_path / 'result'


def action(workspace, **files):
    def respond():
        for name, value in files.items():
            (workspace / (name + '.txt')).write_text(value)
        return LLMResponse(content='done')
    return respond


def run(scenario, *responses, **limits):
    workspace, _, spec, regression, result_dir = scenario
    model = ScriptedModel(*responses)
    result = RepairController(Agent(model, tools=[]), workspace, spec, result_dir,
                              RepairLimits(**limits), regression=regression).run('Fix answer without breaking old behavior')
    return result, model


def test_regression_failure_is_feedback_then_both_checks_pass(scenario):
    workspace, _, _, _, output = scenario
    result, model = run(scenario, action(workspace, answer='fixed', legacy='broken'), action(workspace, legacy='ok'))
    assert result.completed and result.status == 'passed'
    assert result.completion_scope == 'acceptance_and_regression'
    assert result.acceptance_status == result.regression_status == 'passed'
    assert [a['verification']['status'] for a in result.attempts] == ['passed', 'passed']
    assert [a['regression']['status'] for a in result.attempts] == ['failed', 'passed']
    first = result.attempts[0]
    assert first['failed_phase'] == 'regression'
    assert first['recovery']['kind'] == 'regression_failed'
    assert first['feedback_submitted_to_agent']
    assert 'test_old::test_legacy' in model.prompts[1] and 'legacy broke' in model.prompts[1]
    assert 'Task acceptance passed' in model.prompts[1]
    assert result.model_calls == 2
    saved = json.loads((output / 'result.json').read_text())
    assert saved['completed'] and saved['regression_baseline']['failures'] == []
    assert (output / 'regression-baseline.xml').exists()
    assert (output / 'regression-1.xml').exists() and (output / 'regression-2.txt').exists()


def test_frozen_preexisting_failure_does_not_block_completion(scenario):
    workspace, _, _, _, _ = scenario
    (workspace / 'tests/test_known.py').write_text('def test_known():\n assert False, "existing failure"\n')
    result, _ = run(scenario, action(workspace, answer='fixed'))
    assert result.completed and result.regression_status == 'passed'
    assert result.regression_baseline['failures'] == ['tests.test_known::test_known']
    assert result.attempts[0]['regression']['known_failures_present'] == ['tests.test_known::test_known']
    assert result.attempts[0]['regression']['exit_code'] == 1


def test_new_failure_cannot_join_frozen_allowance(scenario):
    workspace, _, _, _, _ = scenario
    (workspace / 'tests/test_known.py').write_text('def test_known():\n assert False\n')
    result, _ = run(scenario, action(workspace, answer='fixed', legacy='broken'), max_attempts=1)
    assert not result.completed and result.status == 'attempt_limit'
    assert result.attempts[0]['regression']['new_failures'] == ['tests.test_old::test_legacy']
    assert result.regression_baseline['failures'] == ['tests.test_known::test_known']


def test_regression_not_run_until_target_passes(scenario):
    result, _ = run(scenario, LLMResponse(content='claimed done'), max_attempts=1)
    assert result.acceptance_status == 'failed'
    assert result.regression_status == 'not_run'
    assert result.attempts[0]['regression']['status'] == 'not_run'
    assert not (scenario[-1] / 'regression-1.xml').exists()
    assert (scenario[-1] / 'regression-baseline.xml').exists()


def test_regression_feedback_does_not_skip_rechecking_target(scenario):
    workspace, _, _, _, _ = scenario
    result, _ = run(scenario, action(workspace, answer='fixed', legacy='broken'),
                    action(workspace, answer='wrong', legacy='ok'), max_attempts=2)
    assert not result.completed
    assert result.attempts[1]['verification']['status'] == 'failed'
    assert result.attempts[1]['regression']['status'] == 'not_run'
    assert result.acceptance_status == 'failed' and result.regression_status == 'not_run'


@pytest.mark.parametrize('limits,expected,calls', [({'max_model_calls': 1}, 'model_call_limit', 1),
                                                  ({'max_attempts': 1}, 'attempt_limit', 1),
                                                  ({}, 'no_progress', 3)])
def test_existing_limits_and_stagnation_apply_to_regression(scenario, limits, expected, calls):
    workspace, _, _, _, _ = scenario
    result, model = run(scenario, action(workspace, answer='fixed', legacy='broken'),
                        LLMResponse(content='done'), LLMResponse(content='done'), **limits)
    assert result.status == expected and not result.completed
    assert result.model_calls == len(model.prompts) == calls
    assert result.acceptance_status == 'passed' and result.regression_status == 'failed'


@pytest.mark.parametrize('protected', ['tests/test_old.py', 'pytest.ini'])
def test_cannot_change_tests_or_collection_configuration(scenario, protected):
    workspace, _, _, _, _ = scenario
    def cheat():
        (workspace / 'answer.txt').write_text('fixed')
        (workspace / protected).write_text('')
        return LLMResponse(content='done')
    result, _ = run(scenario, cheat)
    assert result.status == 'protected_inputs_changed' and not result.completed
    assert result.attempts[0]['regression']['status'] == 'not_run'


def test_baseline_errors_stop_before_model_calls(scenario):
    workspace, _, spec, _, output = scenario
    regression = RegressionSpec((sys.executable, '-m', 'pytest', 'missing-tests', '-q'))
    model = ScriptedModel()
    result = RepairController(Agent(model, tools=[]), workspace, spec, output, regression=regression).run('fix')
    assert result.status == 'regression_baseline_error' and result.model_calls == 0
    assert not result.completed and not result.attempts and not model.prompts


def test_regression_environment_error_is_not_sent_back_for_repair(scenario, monkeypatch):
    workspace, _, spec, regression, output = scenario
    controller = RepairController(Agent(ScriptedModel(action(workspace, answer='fixed')), tools=[]),
                                  workspace, spec, output, regression=regression)
    original = controller._regress
    def regress(label, timeout, baseline=None):
        if label == 'baseline':
            return original(label, timeout, baseline)
        return {'phase': 'regression', 'status': 'error', 'exit_code': 2, 'feedback': 'collection unavailable'}
    monkeypatch.setattr(controller, '_regress', regress)
    result = controller.run('fix')
    assert result.status == 'regression_error' and result.model_calls == 1 and not result.completed
    assert result.attempts[0]['feedback_submitted_to_agent'] is False


@pytest.mark.parametrize("omit", [False, True])
def test_missing_or_skipped_baseline_tests_do_not_count_as_success(scenario, monkeypatch, omit):
    workspace, _, spec, regression, output = scenario
    controller = RepairController(Agent(ScriptedModel(action(workspace, answer='fixed')), tools=[]),
                                  workspace, spec, output, RepairLimits(max_attempts=1), regression=regression)
    original = controller._run_check
    def check(argv, log, timeout, failure_codes):
        if log.name == 'regression-1.txt':
            report = output / 'regression-1.xml'
            report.write_text('<testsuite><testcase classname="test_new" name="test_ok"/>'
                              + ('' if omit else '<testcase classname="tests.test_old" name="test_legacy"><skipped/></testcase>')
                              + '</testsuite>')
            return {'status': 'passed', 'exit_code': 0, 'feedback': ''}
        return original(argv, log, timeout, failure_codes)
    monkeypatch.setattr(controller, '_run_check', check)
    result = controller.run('fix')
    assert not result.completed
    assert result.attempts[0]['regression']['lost_tests'] == ['tests.test_old::test_legacy']


def test_no_regression_configuration_is_explicitly_acceptance_only(scenario):
    workspace, _, spec, _, output = scenario
    model = ScriptedModel(action(workspace, answer='fixed'))
    result = RepairController(Agent(model, tools=[]), workspace, spec, output).run('fix')
    assert result.status == 'passed' and result.acceptance_status == 'passed'
    assert result.completion_scope == 'acceptance_only' and not result.completed
    assert result.regression_status == 'not_configured'


def test_cli_reads_regression_config_and_reports_both_checks(scenario, capsys):
    from corecoder.cli import _repair_once
    workspace, verifier, _, regression, output = scenario
    config = output.parent / 'config.json'
    config.write_text(json.dumps({'workspace': str(workspace), 'result_dir': str(output),
                                  'verification': {'argv': [sys.executable, str(verifier)]},
                                  'regression': {'argv': list(regression.argv)}}))
    _repair_once(Agent(ScriptedModel(action(workspace, answer='fixed')), tools=[]), 'fix', config)
    text = capsys.readouterr().out
    assert 'Acceptance: passed' in text and 'regression: passed' in text
    assert 'acceptance_and_regression' in text


@pytest.mark.parametrize('xml', ['', '<testsuite/>', '<invalid',
                               '<testsuite><testcase classname="x" name="t"><failure/></testcase></testsuite>',
                               '<testsuite><testcase classname="x" name="t"/><testcase classname="x" name="t"/></testsuite>'])
def test_invalid_regression_report_never_passes(scenario, monkeypatch, xml):
    workspace, _, spec, regression, output = scenario
    controller = RepairController(Agent(ScriptedModel(action(workspace, answer='fixed')), tools=[]),
                                  workspace, spec, output, regression=regression)
    original = controller._run_check
    def check(argv, log, timeout, failure_codes):
        if log.name == 'regression-1.txt':
            (output / 'regression-1.xml').write_text(xml)
            return {'status': 'passed', 'exit_code': 0, 'feedback': ''}
        return original(argv, log, timeout, failure_codes)
    monkeypatch.setattr(controller, '_run_check', check)
    result = controller.run('fix')
    assert result.status == 'regression_error' and not result.completed and result.model_calls == 1


def test_real_regression_timeout_stops_without_additional_model_calls(scenario):
    workspace, _, _, _, _ = scenario
    (workspace / 'tests/test_old.py').write_text(
        "from pathlib import Path\nimport time\ndef test_legacy():\n"
        " if Path('legacy.txt').read_text() == 'broken': time.sleep(5)\n")
    result, _ = run(scenario, action(workspace, answer='fixed', legacy='broken'), verifier_seconds=1)
    assert result.status == 'regression_timeout' and not result.completed
    assert result.model_calls == 1
    assert result.attempts[0]['regression']['status'] == 'timeout'


def test_regression_check_cannot_mutate_workspace_and_claim_success(scenario, monkeypatch):
    workspace, _, spec, regression, output = scenario
    controller = RepairController(Agent(ScriptedModel(action(workspace, answer='fixed')), tools=[]),
                                  workspace, spec, output, regression=regression)
    original = controller._regress
    def regress(label, timeout, baseline=None):
        value = original(label, timeout, baseline)
        if label != 'baseline':
            (workspace / 'legacy.txt').write_text('broken')
        return value
    monkeypatch.setattr(controller, '_regress', regress)
    result = controller.run('fix')
    assert result.status == 'workspace_changed_during_verification' and not result.completed


def test_offline_demo_exercises_real_tools_and_both_verifiers(tmp_path):
    from examples.regression_repair_demo import run_demo
    result = run_demo(tmp_path / 'demo')
    assert result.completed and result.model_calls == 2
    assert [a['verification']['status'] for a in result.attempts] == ['passed', 'passed']
    assert [a['regression']['status'] for a in result.attempts] == ['failed', 'passed']
    assert result.attempts[0]['feedback_submitted_to_agent'] is True
