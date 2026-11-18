"""Durable single-agent repair tasks. A checkpoint is authoritative, not a summary."""
import copy
import math
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from .agent import RunResult
from .messages import clean_history
from .prompt import system_prompt
from .repair import (
    RegressionSpec,
    RepairLimits,
    VerificationSpec,
    _execution_facts,
    _fingerprint,
    _recovery_feedback,
    _workspace_files,
)
from .task_store import checksum, read_record, task_lock, write_record
from .tools.bash import get_tracked_cwd, set_tracked_cwd
from .verification import VerificationRunner


class TaskStop(RuntimeError):
    pass


class TaskStorageError(RuntimeError):
    pass


def _identity(agent, workspace):
    model = agent.llm
    return {'model': getattr(model, 'model', None), 'temperature': getattr(model, 'temperature', None),
            'max_tokens': getattr(model, 'max_tokens', None),
            'endpoint_hash': checksum(str(getattr(getattr(model, 'client', None), 'base_url', ''))),
            'context_tokens': agent.context.max_tokens, 'empty_retries': agent.max_empty_retries,
            'hooks': checksum({'pre': getattr(agent.hooks, 'pre', []), 'post': getattr(agent.hooks, 'post', [])}),
            'python': sys.version,
            'tools': checksum([t.schema() for t in agent.tools if t.name != 'agent']),
            'system': checksum(system_prompt([t for t in agent.tools if t.name != 'agent'], cwd=str(workspace))),
            'engine': {name: checksum((Path(__file__).parent / name).read_text())
                       for name in ('agent.py', 'repair.py', 'tasks.py', 'task_store.py', 'prompt.py', 'verification.py')}}


