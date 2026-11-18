"""Repair orchestration with deterministic models and actual local verification."""
import json
import sys
import time
from pathlib import Path

import pytest

from corecoder.agent import Agent
from corecoder.llm import LLMResponse, ToolCall
from corecoder.repair import RepairController, RepairLimits, VerificationSpec
from corecoder.tools.agent import AgentTool
from corecoder.tools.bash import BashTool, get_tracked_cwd
from corecoder.tools.write import WriteFileTool


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.prompts = []

    def chat(self, messages, **kwargs):
        self.prompts.append(messages[-1]["content"])
        response = next(self.responses)
        if callable(response):
            response = response()
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture
def setup(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    verifier = tmp_path / "verify.py"
    verifier.write_text("from pathlib import Path\nassert Path('answer.txt').read_text() == 'fixed', 'expected fixed'\n")
    (workspace / "answer.txt").write_text("wrong")
    spec = VerificationSpec((sys.executable, str(verifier)), protected_paths=(str(verifier),))
    return workspace, verifier, spec, tmp_path / "results"


def test_failed_verification_feedback_then_success_and_record(setup):
    workspace, verifier, spec, output = setup

    def fix():
        (workspace / "answer.txt").write_text("fixed")
        return LLMResponse(content="ready")

    model = ScriptedModel(LLMResponse(content="done"), fix)
    agent = Agent(model, tools=[])
    original_cwd = get_tracked_cwd()
    result = RepairController(agent, workspace, spec, output).run("fix it")
    assert result.status == "passed"
    assert [a["verification"]["status"] for a in result.attempts] == ["failed", "passed"]
    assert "expected fixed" in model.prompts[1]
    assert "assert Path" not in model.prompts[0]  # no verifier source in initial prompt
    assert result.model_calls == 2
    assert agent.llm is model and agent.max_rounds == 50
    assert agent.execution_check is None and get_tracked_cwd() == original_cwd
    saved = json.loads((output / "result.json").read_text())
    assert saved["status"] == "passed"
    assert saved["attempts"][0]["after"] != saved["attempts"][1]["after"]
    assert verifier.read_text().startswith("from pathlib")


def test_model_saying_done_cannot_pass_bad_patch(setup):
    workspace, _, spec, output = setup
    agent = Agent(ScriptedModel(LLMResponse(content="all tests passed")), tools=[])
    result = RepairController(agent, workspace, spec, output, RepairLimits(max_attempts=1)).run("fix")
    assert result.status == "attempt_limit"
    assert result.attempts[0]["verification"]["status"] == "failed"


def test_round_limit_still_verifies_valid_patch(setup):
    workspace, _, spec, output = setup
    target = workspace / "answer.txt"
    model = ScriptedModel(LLMResponse(tool_calls=[ToolCall("write", "write_file", {"file_path": str(target), "content": "fixed"})]))
    agent = Agent(model, tools=[WriteFileTool()])
    result = RepairController(agent, workspace, spec, output, RepairLimits(rounds_per_attempt=1)).run("fix")
    assert result.status == "passed"
    assert result.attempts[0]["agent_status"] == "round_limit"


def test_empty_response_gets_failed_verification_and_bounded_feedback(setup):
    workspace, _, spec, output = setup
    model = ScriptedModel(LLMResponse(), LLMResponse(content="still wrong"))
    agent = Agent(model, tools=[], max_empty_retries=0)
    result = RepairController(agent, workspace, spec, output, RepairLimits(max_attempts=2)).run("fix")
    assert result.attempts[0]["agent_status"] == "empty_response"
    assert len(result.attempts) == 2
    assert result.status == "no_progress"


def test_attempt_limit_and_no_progress_are_distinct(setup):
    workspace, _, spec, output = setup
    model = ScriptedModel(*[LLMResponse(content="done") for _ in range(4)])
    result = RepairController(Agent(model, tools=[]), workspace, spec, output,
                              RepairLimits(max_attempts=3, max_stagnant_attempts=2)).run("fix")
    assert result.status == "no_progress"
    assert len(result.attempts) == 2


def test_total_model_budget_counts_empty_retries(setup):
    workspace, _, spec, output = setup
    model = ScriptedModel(LLMResponse(), LLMResponse())
    result = RepairController(Agent(model, tools=[]), workspace, spec, output,
                              RepairLimits(max_model_calls=1)).run("fix")
    assert result.status == "model_call_limit"
    assert len(model.prompts) == result.model_calls == 1
    assert result.attempts[0]["verification"]["status"] == "failed"


def test_environment_exit_code_does_not_trigger_model_retry(setup):
    workspace, verifier, _, output = setup
    verifier.write_text("raise SystemExit(5)\n")
    spec = VerificationSpec((sys.executable, str(verifier)))
    model = ScriptedModel(LLMResponse(content="ready"))
    result = RepairController(Agent(model, tools=[]), workspace, spec, output).run("fix")
    assert result.status == "verification_error"
    assert len(model.prompts) == 1


def test_missing_verifier_command_is_recorded(setup):
    workspace, _, _, output = setup
    result = RepairController(Agent(ScriptedModel(LLMResponse(content="ready")), tools=[]), workspace,
                              VerificationSpec((str(workspace / "missing-program"),)), output).run("fix")
    assert result.status == "verification_error"
    assert result.attempts[0]["verification"]["exit_code"] is None


def test_verifier_timeout_stops_without_more_model_calls(setup):
    workspace, verifier, _, output = setup
    verifier.write_text("import time\ntime.sleep(10)\n")
    model = ScriptedModel(LLMResponse(content="ready"))
    result = RepairController(Agent(model, tools=[]), workspace,
                              VerificationSpec((sys.executable, str(verifier))), output,
                              RepairLimits(verifier_seconds=0.1)).run("fix")
    assert result.status == "verification_timeout"
    assert len(model.prompts) == 1


def test_changed_protected_test_fails_before_verifier_runs(setup):
    workspace, verifier, spec, output = setup

    def cheat():
        verifier.write_text("pass\n")
        return LLMResponse(content="ready")

    result = RepairController(Agent(ScriptedModel(cheat), tools=[]), workspace, spec, output).run("fix")
    assert result.status == "protected_inputs_changed"
    assert "verification" not in result.attempts[0]


def test_verifier_cannot_mutate_workspace_and_claim_pass(setup):
    workspace, verifier, spec, output = setup
    verifier.write_text("from pathlib import Path\nPath('answer.txt').write_text('fixed')\n")
    result = RepairController(Agent(ScriptedModel(LLMResponse(content="ready")), tools=[]), workspace, spec, output).run("fix")
    assert result.status == "workspace_changed_during_verification"


def test_deadline_is_checked_after_running_model_returns(setup):
    workspace, _, spec, output = setup

    def slow():
        time.sleep(0.05)
        return LLMResponse(content="ready")

    result = RepairController(Agent(ScriptedModel(slow), tools=[]), workspace, spec, output,
                              RepairLimits(max_seconds=0.01)).run("fix")
    assert result.status == "time_limit"
    assert "verification" not in result.attempts[0]


def test_agent_error_is_recorded_and_existing_good_patch_can_pass(setup):
    workspace, _, spec, output = setup
    (workspace / "answer.txt").write_text("fixed")
    result = RepairController(Agent(ScriptedModel(RuntimeError("failure")), tools=[]), workspace, spec, output).run("fix")
    assert result.status == "passed"
    assert result.attempts[0]["agent_status"] == "error"
    assert result.attempts[0]["error_type"] == "RuntimeError"


def test_interruption_records_result_and_restores_agent(setup):
    workspace, _, spec, output = setup
    model = ScriptedModel(KeyboardInterrupt())
    agent = Agent(model, tools=[])
    result = RepairController(agent, workspace, spec, output).run("fix")
    assert result.status == "interrupted"
    assert json.loads((output / "result.json").read_text())["status"] == "interrupted"
    assert agent.llm is model


def test_result_directory_cannot_overwrite_workspace_or_previous_run(setup):
    workspace, _, spec, output = setup
    agent = Agent(ScriptedModel(LLMResponse(content="done")), tools=[])
    with pytest.raises(ValueError):
        RepairController(agent, workspace, spec, workspace / "results")
    output.mkdir()
    with pytest.raises(FileExistsError):
        RepairController(agent, workspace, spec, output).run("fix")


def test_budget_counts_nested_agent_model_calls(setup):
    workspace, _, spec, output = setup
    model = ScriptedModel(LLMResponse(tool_calls=[ToolCall("delegate", "agent", {"task": "inspect"})]))
    agent = Agent(model, tools=[AgentTool()])
    result = RepairController(agent, workspace, spec, output, RepairLimits(max_model_calls=1)).run("fix")
    assert result.status == "model_call_limit"
    assert result.model_calls == len(model.prompts) == 1


def test_expired_deadline_prevents_tool_and_preserves_message_pairs(setup):
    workspace, _, spec, output = setup
    target = workspace / "answer.txt"

    def slow_tool_response():
        time.sleep(0.05)
        return LLMResponse(tool_calls=[ToolCall("late", "write_file", {"file_path": str(target), "content": "fixed"})])

    agent = Agent(ScriptedModel(slow_tool_response), tools=[WriteFileTool()])
    result = RepairController(agent, workspace, spec, output, RepairLimits(max_seconds=0.01)).run("fix")
    assert result.status == "time_limit"
    assert target.read_text() == "wrong"
    assert agent.messages[-1]["role"] == "tool"
    assert agent.messages[-1]["tool_call_id"] == "late"


def test_repair_cli_uses_external_configuration_without_real_model(setup, capsys):
    from corecoder.cli import _repair_once

    workspace, verifier, _, output = setup
    (workspace / "answer.txt").write_text("fixed")
    config = output.parent / "repair.json"
    config.write_text(json.dumps({"workspace": str(workspace), "result_dir": str(output),
                                 "verification": {"argv": [sys.executable, str(verifier)],
                                                  "protected_paths": [str(verifier)]}}))
    _repair_once(Agent(ScriptedModel(LLMResponse(content="ready")), tools=[]), "fix", config)
    assert "Verification: passed" in capsys.readouterr().out


def test_demo_really_runs_local_verification_twice(tmp_path):
    from examples.repair_demo import run_demo

    result = run_demo(tmp_path / "demo")
    assert result.status == "passed"
    assert result.model_calls == 4
    assert [a["verification"]["status"] for a in result.attempts] == ["failed", "passed"]


def test_agent_shell_starts_in_workspace_without_global_chdir(setup):
    import os

    workspace, _, spec, output = setup
    cwd = os.getcwd()
    model = ScriptedModel(LLMResponse(tool_calls=[ToolCall("where", "bash", {"command": "pwd"})]),
                          LLMResponse(content="ready"))
    agent = Agent(model, tools=[BashTool()])
    RepairController(agent, workspace, spec, output, RepairLimits(max_attempts=1)).run("fix")
    result = next(m["content"] for m in agent.messages if m.get("role") == "tool")
    assert Path(result.strip()).resolve() == workspace.resolve()
    assert os.getcwd() == cwd


def test_feedback_is_bounded_but_full_output_is_retained(setup):
    workspace, verifier, _, output = setup
    verifier.write_text("print('x' * 10000)\nraise SystemExit(1)\n")
    result = RepairController(Agent(ScriptedModel(LLMResponse(content="ready")), tools=[]), workspace,
                              VerificationSpec((sys.executable, str(verifier))), output,
                              RepairLimits(max_attempts=1)).run("fix")
    verification = result.attempts[0]["verification"]
    assert len(verification["feedback"]) < 4050
    assert len(Path(verification["output"]).read_text()) > 10000


def test_external_python_verifier_imports_workspace_and_has_no_model_keys(setup, monkeypatch):
    workspace, verifier, spec, output = setup
    (workspace / "target_marker.py").write_text("VALUE = 'workspace'\n")
    verifier.write_text("import os, target_marker\n"
                        "assert target_marker.VALUE == 'workspace'\n"
                        "assert os.environ['CORECODER_TARGET'] == os.getcwd()\n"
                        "assert 'OPENAI_API_KEY' not in os.environ\n"
                        "assert 'CORECODER_LIVE_SMOKE' not in os.environ\n")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-verifier")
    monkeypatch.setenv("CORECODER_LIVE_SMOKE", "1")
    result = RepairController(Agent(ScriptedModel(LLMResponse(content="ready")), tools=[]), workspace, spec, output).run("fix")
    assert result.status == "passed"


@pytest.mark.parametrize("kwargs", [{"max_attempts": 0}, {"max_model_calls": -1}, {"max_seconds": float('inf')},
                                   {"verifier_seconds": 0}, {"rounds_per_attempt": True}])
def test_invalid_limits_rejected(kwargs):
    with pytest.raises(ValueError):
        RepairLimits(**kwargs)


# --- soft recovery for protected-input violations (max_protected_violations > 0) ---

def test_protected_edit_is_restored_and_agent_can_still_fix(setup):
    workspace, verifier, spec, output = setup
    original = verifier.read_text()

    def cheat():
        verifier.write_text("pass\n")  # tamper with the verifier ...
        (workspace / "answer.txt").write_text("still wrong")  # ... and leave a bad source edit
        return LLMResponse(content="ready")

    def fix():
        (workspace / "answer.txt").write_text("fixed")
        return LLMResponse(content="ready")

    model = ScriptedModel(cheat, fix)
    result = RepairController(Agent(model, tools=[]), workspace, spec, output,
                              RepairLimits(max_protected_violations=1)).run("fix")
    assert result.status == "passed"
    assert verifier.read_text() == original  # verifier restored before it ever ran
    first = result.attempts[0]
    assert first["protected_violation"]["number"] == 1
    assert first["protected_violation"]["restored"] == [str(verifier)]
    assert first["verification"]["status"] == "failed"  # the tampered verifier was never used
    assert "restored to their original content" in model.prompts[1]
    assert (workspace / "answer.txt").read_text() == "fixed"  # unprotected source edits are kept


def test_repeated_protected_edit_ends_run(setup):
    workspace, verifier, spec, output = setup

    def cheat():
        verifier.write_text("pass\n")
        return LLMResponse(content="ready")

    result = RepairController(Agent(ScriptedModel(cheat, cheat), tools=[]), workspace, spec, output,
                              RepairLimits(max_protected_violations=1)).run("fix")
    assert result.status == "protected_inputs_changed"
    assert len(result.attempts) == 2
    assert "verification" not in result.attempts[1]


def test_restore_removes_files_and_dirs_added_under_protected_dir(tmp_path):
    workspace = tmp_path / "repo"
    tests_dir = workspace / "tests"
    (tests_dir / "keep").mkdir(parents=True)
    (tests_dir / "test_a.py").write_text("def test_a():\n    assert True\n")
    (workspace / "answer.txt").write_text("wrong")
    verifier = tmp_path / "verify.py"
    verifier.write_text("from pathlib import Path\nassert Path('answer.txt').read_text() == 'fixed'\n")
    spec = VerificationSpec((sys.executable, str(verifier)), protected_paths=(str(verifier), str(tests_dir)))

    def cheat():
        (tests_dir / "test_a.py").write_text("")
        (tests_dir / "sneaky").mkdir()
        (tests_dir / "sneaky" / "test_b.py").write_text("x = 1\n")
        (tests_dir / "keep").rmdir()
        (workspace / "answer.txt").write_text("fixed")
        return LLMResponse(content="ready")

    result = RepairController(Agent(ScriptedModel(cheat), tools=[]), workspace, spec, tmp_path / "out",
                              RepairLimits(max_protected_violations=1)).run("fix")
    assert result.status == "passed"
    assert (tests_dir / "test_a.py").read_text().startswith("def test_a")
    assert not (tests_dir / "sneaky").exists() and (tests_dir / "keep").is_dir()


def test_negative_violation_budget_rejected():
    with pytest.raises(ValueError):
        RepairLimits(max_protected_violations=-1)
