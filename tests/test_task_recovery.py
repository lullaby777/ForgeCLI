"""Durable task recovery with scripted models and real local verification only."""
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from corecoder.agent import Agent
from corecoder.llm import LLMResponse, ToolCall
from corecoder.repair import RegressionSpec, RepairLimits, VerificationSpec
from corecoder.task_store import read_record, task_lock, write_record
from corecoder.tasks import RepairTask, TaskStorageError
from corecoder.tools.base import Tool
from tests.test_repair import ScriptedModel

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='First durable recovery version uses POSIX file locking')


@pytest.fixture
def scene(tmp_path):
    workspace = tmp_path / 'repo'
    workspace.mkdir()
    (workspace / 'answer.txt').write_text('wrong')
    (workspace / 'legacy.txt').write_text('ok')
    (workspace / 'tests').mkdir()
    (workspace / 'tests/test_old.py').write_text(
        "from pathlib import Path\ndef test_legacy():\n assert Path('legacy.txt').read_text() == 'ok', 'legacy broke'\n")
    check = tmp_path / 'accept.py'
    check.write_text("from pathlib import Path\nassert Path('answer.txt').read_text() == 'fixed'\n")
    return workspace, VerificationSpec((sys.executable, str(check)), (str(check),)), \
        RegressionSpec((sys.executable, '-m', 'pytest', 'tests', '-q')), tmp_path / 'task'


def create(scene, model, **limits):
    workspace, verification, regression, directory = scene
    agent = Agent(model, tools=[])
    task = RepairTask.create(agent, directory, 'Fix answer and preserve legacy', workspace, verification,
                             regression, RepairLimits(**limits))
    return task


def fix(scene, **files):
    def response():
        for name, content in files.items():
            (scene[0] / f'{name}.txt').write_text(content)
        return LLMResponse(content='ready')
    return response


def resume(scene, model):
    return RepairTask(Agent(model, tools=[]), scene[-1]).run()


def test_pause_resume_inherits_calls_attempts_rounds_and_original_baseline(scene):
    first = create(scene, ScriptedModel(KeyboardInterrupt()), rounds_per_attempt=2).run()
    assert first['status'] == 'paused'
    assert first['ledger']['model_calls'] == first['ledger']['attempts'] == first['ledger']['rounds'] == 1
    baseline = (scene[-1] / 'baseline.json').read_bytes()
    model = ScriptedModel(fix(scene, answer='fixed'))
    final = resume(scene, model)
    assert final['completed'] and final['ledger']['model_calls'] == 2
    assert final['ledger']['attempts'] == 1 and final['ledger']['rounds'] == 2
    assert final['ledger']['active_seconds'] >= first['ledger']['active_seconds']
    assert (scene[-1] / 'baseline.json').read_bytes() == baseline
    assert len(model.prompts) == 1


@pytest.mark.parametrize('limits,expected', [({'max_model_calls': 1}, 'model_call_limit'),
                                            ({'rounds_per_attempt': 1, 'max_attempts': 1}, 'attempt_limit')])
def test_resume_never_replenishes_exhausted_budgets(scene, limits, expected):
    create(scene, ScriptedModel(KeyboardInterrupt()), **limits).run()
    model = ScriptedModel()
    final = resume(scene, model)
    assert final['status'] == expected and not final['completed']
    assert final['ledger']['model_calls'] == final['ledger']['attempts'] == 1
    assert not model.prompts