class RepairTask:
    """Use create once, then open the same directory to resume; no budget overrides."""
    def __init__(self, agent, directory):
        self.agent = agent
        self.directory = Path(directory).expanduser().resolve()
        self.state = None
        self._tick = None

    @classmethod
    def create(cls, agent, directory, task, workspace, verification, regression, limits=None):
        if regression is None:
            raise ValueError('Durable tasks require a regression gate')
        workspace, directory = Path(workspace).resolve(), Path(directory).expanduser().resolve()
        if not workspace.is_dir() or directory == workspace or workspace in directory.parents:
            raise ValueError('Task directory must be new and outside the workspace')
        limits = limits or RepairLimits()
        protected = tuple(dict.fromkeys((*verification.protected_paths, *regression.protected_paths,
                          'pyproject.toml', 'pytest.ini', 'conftest.py', 'setup.cfg', 'tox.ini')))
        command_files = [str((workspace / arg).resolve()) for argv in (verification.argv, regression.argv)
                         for arg in argv if (workspace / arg).is_file()]
        protected = tuple(dict.fromkeys((*protected, *command_files)))
        config = {'workspace': str(workspace), 'verification': asdict(verification), 'regression': asdict(regression),
                  'limits': asdict(limits), 'protected': list(protected), 'agent': _identity(agent, workspace)}
        state = {'version': 1, 'id': uuid.uuid4().hex, 'task': task, 'config': config, 'config_hash': checksum(config),
                 'status': 'ready', 'phase': 'baseline', 'completed': False, 'baseline_hash': None,
                 'initial_workspace': _fingerprint([workspace]), 'workspace_hash': _fingerprint([workspace]),
                 'protected_hash': _fingerprint([workspace / p for p in protected]),
                 'ledger': {'model_calls': 0, 'attempts': 0, 'rounds': 0, 'active_seconds': 0.0,
                            'stagnant': 0, 'previous_failure': None, 'prompt_tokens': 0,
                            'completion_tokens': 0, 'unknown_usage_requests': 0},
                 'attempt_open': False, 'attempt_finished': False, 'checks': {}, 'check_sequence': 0,
                 'messages': clean_history(copy.deepcopy(agent.messages)),
                 'todo': copy.deepcopy(agent._todo._tasks) if agent._todo is not None else [], 'plan_mode': agent.plan_mode,
                 'agent_run': asdict(agent.last_run),
                 'cwd': str(workspace), 'pending': None, 'feedback': None, 'next_prompt': task, 'history': []}
        directory.mkdir(parents=True, exist_ok=False)
        write_record(directory / 'state.json', state, exclusive=True)
        return cls(agent, directory)

    @staticmethod
    def info(directory):
        state = read_record(Path(directory).expanduser().resolve() / 'state.json')
        if not isinstance(state, dict) or state.get('version') != 1:
            raise ValueError('Unsupported task format')
        if not {'config', 'ledger', 'messages', 'pending', 'phase', 'status', 'completed'} <= state.keys():
            raise ValueError('Incomplete task checkpoint')
        return state

    def _save(self):
        now = time.monotonic()
        if self._tick is not None:
            self.state['ledger']['active_seconds'] += max(0, now - self._tick)
        self._tick = now
        self.state.update(messages=copy.deepcopy(self.agent.messages), cwd=get_tracked_cwd(),
                          plan_mode=self.agent.plan_mode, agent_run=asdict(self.agent.last_run))
        if self.agent._todo is not None:
            self.state['todo'] = copy.deepcopy(self.agent._todo._tasks)
        self.state['workspace_hash'] = _fingerprint([self.workspace])
        try:
            write_record(self.directory / 'state.json', self.state)
        except OSError as exc:
            raise TaskStorageError('Checkpoint could not be committed; execution stopped') from exc

    def _remaining(self):
        elapsed = self.state['ledger']['active_seconds'] + max(0, time.monotonic() - self._tick)
        return self.limits.max_seconds - elapsed

    def _hook(self, event, **fields):
        state = self.state
        if event == 'round_started':
            self._time_check()
            if state['ledger']['rounds'] >= self.limits.rounds_per_attempt:
                raise TaskStop('round_limit')
            state['ledger']['rounds'] += 1
            state['pending'] = None
        elif event == 'tools_started':
            self._time_check()
            state['pending'] = {'kind': 'tools', 'calls': fields['calls'], 'charged_until': time.time()}
        else:  # response saved, tools planned, or all tool replies saved
            state['pending'] = None
        self._save()  # must succeed before a tool or model side effect

    def _time_check(self):
        if self._remaining() <= 0:
            raise TaskStop('time_limit')
        if self._old_check is not None:
            self._old_check()

    def _model_call(self, *args, **kwargs):
        self._time_check()
        if self.state['ledger']['model_calls'] >= self.limits.max_model_calls:
            raise TaskStop('model_call_limit')
        self.state['ledger']['model_calls'] += 1
        self.state['pending'] = {'kind': 'model', 'charged_until': time.time()}
        self._save()  # reserve persistently before even entering the provider
        response = self._model.chat(*args, **kwargs)
        self.state['ledger']['prompt_tokens'] += getattr(response, 'prompt_tokens', 0) or 0
        self.state['ledger']['completion_tokens'] += getattr(response, 'completion_tokens', 0) or 0
        self.state['pending']['usage_recorded'] = True
        self._save()
        return response

    def _check(self, phase):
        self._time_check()
        state = self.state
        before = _fingerprint([self.workspace])
        state['check_sequence'] += 1
        label = f"task-{state['check_sequence']}"
        state['pending'] = {'kind': 'check', 'phase': phase, 'charged_until': time.time()}
        self._save()
        timeout = min(self._remaining(), self.limits.verifier_seconds)
        if timeout <= 0:
            state['pending'] = None
            raise TaskStop('time_limit')
        try:
            if phase == 'acceptance':
                result = self.checker.verify(label, timeout)
            else:
                result = self.checker.regress(label, timeout, self.baseline if phase == 'regression' else None)
        except KeyboardInterrupt:
            # VerificationRunner killed and waited for its local subprocess group.
            state['pending'] = None
            raise
        if _fingerprint(self.protected) != state['protected_hash']:
            raise TaskStop('protected_inputs_changed')
        if _fingerprint([self.workspace]) != before:
            raise TaskStop('workspace_changed_during_verification')
        state['pending'] = None
        result['input_hash'] = before
        state['checks'][phase] = result
        self._save()
        self._time_check()
        return result

    def _baseline(self):
        path = self.directory / 'baseline.json'
        if path.exists():
            frozen = read_record(path)
            if (frozen['config_hash'] != self.state['config_hash']
                    or frozen['workspace_start'] != self.state['initial_workspace']):
                raise ValueError('Baseline belongs to another task')
            if self.state['baseline_hash'] and checksum(frozen) != self.state['baseline_hash']:
                raise ValueError('Frozen baseline changed')
        else:
            if self.state['baseline_hash'] or _fingerprint([self.workspace]) != self.state['initial_workspace']:
                raise ValueError('Original baseline missing; cannot recapture from edited code')
            result = self._check('baseline')
            if result['status'] != 'passed':
                raise TaskStop('baseline_' + result['status'])
            frozen = {'config_hash': self.state['config_hash'], 'workspace_start': self.state['initial_workspace'], 'result': result}
            write_record(path, frozen, exclusive=True)
        self.state['baseline_hash'] = checksum(frozen)
        self.baseline = frozen['result']
        if self.state['phase'] == 'baseline':
            self.state['phase'] = 'agent'
        self._save()

    def _failure(self, result, phase):
        state, ledger = self.state, self.state['ledger']
        before = state.get('before_files', _workspace_files(self.workspace))
        facts = _execution_facts(self.agent, self.workspace, before, _workspace_files(self.workspace))
        feedback = _recovery_feedback(state['task'], facts, {**result, 'phase': phase})
        state.update(feedback=feedback, next_prompt=feedback['message'], completed=False)
        state['history'].append({'phase': phase, 'status': result['status'], 'input_hash': result['input_hash'],
                                 'attempt': ledger['attempts'], 'model_calls': ledger['model_calls']})
        if result['status'] != 'failed':
            raise TaskStop(phase + '_' + result['status'])
        if state.get('agent_error'):
            raise TaskStop('agent_error')
        if state['attempt_open'] and (state['attempt_finished'] or ledger['rounds'] >= self.limits.rounds_per_attempt):
            signature = checksum([result['input_hash'], phase, result.get('exit_code'), result['status']])
            unchanged = state.get('attempt_before') == result['input_hash']
            ledger['stagnant'] = ledger['stagnant'] + 1 if unchanged and ledger['previous_failure'] in (None, signature) else 0
            ledger['previous_failure'] = signature
            state['attempt_open'] = False
        if ledger['stagnant'] >= self.limits.max_stagnant_attempts:
            raise TaskStop('no_progress')
        if ledger['model_calls'] >= self.limits.max_model_calls:
            raise TaskStop('model_call_limit')
        if not state['attempt_open'] and ledger['attempts'] >= self.limits.max_attempts:
            raise TaskStop('attempt_limit')
        state['phase'] = 'agent'
        self._save()

    def _execute(self):
        state, ledger = self.state, self.state['ledger']
        while state['phase'] not in {'done', 'stopped'}:
            self._time_check()
            if state['phase'] == 'agent':
                if ledger['model_calls'] >= self.limits.max_model_calls:
                    raise TaskStop('model_call_limit')
                if not state['attempt_open']:
                    if ledger['attempts'] >= self.limits.max_attempts:
                        raise TaskStop('attempt_limit')
                    ledger['attempts'] += 1
                    ledger['rounds'] = 0
                    state.update(attempt_open=True, attempt_finished=False, agent_error=False,
                                 attempt_before=_fingerprint([self.workspace]), before_files=_workspace_files(self.workspace))
                remaining_rounds = self.limits.rounds_per_attempt - ledger['rounds']
                if remaining_rounds <= 0:
                    state['phase'] = 'acceptance'
                    continue
                self.agent.max_rounds = remaining_rounds
                self._save()
                try:
                    prompt = (f"Workspace: {self.workspace}. Use absolute paths for file tools.\n"
                              f"Do not change protected verification inputs: {self.protected}.\n"
                              "Do not read or edit task checkpoint/baseline artifacts. "
                              "Completion requires both acceptance and regression gates.\n" + state['next_prompt'])
                    self.agent.chat(prompt)
                    state['attempt_finished'] = True
                except TaskStorageError:
                    raise
                except TaskStop:
                    state['phase'] = 'acceptance'
                    if state['pending'] and state['pending']['kind'] == 'tools':
                        raise
                except KeyboardInterrupt:
                    state['phase'] = 'acceptance'
                    raise
                except Exception as exc:
                    if state['pending'] and state['pending']['kind'] == 'tools':
                        raise TaskStop('tool_outcome_unknown') from exc
                    if state['pending'] and state['pending']['kind'] == 'model' and not state['pending'].get('usage_recorded'):
                        ledger['unknown_usage_requests'] += 1
                    state.update(agent_error=True, error_type=type(exc).__name__, attempt_finished=True)
                state['pending'] = None
                state['phase'] = 'acceptance'
                self._save()
            elif state['phase'] == 'acceptance':
                result = self._check('acceptance')
                if result['status'] == 'passed':
                    state['phase'] = 'regression'
                    self._save()
                else:
                    self._failure(result, 'acceptance')
            elif state['phase'] == 'regression':
                result = self._check('regression')
                if result['status'] == 'passed':
                    state.update(completed=True, status='passed', phase='done', attempt_open=False)
                    self._save()
                else:
                    self._failure(result, 'regression')
            else:
                raise ValueError('Invalid task phase')

    def run(self):
        with task_lock(self.directory):
            return self._run_locked()

    def _run_locked(self):
        state = self.info(self.directory)
        self.state = state
        if checksum(state['config']) != state['config_hash'] or _identity(self.agent, state['config']['workspace']) != state['config']['agent']:
            raise ValueError('Task configuration or agent runtime changed')
        cfg = state['config']
        self.workspace = Path(cfg['workspace'])
        if not self.workspace.is_dir():
            raise ValueError('Task workspace missing; restore the same directory')
        self.limits = RepairLimits(**cfg['limits'])
        for key in ('model_calls', 'attempts', 'rounds', 'stagnant'):
            if type(state['ledger'][key]) is not int or state['ledger'][key] < 0:
                raise ValueError('Invalid task budget ledger')
        if not math.isfinite(state['ledger']['active_seconds']) or state['ledger']['active_seconds'] < 0:
            raise ValueError('Invalid task time ledger')
        self.protected = [self.workspace / p for p in cfg['protected']]
        if _fingerprint(self.protected) != state['protected_hash']:
            raise ValueError('Protected tests or verification configuration changed')
        previous_hash = state['workspace_hash']
        self.checker = VerificationRunner(self.workspace, VerificationSpec(**cfg['verification']),
                                          self.directory, regression=RegressionSpec(**cfg['regression']))
        self._model = self.agent.llm
        old = (self.agent.checkpoint_hook, self.agent.execution_check, self.agent.max_rounds, get_tracked_cwd(),
               self.agent.tools, self.agent._tool_by_name, self.agent._todo, self.agent._system)
        self._old_check = self.agent.execution_check
        self.agent.messages = clean_history(copy.deepcopy(state['messages']))
        self.agent.last_run = RunResult(**state['agent_run'])
        self.agent.plan_mode = state['plan_mode']
        # Do not restore a process or a sub-agent. Main-agent tool batches are
        # journalled as a whole; unknown partial batches block automatic resume.
        self.agent.tools = [copy.copy(t) for t in self.agent.tools if t.name != 'agent']
        self.agent._tool_by_name = {t.name: t for t in self.agent.tools}
        self.agent._system = system_prompt(self.agent.tools, cwd=str(self.workspace))
        self.agent._todo = next((t for t in self.agent.tools if t.name == 'todo_write'), None)
        if self.agent._todo is not None:
            self.agent._todo._tasks = copy.deepcopy(state['todo'])
        set_tracked_cwd(state['cwd'] if state['cwd'] and Path(state['cwd']).is_dir() else str(self.workspace))
        owner = self
        class BudgetModel:
            def __getattr__(self, name):
                return getattr(owner._model, name)
            def chat(self, *args, **kwargs):
                return owner._model_call(*args, **kwargs)
        self.agent.llm = BudgetModel()
        self.agent.checkpoint_hook = self._hook
        self.agent.execution_check = self._time_check
        self._tick = time.monotonic()
        try:
            pending = state['pending']
            if pending:
                # A hard crash has an unknown elapsed interval. Charge it
                # conservatively (including downtime), never replenish time.
                gap = time.time() - pending['charged_until']
                remaining = max(0, self.limits.max_seconds - state['ledger']['active_seconds'])
                state['ledger']['active_seconds'] += remaining if gap < 0 else min(gap, remaining)
                pending['charged_until'] = time.time()
                if pending['kind'] != 'model':
                    raise TaskStop(pending['kind'] + '_outcome_unknown')
                if not pending.get('usage_recorded'):
                    state['ledger']['unknown_usage_requests'] += 1
                state['pending'] = None
                state['phase'] = 'acceptance'
            # Complete protocol pairs without ever replaying historical calls.
            for message in list(self.agent.messages):
                if message.get('role') == 'assistant' and message.get('tool_calls'):
                    calls = [SimpleNamespace(id=t['id']) for t in message['tool_calls']]
                    self.agent._answer_pending_tool_calls(calls, '[not replayed: interrupted before confirmed execution]')
            self._baseline()
            current = _fingerprint([self.workspace])
            if current != previous_hash:
                state.update(checks={}, completed=False, status='ready', phase='acceptance')
                state['history'].append({'event': 'workspace_changed', 'old_checks_invalidated': True})
            if state['status'] == 'paused':
                state['status'] = 'ready'
            if state['phase'] == 'agent' and state['ledger']['model_calls'] > 0:
                # A safe checkpoint may have landed tool effects just before
                # process exit. Verify them before spending another request.
                state['phase'] = 'acceptance'
            self._execute()
        except KeyboardInterrupt:
            state.update(status='paused', completed=False)
            if state['pending'] and state['pending']['kind'] == 'model':
                if not state['pending'].get('usage_recorded'):
                    state['ledger']['unknown_usage_requests'] += 1
                state['pending'] = None  # request was cancelled; its budget stays spent
            self._save()
        except TaskStorageError:
            state.update(status='checkpoint_error', completed=False, phase='stopped')
            raise
        except TaskStop as exc:
            state.update(status=str(exc), completed=False)
            if not state['pending']:
                state['phase'] = 'stopped'
            self._save()
        finally:
            try:
                self._save()
            finally:
                self.agent.llm = self._model
                (self.agent.checkpoint_hook, self.agent.execution_check, self.agent.max_rounds, cwd,
                 self.agent.tools, self.agent._tool_by_name, self.agent._todo, self.agent._system) = old
                set_tracked_cwd(cwd)
                self._tick = None
        return copy.deepcopy(state)
