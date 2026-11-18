"""ISSUE-001: deterministic responses only; never contact a model provider."""
import copy
import json
import threading
from types import SimpleNamespace
from typing import ClassVar

import pytest

from corecoder import session
from corecoder.agent import Agent
from corecoder.cli import _run_once
from corecoder.config import Config
from corecoder.llm import LLM, LLMResponse, ToolCall
from corecoder.messages import clean_history
from corecoder.tools.base import Tool
from tests.conftest import repl_with


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, messages, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class CounterTool(Tool):
    name = "counter"
    description = "count side effects"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def __init__(self):
        self.calls = 0

    def execute(self):
        self.calls += 1
        return "counted"


def call_response(**kwargs):
    return LLMResponse(tool_calls=[ToolCall("c1", "counter", {})], **kwargs)


@pytest.mark.parametrize("content", ["", "  \n", None])
def test_empty_retry_has_identical_valid_history_then_normal_response(content, tmp_path):
    model = ScriptedModel(LLMResponse(content=content), LLMResponse(content="done", finish_reason="stop"))
    agent = Agent(model, tools=[], event_log_path=tmp_path / "events.jsonl")
    assert agent.chat("SECRET INPUT") == "done"
    assert model.requests[0] == model.requests[1]
    assert agent.messages == [{"role": "user", "content": "SECRET INPUT"}, {"role": "assistant", "content": "done"}]
    assert agent.last_run.status == "responded"
    assert agent.last_run.empty_retries == 1
    events = [json.loads(line) for line in agent.event_log_path.read_text().splitlines()]
    assert events[-1]["status"] == "responded"
    assert events[-1]["finish_reason"] == "stop"
    assert "SECRET INPUT" not in agent.event_log_path.read_text()


def test_repeated_reasoning_only_response_stops_and_is_not_saved(tmp_path, monkeypatch):
    model = ScriptedModel(LLMResponse(reasoning_content="internal"), LLMResponse())
    agent = Agent(model, tools=[])
    assert agent.chat("task")
    assert agent.last_run.status == "empty_response"
    assert len(model.requests) == 2
    assert agent.messages == [{"role": "user", "content": "task"}]
    monkeypatch.setattr(session, "SESSIONS_DIR", tmp_path)
    sid = session.save_session(agent.messages, "fake")
    assert session.load_session(sid) == (agent.messages, "fake")


@pytest.mark.parametrize("retries,max_rounds,expected_calls", [(0, 50, 1), (4, 2, 2), (1, 1, 1)])
def test_empty_retries_obey_both_limits(retries, max_rounds, expected_calls):
    model = ScriptedModel(*(LLMResponse() for _ in range(6)))
    agent = Agent(model, tools=[], max_empty_retries=retries, max_rounds=max_rounds)
    agent.chat("task")
    assert agent.last_run.status == "empty_response"
    assert len(model.requests) == expected_calls


def test_empty_retry_does_not_replay_side_effect_tools():
    tool = CounterTool()
    model = ScriptedModel(call_response(), LLMResponse(), LLMResponse())
    agent = Agent(model, tools=[tool])
    agent.chat("task")
    assert tool.calls == 1
    assert model.requests[1] == model.requests[2]
    assert agent.last_run.status == "empty_response"
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "tool"]
    assert agent.messages[1]["content"] is None
    assert agent.messages[2]["tool_call_id"] == "c1"


def test_round_limit_preserves_tool_results_and_reports_status(tmp_path):
    tool = CounterTool()
    agent = Agent(ScriptedModel(call_response()), tools=[tool], max_rounds=1,
                  event_log_path=tmp_path / "events.jsonl")
    assert "maximum" in agent.chat("task")
    assert agent.last_run.status == "round_limit"
    assert agent.last_run.rounds == 1
    assert tool.calls == 1
    assert agent.messages[-1]["role"] == "tool"
    events = [json.loads(line) for line in agent.event_log_path.read_text().splitlines()]
    assert {"tool_started", "tool_returned", "run_ended"} <= {e["event"] for e in events}


@pytest.mark.parametrize("reason,status", [("length", "output_limit"), ("content_filter", "filtered")])
def test_incomplete_tool_call_is_never_executed(reason, status):
    tool = CounterTool()
    agent = Agent(ScriptedModel(call_response(finish_reason=reason)), tools=[tool])
    agent.chat("task")
    assert agent.last_run.status == status
    assert tool.calls == 0
    assert agent.messages == [{"role": "user", "content": "task"}]


def test_partial_text_is_preserved_but_not_reported_as_normal_completion():
    agent = Agent(ScriptedModel(LLMResponse(content="partial", finish_reason="length")), tools=[])
    assert agent.chat("task") == "partial"
    assert agent.last_run.status == "output_limit"
    assert agent.messages[-1]["content"] == "partial"