def test_new_regression_stays_new_after_pause(scene):
    (scene[0] / 'tests/test_known.py').write_text('def test_known():\n assert False\n')
    def landed_then_pause():
        fix(scene, answer='fixed', legacy='broken')()
        raise KeyboardInterrupt
    first = create(scene, ScriptedModel(landed_then_pause), rounds_per_attempt=3, max_attempts=1).run()
    baseline = read_record(scene[-1] / 'baseline.json')
    assert baseline['result']['failures'] == ['tests.test_known::test_known']
    model = ScriptedModel(LLMResponse(content='claimed done'))
    final = resume(scene, model)
    assert not final['completed'] and final['checks']['regression']['new_failures'] == ['tests.test_old::test_legacy']
    assert 'regression_failed' in model.prompts[0]
    assert 'test_old::test_legacy' in model.prompts[0]
    assert read_record(scene[-1] / 'baseline.json') == baseline
    assert final['ledger']['model_calls'] == first['ledger']['model_calls'] + 1


def test_external_edit_invalidates_old_success_but_not_baseline_or_budget(scene):
    first = create(scene, ScriptedModel(fix(scene, answer='fixed'))).run()
    assert first['completed']
    (scene[0] / 'legacy.txt').write_text('broken')
    model = ScriptedModel(fix(scene, legacy='ok'))
    final = resume(scene, model)
    assert final['completed'] and final['ledger']['model_calls'] == 2
    assert final['ledger']['attempts'] == 2
    assert any(e.get('event') == 'workspace_changed' for e in final['history'])
    assert 'regression_failed' in model.prompts[0]
    assert not read_record(scene[-1] / 'baseline.json')['result']['failures']


def test_completed_unchanged_task_does_not_call_model_again(scene):
    first = create(scene, ScriptedModel(fix(scene, answer='fixed'))).run()
    model = ScriptedModel()
    final = resume(scene, model)
    assert final['completed'] and final['ledger']['model_calls'] == first['ledger']['model_calls']
    assert not model.prompts


@pytest.mark.parametrize('kind', ['test', 'config', 'baseline', 'missing_baseline', 'state'])
def test_changed_trusted_inputs_or_corrupt_checkpoint_block_resume(scene, kind):
    create(scene, ScriptedModel(KeyboardInterrupt())).run()
    if kind == 'test':
        (scene[0] / 'tests/test_old.py').write_text('def test_legacy():\n pass\n')
    elif kind == 'config':
        Path(scene[1].argv[1]).write_text('pass\n')
    elif kind == 'missing_baseline':
        (scene[-1] / 'baseline.json').unlink()
    else:
        (scene[-1] / ('baseline.json' if kind == 'baseline' else 'state.json')).write_text('{}')
    model = ScriptedModel()
    with pytest.raises(ValueError):
        resume(scene, model)
    assert not model.prompts


def test_unknown_tool_effect_is_not_replayed(scene):
    class Partial(Tool):
        name = 'partial'
        description = 'test tool'
        parameters: ClassVar[dict] = {'type': 'object', 'properties': {}}
        count = 0
        def execute(self):
            Partial.count += 1
            (scene[0] / 'answer.txt').write_text('fixed')
            raise KeyboardInterrupt
    model = ScriptedModel(LLMResponse(tool_calls=[ToolCall('partial-1', 'partial', {})]))
    agent = Agent(model, tools=[Partial()])
    task = RepairTask.create(agent, scene[-1], 'Fix', scene[0], scene[1], scene[2])
    first = task.run()
    assert first['status'] == 'paused' and first['pending']['kind'] == 'tools'
    next_model = ScriptedModel()
    result = RepairTask(Agent(next_model, tools=[Partial()]), scene[-1]).run()
    assert result['status'] == 'tools_outcome_unknown' and not result['completed']
    assert Partial.count == 1 and not next_model.prompts
    assert result['ledger']['model_calls'] == 1


def test_hard_process_exit_reserves_request_before_dispatch(scene):
    _, _, _, directory = scene
    create(scene, ScriptedModel(), rounds_per_attempt=2)
    script = """
import os,sys
from corecoder.agent import Agent
from corecoder.tasks import RepairTask, TaskStorageError
class CrashModel:
 def chat(self,*a,**kw): os._exit(23)
RepairTask(Agent(CrashModel(),tools=[]),sys.argv[1]).run()
"""
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, '-c', script, str(directory)], env=env, check=False)
    assert result.returncode == 23
    crashed = RepairTask.info(directory)
    assert crashed['pending']['kind'] == 'model' and crashed['ledger']['model_calls'] == 1
    final = resume(scene, ScriptedModel(fix(scene, answer='fixed')))
    assert final['completed'] and final['ledger']['model_calls'] == 2
    assert final['ledger']['attempts'] == 1


