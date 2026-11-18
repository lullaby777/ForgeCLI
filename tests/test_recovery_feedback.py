"""Stage-aware recovery tested with scripted models and real local verifiers."""
import json
import sys

import pytest

from corecoder import checkpoints
from corecoder.agent import Agent
from corecoder.llm import LLMResponse, ToolCall
from corecoder.repair import RepairController, RepairLimits, VerificationSpec, _execution_facts, _recovery_feedback
from corecoder.tools.read import ReadFileTool
from corecoder.tools.write import WriteFileTool
from tests.test_repair import ScriptedModel


@pytest.fixture
def scenario(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "answer.txt"
    target.write_text("wrong")
    check = tmp_path / "verify.py"
    check.write_text("from pathlib import Path\nassert Path('answer.txt').read_text() == 'fixed', 'expected fixed'\n")
    return workspace, target, VerificationSpec((sys.executable, str(check)), (str(check),)), tmp_path / "results"


def test_truncation_without_changes_gets_targeted_recovery_and_still_stops(scenario):
    workspace, _, spec, output = scenario
    model = ScriptedModel(LLMResponse(finish_reason="length"), LLMResponse(finish_reason="length"))
    result = RepairController(Agent(model, tools=[]), workspace, spec, output).run("Repair the answer")
    assert result.status == "no_progress"
    assert len(model.prompts) == 2
    feedback = model.prompts[1]
    assert "truncated_without_changes" in feedback
    assert "output limit" in feedback and "no file changes landed" in feedback
    assert "one minimal tool action" in feedback
    assert "Traceback" not in feedback and "AssertionError" not in feedback
    assert result.attempts[0]["feedback_submitted_to_agent"] is True
    assert result.attempts[1]["feedback_submitted_to_agent"] is False
    assert result.attempts[0]["facts"]["files_changed"] is False
    saved = json.loads((output / "result.json").read_text())
    assert saved["attempts"][0]["recovery"]["kind"] == "truncated_without_changes"


def test_modified_but_failed_patch_gets_short_evidence_then_passes(scenario):
    workspace, target, spec, output = scenario

    def wrong_edit():
        target.write_text("still-wrong")
        return LLMResponse(content="ready")

    def correct():
        target.write_text("fixed")
        return LLMResponse(content="ready")

    model = ScriptedModel(wrong_edit, correct)
    result = RepairController(Agent(model, tools=[]), workspace, spec, output).run("Repair the answer")
    assert result.status == "passed"
    assert "modified_verification_failed" in model.prompts[1]
    assert "expected fixed" in model.prompts[1]
    assert "one targeted correction" in model.prompts[1]
    assert "Traceback" not in model.prompts[1]
    facts = result.attempts[0]["facts"]
    assert facts["changed_files"] == ["answer.txt"]
    assert facts["changed_file_count"] == 1
    assert facts["verification_input_fingerprint"] == result.attempts[0]["after"]


def test_successful_read_evidence_survives_into_feedback_but_failed_read_does_not(scenario):
    workspace, target, spec, output = scenario
    model = ScriptedModel(
        LLMResponse(tool_calls=[ToolCall("read-ok", "read_file", {"file_path": str(target)})]),
        LLMResponse(tool_calls=[ToolCall("read-bad", "read_file", {"file_path": str(workspace / "missing.txt")})]),
        LLMResponse(finish_reason="length"),
        LLMResponse(content="still investigating"),
    )
    agent = Agent(model, tools=[ReadFileTool()])
    result = RepairController(agent, workspace, spec, output).run("Repair the answer")
    assert result.status == "no_progress"
    facts = result.attempts[0]["facts"]
    assert facts["successful_read_requests"] == ["answer.txt"]
    assert "answer.txt" in model.prompts[-1]
    # The next-turn structured facts contain observations, not model claims.
    assert "missing.txt" not in result.attempts[0]["recovery"]["message"]


def test_assistant_claims_are_not_read_or_edit_evidence(scenario):
    workspace, _, spec, output = scenario
    model = ScriptedModel(LLMResponse(content="I read secret.py and fixed everything"), LLMResponse(content="done"))
    result = RepairController(Agent(model, tools=[]), workspace, spec, output).run("Repair the answer")
    facts = result.attempts[0]["facts"]
    assert facts["successful_read_requests"] == []
    assert not facts["files_changed"]
    assert result.attempts[0]["recovery"]["kind"] == "no_changes"


def test_recovery_never_replays_completed_write(scenario):
    workspace, target, spec, output = scenario

    class CountingWrite(WriteFileTool):
        def __init__(self):
            self.count = 0

        def execute(self, file_path, content):
            self.count += 1
            return super().execute(file_path, content)

    tool = CountingWrite()
    model = ScriptedModel(
        LLMResponse(tool_calls=[ToolCall("write-once", "write_file", {"file_path": str(target), "content": "wrong"})]),
        LLMResponse(finish_reason="length"), LLMResponse(content="no patch"),
    )
    agent = Agent(model, tools=[tool])
    result = RepairController(agent, workspace, spec, output).run("Repair the answer")
    assert tool.count == checkpoints.pending() == 1
    assert result.status == "no_progress"
    assert sum(m.get("role") == "tool" and m.get("tool_call_id") == "write-once" for m in agent.messages) == 1


def test_changed_files_and_excerpt_are_bounded(scenario):
    workspace, _, spec, output = scenario

    def many_changes():
        for index in range(20):
            (workspace / f"file-{index}.txt").write_text("changed")
        return LLMResponse(content="ready")

    result = RepairController(Agent(ScriptedModel(many_changes), tools=[]), workspace, spec, output,
                              RepairLimits(max_attempts=1)).run("Repair the answer")
    attempt = result.attempts[0]
    assert attempt["facts"]["changed_file_count"] == 20
    assert len(attempt["facts"]["changed_files"]) == 8
    assert len(attempt["recovery"]["failure_excerpt"]) <= 600
    assert not attempt["feedback_submitted_to_agent"]


def test_environment_error_is_not_resubmitted_as_patch_failure(scenario):
    workspace, _, _, output = scenario
    spec = VerificationSpec((str(workspace / "missing-program"),))
    model = ScriptedModel(LLMResponse(content="ready"))
    result = RepairController(Agent(model, tools=[]), workspace, spec, output).run("Repair the answer")
    assert result.status == "verification_error"
    assert len(model.prompts) == 1
    assert result.attempts[0]["recovery"]["kind"] == "verification_unavailable"
    assert not result.attempts[0]["feedback_submitted_to_agent"]


def test_truncated_tool_call_is_not_executed_during_stage_recovery(scenario):
    workspace, target, spec, output = scenario
    response = LLMResponse(finish_reason="length", tool_calls=[ToolCall("partial", "write_file", {
        "file_path": str(target), "content": "fixed"})])
    model = ScriptedModel(response, LLMResponse(content="still wrong"))
    result = RepairController(Agent(model, tools=[WriteFileTool()]), workspace, spec, output).run("Repair the answer")
    assert result.status == "no_progress"
    assert target.read_text() == "wrong"
    assert checkpoints.pending() == 0


@pytest.mark.parametrize("status,changed,expected", [
    ("output_limit", False, "truncated_without_changes"),
    ("output_limit", True, "modified_verification_failed"),
    ("responded", True, "modified_verification_failed"),
    ("empty_response", False, "no_changes"),
])
def test_feedback_stage_selection(status, changed, expected):
    facts = {"agent_status": status, "finish_reason": None, "files_changed": changed,
             "changed_file_count": int(changed), "changed_files": [], "successful_read_requests": []}
    feedback = _recovery_feedback("task", facts, {"status": "failed", "exit_code": 1, "feedback": "x" * 10000})
    assert feedback["kind"] == expected
    assert len(feedback["failure_excerpt"]) <= 600
    assert len(feedback["message"]) < 2000


def test_blocked_read_and_unmatched_tool_result_do_not_create_facts(tmp_path):
    agent = Agent(ScriptedModel(), tools=[])
    agent.messages = [
        LLMResponse(tool_calls=[ToolCall("blocked", "read_file", {"file_path": str(tmp_path / "a")})]).message,
        {"role": "tool", "tool_call_id": "blocked", "content": "Blocked by hook: no"},
        {"role": "tool", "tool_call_id": "unknown", "content": "1\tfake"},
    ]
    assert _execution_facts(agent, tmp_path, {}, {})["successful_read_requests"] == []