@pytest.mark.parametrize("error,status", [(RuntimeError("SECRET ERROR"), "error"), (KeyboardInterrupt(), "interrupted")])
def test_exceptions_have_terminal_status_and_safe_log(error, status, tmp_path):
    agent = Agent(ScriptedModel(error), tools=[], event_log_path=tmp_path / "events.jsonl")
    with pytest.raises(type(error)):
        agent.chat("SECRET PROMPT")
    events = [json.loads(line) for line in agent.event_log_path.read_text().splitlines()]
    assert events[-1]["status"] == status
    assert "SECRET" not in agent.event_log_path.read_text()
    assert agent.last_run.status == status


def test_interrupt_during_tool_callback_backfills_history():
    agent = Agent(ScriptedModel(call_response()), tools=[CounterTool()])

    def interrupt(*args):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        agent.chat("task", on_tool=interrupt)
    assert agent.last_run.status == "interrupted"
    assert agent.messages[-1]["role"] == "tool"
    assert agent.messages[-1]["tool_call_id"] == "c1"


def test_load_legacy_history_repairs_only_empty_turn_without_rewriting_disk(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(session, "SESSIONS_DIR", tmp_path)
    valid_tool_turn = call_response().message
    history = [{"role": "user", "content": "task"}, valid_tool_turn,
               {"role": "tool", "tool_call_id": "c1", "content": "counted"},
               {"role": "assistant", "content": None}]
    raw = json.dumps({"messages": history, "model": "fake"}).encode()
    path = tmp_path / "legacy.json"
    path.write_bytes(raw)
    messages, model = session.load_session("legacy")
    assert model == "fake"
    assert messages == history[:-1]
    assert path.read_bytes() == raw
    assert "history_repaired" in caplog.text
    # Simulate the provider's validity check before returning a reply.
    class ValidatingModel:
        def chat(self, messages, **kwargs):
            assert clean_history(messages) == messages
            return LLMResponse(content="resumed")
    agent = Agent(ValidatingModel(), tools=[])
    agent.messages = messages
    assert agent.chat("continue") == "resumed"


def test_save_and_direct_agent_history_are_also_repaired(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "SESSIONS_DIR", tmp_path)
    history = [{"role": "assistant", "content": "  "}]
    sid = session.save_session(history, "fake")
    assert json.loads((tmp_path / f"{sid}.json").read_text())["messages"] == []
    assert len(history) == 1
    agent = Agent(ScriptedModel(LLMResponse(content="ok")), tools=[])
    agent.messages = copy.deepcopy(history)
    agent.chat("new")
    assert agent.messages[0]["role"] == "user"


def test_next_turn_and_reset_do_not_keep_previous_error_status():
    agent = Agent(ScriptedModel(LLMResponse(), LLMResponse(content="ok")), tools=[], max_empty_retries=0)
    agent.chat("first")
    assert agent.last_run.status == "empty_response"
    agent.chat("second")
    assert agent.last_run.status == "responded"
    assert agent.last_run.empty_retries == 0
    agent.reset()
    assert agent.last_run.status == "idle"


def test_unwritable_event_log_does_not_break_execution(tmp_path, caplog):
    agent = Agent(ScriptedModel(LLMResponse(content="ok")), tools=[], event_log_path=tmp_path)
    assert agent.chat("task") == "ok"
    assert "event_log_unavailable" in caplog.text


def test_stream_records_finish_reason_and_usage_even_when_delta_missing():
    llm = LLM.__new__(LLM)
    llm.total_prompt_tokens = llm.total_completion_tokens = 0
    llm._usage_lock = threading.Lock()
    stream = iter([
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(reasoning_content="thinking"), finish_reason=None)]),
        SimpleNamespace(choices=[SimpleNamespace(delta=None, finish_reason="length")]),
        SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4)),
    ])
    response = llm._drain(stream, None, None)
    assert response.finish_reason == "length"
    assert response.content == ""
    assert response.reasoning_content == "thinking"
    assert (response.prompt_tokens, response.completion_tokens) == (12, 4)


def test_missing_finish_reason_stays_unknown():
    agent = Agent(ScriptedModel(LLMResponse(content="ok")), tools=[])
    agent.chat("task")
    assert agent.last_run.finish_reason is None
    assert agent.last_run.status == "responded"


def test_cli_shows_round_limit_even_after_reasoning_stream(monkeypatch, capsys):
    class ReasoningModel:
        def chat(self, messages, on_reasoning=None, **kwargs):
            if on_reasoning:
                on_reasoning("thinking")
            return call_response()
    agent = Agent(ReasoningModel(), tools=[CounterTool()], max_rounds=1)
    repl_with(monkeypatch, agent, ["task", "quit"], Config(model="fake"))
    output = capsys.readouterr().out
    assert "round_limit" in output
    assert "maximum tool-call rounds" in output


def test_one_shot_empty_response_exits_nonzero(capsys):
    agent = Agent(ScriptedModel(LLMResponse()), tools=[], max_empty_retries=0)
    with pytest.raises(SystemExit) as exc:
        _run_once(agent, "task")
    assert exc.value.code == 1
    assert "empty_response" in capsys.readouterr().out