def test_checkpoint_write_failure_prevents_provider_call(scene, monkeypatch):
    from corecoder import tasks
    model = ScriptedModel(LLMResponse(content='must not run'))
    task = create(scene, model)
    original = tasks.write_record
    def fail_on_reservation(path, data, **kwargs):
        if (data.get('pending') or {}).get('kind') == 'model':
            raise OSError('simulated disk failure')
        return original(path, data, **kwargs)
    monkeypatch.setattr(tasks, 'write_record', fail_on_reservation)
    with pytest.raises(TaskStorageError):
        task.run()
    assert not model.prompts
    assert RepairTask.info(scene[-1])['ledger']['model_calls'] == 0


def test_task_lock_prevents_two_resumers(scene):
    task = create(scene, ScriptedModel())
    with task_lock(scene[-1]), pytest.raises(ValueError, match='already running'):
        task.run()


def test_freeze_file_is_exclusive_and_bad_checksum_not_rolled_back(tmp_path):
    path = tmp_path / 'record.json'
    write_record(path, {'used': 8}, exclusive=True)
    with pytest.raises(FileExistsError):
        write_record(path, {'used': 0}, exclusive=True)
    data = json.loads(path.read_text());data['payload']['used'] = 0
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        read_record(path)


def test_session_links_task_without_changing_legacy_load_format(scene, monkeypatch):
    from corecoder import session
    monkeypatch.setattr(session, 'SESSIONS_DIR', scene[-1].parent / 'sessions')
    task = create(scene, ScriptedModel())
    sid = session.save_session([], 'fake', task_checkpoint=task.directory)
    assert session.load_session(sid) == ([], 'fake')
    assert session.load_task_link(sid) == str(task.directory)


def test_cli_continue_is_program_control_not_a_model_prompt(scene, monkeypatch, capsys):
    from corecoder import cli, session
    monkeypatch.setattr(session, 'SESSIONS_DIR', scene[-1].parent / 'sessions')
    task = create(scene, ScriptedModel(KeyboardInterrupt()))
    task.run()
    agent = Agent(ScriptedModel(fix(scene, answer='fixed')), tools=[])
    agent.task_checkpoint = str(scene[-1])
    cli._continue_task(agent)
    assert 'Completed: True' in capsys.readouterr().out
    assert RepairTask.info(scene[-1])['completed']


def test_no_progress_counter_survives_an_interruption(scene):
    first = create(scene, ScriptedModel(LLMResponse(content='no change'), KeyboardInterrupt())).run()
    assert first['ledger']['stagnant'] == 1 and first['ledger']['attempts'] == 2
    final = resume(scene, ScriptedModel(LLMResponse(content='still no change')))
    assert final['status'] == 'no_progress' and final['ledger']['stagnant'] == 2
    assert final['ledger']['attempts'] == 2 and final['ledger']['model_calls'] == 3


def test_active_time_budget_is_inherited(scene):
    create(scene, ScriptedModel(KeyboardInterrupt())).run()
    state = RepairTask.info(scene[-1])
    state['ledger']['active_seconds'] = state['config']['limits']['max_seconds'] + 1
    write_record(scene[-1] / 'state.json', state)
    model = ScriptedModel()
    final = resume(scene, model)
    assert final['status'] == 'time_limit' and final['ledger']['model_calls'] == 1
    assert not model.prompts


def test_unknown_check_after_hard_exit_blocks_reexecution(scene):
    create(scene, ScriptedModel(KeyboardInterrupt())).run()
    state = RepairTask.info(scene[-1])
    state['pending'] = {'kind': 'check', 'charged_until': 1.0, 'phase': 'acceptance'}
    write_record(scene[-1] / 'state.json', state)
    model = ScriptedModel()
    final = resume(scene, model)
    assert final['status'] == 'check_outcome_unknown' and not model.prompts


def test_usage_is_persisted_and_unknown_cancelled_usage_is_marked(scene):
    first = create(scene, ScriptedModel(KeyboardInterrupt())).run()
    assert first['ledger']['unknown_usage_requests'] == 1
    def fix_with_usage():
        fix(scene, answer='fixed')()
        return LLMResponse(content='ready', prompt_tokens=12, completion_tokens=3)
    final = resume(scene, ScriptedModel(fix_with_usage))
    assert final['ledger']['prompt_tokens'] == 12 and final['ledger']['completion_tokens'] == 3
    assert final['ledger']['unknown_usage_requests'] == 1


def test_repl_selects_task_and_intercepts_continue_without_unbudgeted_chat(scene, monkeypatch, capsys):
    from corecoder import session
    from tests.conftest import repl_with
    monkeypatch.setattr(session, 'SESSIONS_DIR', scene[-1].parent / 'sessions')
    create(scene, ScriptedModel(KeyboardInterrupt())).run()
    model = ScriptedModel(fix(scene, answer='fixed'))
    agent = Agent(model, tools=[])
    agent.task_checkpoint = str(scene[-1])
    repl_with(monkeypatch, agent, ['/task', '继续任务', '/compact', 'arbitrary chat', '/save', 'quit'])
    out = capsys.readouterr().out
    assert 'Completed: True' in out and '任务配置和预算' in out
    assert len(model.prompts) == 1
    assert session.load_task_link(agent.session_id) == str(scene[-1])


def test_start_cli_links_session_before_model_and_resume_shares_task(scene, monkeypatch, capsys):
    from corecoder import cli, session
    monkeypatch.setattr(session, 'SESSIONS_DIR', scene[-1].parent / 'sessions')
    config = scene[-1].parent / 'config.json'
    config.write_text(json.dumps({'task': 'Fix answer', 'workspace': str(scene[0]),
        'checkpoint_dir': str(scene[-1]), 'verification': {'argv': list(scene[1].argv)},
        'regression': {'argv': list(scene[2].argv)}}))
    agent = Agent(ScriptedModel(KeyboardInterrupt()), tools=[])
    cli._start_task(agent, config)
    assert session.load_task_link(agent.session_id) == str(scene[-1])
    assert 'corecoder -r' in capsys.readouterr().out
    assert RepairTask.info(scene[-1])['status'] == 'paused'


def test_resume_from_another_terminal_directory(scene, monkeypatch):
    create(scene, ScriptedModel(KeyboardInterrupt())).run()
    monkeypatch.chdir(scene[0])
    final = resume(scene, ScriptedModel(fix(scene, answer='fixed')))
    assert final['completed'] and final['ledger']['model_calls'] == 2


def test_offline_demo_runs_real_tools_pause_and_recover_without_model(tmp_path):
    from examples.task_recovery_demo import run_demo
    state = run_demo(tmp_path / 'demo')
    assert state['completed'] and state['ledger']['model_calls'] == 4


def test_todo_is_restored_separately_from_chat_history(scene):
    from corecoder.tools.todo import TodoWriteTool
    todo = TodoWriteTool()
    todo.execute([{'content': 'Preserve legacy behavior', 'status': 'in_progress'}])
    task = RepairTask.create(Agent(ScriptedModel(KeyboardInterrupt()), tools=[todo]), scene[-1], 'Fix',
                             scene[0], scene[1], scene[2])
    first = task.run()
    assert first['todo'] == todo._tasks
    new_todo = TodoWriteTool()
    final = RepairTask(Agent(ScriptedModel(fix(scene, answer='fixed')), tools=[new_todo]), scene[-1]).run()
    assert final['completed'] and final['todo'] == first['todo']
    assert new_todo._tasks == []  # task state did not leak into another agent's tool


def test_failed_request_usage_remains_unknown_and_budget_stays_spent(scene):
    final = create(scene, ScriptedModel(RuntimeError('provider error'))).run()
    assert final['status'] == 'agent_error' and final['ledger']['model_calls'] == 1
    assert final['ledger']['unknown_usage_requests'] == 1 and not final['completed']


def test_safe_point_recovery_checks_landed_code_before_another_model_call(scene):
    create(scene, ScriptedModel(fix(scene, answer='fixed'))).run()
    state = RepairTask.info(scene[-1])
    # Represent the safe, fully paired checkpoint just before final verification.
    state.update(completed=False, phase='agent', status='ready', attempt_open=True, attempt_finished=False)
    state['checks'] = {}
    write_record(scene[-1] / 'state.json', state)
    model = ScriptedModel()
    final = resume(scene, model)
    assert final['completed'] and final['ledger']['model_calls'] == 1 and not model.prompts


# --- resuming a tool batch that was cut off, when the caller guarantees it stopped ---

def _interrupting_tool(scene, content):
    class Partial(Tool):
        name = 'partial'
        description = 'test tool'
        parameters: ClassVar[dict] = {'type': 'object', 'properties': {}}
        count = 0
        def execute(self):
            Partial.count += 1
            (scene[0] / 'answer.txt').write_text(content)
            raise KeyboardInterrupt  # the process dies in the middle of the tool batch
    return Partial


def _start_and_cut(scene, Partial):
    model = ScriptedModel(LLMResponse(tool_calls=[ToolCall('partial-1', 'partial', {})]))
    task = RepairTask.create(Agent(model, tools=[Partial()]), scene[-1], 'Fix', scene[0], scene[1], scene[2])
    first = task.run()
    assert first['pending']['kind'] == 'tools'


def test_terminated_interrupted_tools_are_verified_not_replayed(scene):
    Partial = _interrupting_tool(scene, 'fixed')  # the cut-off command had already written the fix
    _start_and_cut(scene, Partial)
    next_model = ScriptedModel()
    result = RepairTask(Agent(next_model, tools=[Partial()]), scene[-1], interrupted_tools_terminated=True).run()
    assert result['status'] == 'passed' and result['completed']
    assert Partial.count == 1  # never re-executed
    assert not next_model.prompts  # verification alone finished the task
    assert result['ledger']['model_calls'] == 1  # budget carried over, not reset
    assert any(h.get('event') == 'interrupted_tools_recovered' and h['tools'] == ['partial'] for h in result['history'])


def test_terminated_interrupted_tools_continue_with_explicit_notice(scene):
    Partial = _interrupting_tool(scene, 'half-done')  # partial effect that does not pass
    _start_and_cut(scene, Partial)
    next_model = ScriptedModel(fix(scene, answer='fixed'))
    agent = Agent(next_model, tools=[Partial()])
    result = RepairTask(agent, scene[-1], interrupted_tools_terminated=True).run()
    assert result['status'] == 'passed' and result['completed']
    assert Partial.count == 1
    replies = [m['content'] for m in result['messages'] if m.get('role') == 'tool' and m.get('tool_call_id') == 'partial-1']
    assert replies == [RepairTask.INTERRUPTED_TOOL_REPLY]  # the agent is told the call was cut off
    assert result['ledger']['model_calls'] == 2


def test_without_termination_guarantee_unknown_tools_still_block(scene):
    Partial = _interrupting_tool(scene, 'fixed')
    _start_and_cut(scene, Partial)
    result = RepairTask(Agent(ScriptedModel(), tools=[Partial()]), scene[-1]).run()
    assert result['status'] == 'tools_outcome_unknown' and not result['completed']
