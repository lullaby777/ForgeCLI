"""Bounded execution + independent local verification, outside Agent.chat."""

import hashlib
import json
import math
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .tools.bash import get_tracked_cwd, set_tracked_cwd
from .verification import VerificationRunner

_IGNORED = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"}


@dataclass(frozen=True)
class RepairLimits:
    max_attempts: int = 3
    max_model_calls: int = 30
    rounds_per_attempt: int = 10
    max_seconds: float = 300
    verifier_seconds: float = 30
    max_stagnant_attempts: int = 2

    def __post_init__(self):
        for name in ("max_attempts", "max_model_calls", "rounds_per_attempt", "max_stagnant_attempts"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_seconds", "verifier_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")


@dataclass(frozen=True)
class VerificationSpec:
    # Trusted caller configuration. Never accept a test command from the model.
    argv: tuple[str, ...]
    protected_paths: tuple[str, ...] = ("tests",)
    failure_codes: tuple[int, ...] = (1,)

    def __post_init__(self):
        if not self.argv or isinstance(self.argv, str) or not all(isinstance(s, str) and s for s in self.argv):
            raise ValueError("argv must be a non-empty sequence of arguments")
        if isinstance(self.protected_paths, str) or not all(isinstance(p, str) and p for p in self.protected_paths):
            raise ValueError("protected_paths must be a sequence of paths")
        if not all(type(code) is int and code > 0 for code in self.failure_codes):
            raise ValueError("failure_codes must contain positive exit codes")
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "protected_paths", tuple(self.protected_paths))
        object.__setattr__(self, "failure_codes", tuple(self.failure_codes))


@dataclass(frozen=True)
class RegressionSpec:
    """Trusted pytest command; controller supplies an external JUnit report path."""
    argv: tuple[str, ...]
    protected_paths: tuple[str, ...] = ("tests",)

    def __post_init__(self):
        validated = VerificationSpec(self.argv, self.protected_paths)
        if any(arg.startswith(("--junitxml", "--junit-xml")) for arg in validated.argv):
            raise ValueError("The controller supplies the JUnit output path")
        object.__setattr__(self, "argv", validated.argv)
        object.__setattr__(self, "protected_paths", validated.protected_paths)


@dataclass
class RepairResult:
    status: str = "running"
    attempts: list[dict] = field(default_factory=list)
    model_calls: int = 0
    elapsed_seconds: float = 0
    error_type: str | None = None
    completion_scope: str = "acceptance_only"
    completed: bool = False
    acceptance_status: str = "not_run"
    regression_status: str = "not_configured"
    regression_baseline: dict | None = None


class BudgetExceeded(RuntimeError):
    pass


def _fingerprint(paths):
    """Hash actual files and symlink destinations; ignore only generated caches."""
    digest = hashlib.sha256()
    for root in paths:
        root = Path(root)
        files = [root] if not root.is_dir() else sorted(root.rglob("*"))
        for path in files:
            if any(part in _IGNORED for part in path.relative_to(root).parts):
                continue
            digest.update(str(path).encode())
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
            if path.is_file():
                digest.update(hashlib.sha256(path.read_bytes()).digest())
            elif not path.exists():
                digest.update(b"missing")
    return digest.hexdigest()


def _workspace_files(workspace):
    return {str(p.relative_to(workspace)): _fingerprint([p]) for p in sorted(workspace.rglob("*"))
            if (p.is_file() or p.is_symlink()) and not any(part in _IGNORED for part in p.relative_to(workspace).parts)}


def _execution_facts(agent, workspace, before_files, after_files):
    """Observations only: never infer progress from the assistant's prose."""
    pending, reads = {}, []
    for message in agent.messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls", []):
                function = call.get("function", {})
                if function.get("name") == "read_file":
                    try:
                        arguments = json.loads(function.get("arguments", "{}"))
                    except (ValueError, TypeError):
                        continue
                    if (isinstance(arguments, dict) and isinstance(arguments.get("file_path"), str)
                            and isinstance(call.get("id"), str) and call["id"]):
                        pending[call.get("id")] = arguments["file_path"]
        elif message.get("role") == "tool" and message.get("tool_call_id") in pending:
            requested = pending.pop(message["tool_call_id"])
            output = message.get("content")
            if not isinstance(output, str) or not (output == "(empty file)" or re.match(r"\d+\t", output)):
                continue
            try:
                path = Path(requested).expanduser()
                if path.is_absolute():
                    display = str(path.resolve().relative_to(workspace))
                else:
                    display = "relative request: " + requested
            except (OSError, ValueError, RuntimeError):
                continue  # unavailable/outside paths are not execution evidence
            if display in reads:
                reads.remove(display)
            reads.append(display)
    changed = sorted(p for p in before_files.keys() | after_files.keys() if before_files.get(p) != after_files.get(p))
    return {"agent_status": agent.last_run.status, "finish_reason": agent.last_run.finish_reason,
            "files_changed": bool(changed), "changed_file_count": len(changed),
            "changed_files": [p[:160] for p in changed[:8]],
            "successful_read_requests": [p[:160] for p in reads[-8:]],
            "read_evidence_scope": "non-error tool results visible in history; not proof of current file correctness"}


def _failure_excerpt(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    headers = [line for line in lines if line.startswith(("FAILED ", "FAIL: ", "ERROR: "))]
    details = [line for line in lines if "AssertionError" in line or line.startswith(("E ", "Error:"))]
    selected = headers[:1] + details[:1]
    if not selected:
        selected = [line for line in lines if not line.startswith("Exit code:")][-2:]
    return "\n".join(line[:240] for line in selected)[:600]


def _recovery_feedback(task, facts, verification):
    if verification["status"] != "failed":
        kind = "verification_unavailable"
        action = "Stop: verification is unavailable; do not guess a code fix from an environment error."
    elif verification.get("phase") == "regression":
        kind = "regression_failed"
        action = ("Task acceptance passed, but regression checks found new failures or lost test coverage. "
                  "Repair these failures without undoing the target fix. Do not modify tests, suppress collection, "
                  "or expand the frozen baseline failure list. Completion requires both checks to pass.")
    elif facts["agent_status"] == "output_limit" and not facts["files_changed"]:
        kind = "truncated_without_changes"
        action = ("The previous response hit the output limit and no file changes landed. Do not repeat a full analysis. "
                  "Use the available read evidence; next perform one minimal tool action that advances the repair, "
                  "such as a focused edit or a small reproduction. Do not claim completion without verification.")
    elif facts["files_changed"]:
        kind = "modified_verification_failed"
        action = ("Files changed but independent verification failed. Inspect the failing behavior of the current patch "
                  "and make one targeted correction; keep unrelated work and protected tests intact.")
    else:
        kind = "no_changes"
        action = ("No file changes landed and verification still fails. Move from investigation to one minimal tool action; "
                  "do not repeat a complete review or claim the repair is done.")
    # Length/no-change recovery does not benefit from repeating old assertion
    # stacks. Keep full verifier output on disk for diagnosis.
    excerpt = _failure_excerpt(verification.get("feedback", "")) if kind in {"modified_verification_failed", "no_changes", "regression_failed"} else ""
    observed = {key: facts[key] for key in ("agent_status", "finish_reason", "files_changed", "changed_file_count",
                                           "changed_files", "successful_read_requests")}
    observed["verification_status"] = verification["status"]
    observed["verification_exit_code"] = verification.get("exit_code")
    if verification.get("phase") == "regression":
        observed["new_regression_failures"] = [key[:200] for key in verification.get("new_failures", [])[:8]]
        observed["missing_or_skipped_tests"] = [key[:200] for key in verification.get("lost_tests", [])[:8]]
    message = (f"Original task: {task}\nRecovery stage: {kind}\n"
               f"Observed execution facts (read requests are not proof of current correctness): {json.dumps(observed)}\n"
               f"Next action: {action}\nDo not change acceptance criteria or protected tests.")
    if excerpt:
        message += "\nKey verification evidence:\n" + excerpt
    return {"kind": kind, "next_action": action, "failure_excerpt": excerpt, "message": message,
            "message_chars": len(message)}


class _BudgetModel:
    def __init__(self, model, check, reserve):
        self._model, self._check, self._reserve = model, check, reserve

    def __getattr__(self, name):
        return getattr(self._model, name)

    def chat(self, *args, **kwargs):
        self._check()
        self._reserve()
        return self._model.chat(*args, **kwargs)


class RepairController:
    def __init__(self, agent, workspace, verification, result_dir, limits=None, regression=None):
        self.agent = agent
        self.workspace = Path(workspace).resolve()
        self.verification = verification
        self.result_dir = Path(result_dir).resolve()
        self.limits = limits or RepairLimits()
        self.regression = regression
        if not self.workspace.is_dir():
            raise ValueError("workspace must exist")
        if self.result_dir == self.workspace or self.workspace in self.result_dir.parents:
            raise ValueError("result_dir must be outside workspace")
        self.checker = VerificationRunner(self.workspace, verification, self.result_dir, regression,
                                          executor=lambda *args: self._run_check(*args))

    def run(self, task):
        # A controller/run is deliberately single-use; never overwrite evidence.
        self.result_dir.mkdir(parents=True, exist_ok=False)
        result = RepairResult()
        if self.regression is not None:
            result.completion_scope = "acceptance_and_regression"
            result.regression_status = "not_run"
        started = time.monotonic()
        deadline = started + self.limits.max_seconds
        protected_paths = self.verification.protected_paths + (self.regression.protected_paths if self.regression else ())
        if self.regression:
            protected_paths += ("pyproject.toml", "pytest.ini", "conftest.py", "setup.cfg", "tox.ini")
        protected = [self.workspace / p for p in dict.fromkeys(protected_paths)]
        trusted_hash = _fingerprint(protected)
        old_model, old_rounds = self.agent.llm, self.agent.max_rounds
        old_check, old_cwd = self.agent.execution_check, get_tracked_cwd()
        old_log = self.agent.event_log_path
        lock = threading.Lock()

        def check():
            if time.monotonic() >= deadline:
                raise BudgetExceeded("time_limit")
            if old_check is not None:
                old_check()

        def reserve():
            with lock:
                if result.model_calls >= self.limits.max_model_calls:
                    raise BudgetExceeded("model_call_limit")
                result.model_calls += 1

        def save():
            result.elapsed_seconds = round(time.monotonic() - started, 3)
            data = {"workspace": str(self.workspace), "limits": asdict(self.limits),
                    "verification": asdict(self.verification),
                    "regression": asdict(self.regression) if self.regression else None,
                    "agent_log": str(self.result_dir / "agent-events.jsonl"),
                    **asdict(result)}
            temp = self.result_dir / "result.tmp"
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.result_dir / "result.json")

        self.agent.llm = _BudgetModel(old_model, check, reserve)
        self.agent.execution_check = check
        self.agent.event_log_path = self.result_dir / "agent-events.jsonl"
        self.agent.max_rounds = self.limits.rounds_per_attempt
        set_tracked_cwd(str(self.workspace))
        prompt = (f"Workspace: {self.workspace}. Use absolute paths for file tools.\n"
                  f"Do not change protected verification inputs (including adding files there): {protected}.\n{task}")
        if self.regression:
            prompt += "\nTask acceptance alone is not completion. Preserve baseline behavior; both acceptance and regression gates must pass."
        stagnant, previous_failure = 0, None
        try:
            if self.regression is not None:
                before = _fingerprint([self.workspace])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result.status = "time_limit"
                    return result
                baseline = self._regress("baseline", min(remaining, self.limits.verifier_seconds))
                result.regression_baseline = baseline
                if _fingerprint(protected) != trusted_hash:
                    result.status = "protected_inputs_changed"
                    return result
                if _fingerprint([self.workspace]) != before:
                    result.status = "workspace_changed_during_verification"
                    return result
                if time.monotonic() >= deadline:
                    result.status = "time_limit"
                    return result
                if baseline["status"] != "passed":
                    result.regression_status = baseline["status"]
                    result.status = "regression_baseline_" + baseline["status"]
                    return result
                save()  # freeze the pre-edit failure set before any model call
            for number in range(1, self.limits.max_attempts + 1):
                if time.monotonic() >= deadline:
                    result.status = "time_limit"
                    break
                if result.model_calls >= self.limits.max_model_calls:
                    result.status = "model_call_limit"
                    break
                before = _fingerprint([self.workspace])
                before_files = _workspace_files(self.workspace)
                attempt = {"number": number, "agent_status": "running", "before": before,
                           "model_calls_before": result.model_calls,
                           "regression": {"status": "not_run" if self.regression else "not_configured"}}
                result.acceptance_status = "not_run"
                result.regression_status = "not_run" if self.regression else "not_configured"
                result.attempts.append(attempt)
                stop = None
                if number > 1:
                    result.attempts[-2]["feedback_submitted_to_agent"] = True
                try:
                    self.agent.chat(prompt)
                    attempt["agent_status"] = self.agent.last_run.status
                except BudgetExceeded as exc:
                    stop = str(exc)
                    attempt["agent_status"] = stop
                except KeyboardInterrupt:
                    result.status = "interrupted"
                    attempt["agent_status"] = "interrupted"
                    break
                except Exception as exc:  # noqa: BLE001 - isolate agent failures, then verify existing files
                    stop = "agent_error"
                    attempt.update(agent_status="error", error_type=type(exc).__name__)
                attempt["agent_run"] = asdict(self.agent.last_run)
                attempt["model_calls_after"] = result.model_calls
                after = _fingerprint([self.workspace])
                attempt["after"] = after
                attempt["facts"] = _execution_facts(self.agent, self.workspace, before_files, _workspace_files(self.workspace))
                attempt["facts"]["workspace_fingerprint"] = after
                if _fingerprint(protected) != trusted_hash:
                    result.status = "protected_inputs_changed"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result.status = "time_limit"
                    break
                verification = self._verify(number, min(remaining, self.limits.verifier_seconds))
                attempt["verification"] = verification
                result.acceptance_status = verification["status"]
                attempt["facts"]["verification_status"] = verification["status"]
                attempt["facts"]["verification_exit_code"] = verification.get("exit_code")
                attempt["facts"]["verification_input_fingerprint"] = after
                if _fingerprint(protected) != trusted_hash:
                    result.status = "protected_inputs_changed"
                    break
                if _fingerprint([self.workspace]) != after:
                    result.status = "workspace_changed_during_verification"
                    break
                if time.monotonic() >= deadline:
                    result.status = "time_limit"
                    break
                if verification["status"] == "passed":
                    if self.regression is None:
                        # Legacy acceptance-only callers retain their status API;
                        # completed remains false and scope is explicitly limited.
                        result.status = "passed"
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        result.status = "time_limit"
                        break
                    regression = self._regress(number, min(remaining, self.limits.verifier_seconds),
                                               result.regression_baseline)
                    attempt["regression"] = regression
                    result.regression_status = regression["status"]
                    if _fingerprint(protected) != trusted_hash:
                        result.status = "protected_inputs_changed"
                        break
                    if _fingerprint([self.workspace]) != after:
                        result.status = "workspace_changed_during_verification"
                        break
                    if time.monotonic() >= deadline:
                        result.status = "time_limit"
                        break
                    if regression["status"] == "passed":
                        result.status, result.completed = "passed", True
                        break
                    verification = regression
                attempt["failed_phase"] = verification.get("phase", "acceptance")
                attempt["facts"]["failed_phase"] = attempt["failed_phase"]
                attempt["recovery"] = _recovery_feedback(task, attempt["facts"], verification)
                attempt["feedback_submitted_to_agent"] = False
                if verification["status"] != "failed":
                    prefix = "regression_" if attempt["failed_phase"] == "regression" else "verification_"
                    result.status = prefix + verification["status"]
                    break
                if stop:
                    result.status = stop
                    break
                signature = (after, attempt["failed_phase"], verification["exit_code"], verification["status"])
                stagnant = stagnant + 1 if after == before and (previous_failure is None or signature == previous_failure) else 0
                previous_failure = signature
                if stagnant >= self.limits.max_stagnant_attempts:
                    result.status = "no_progress"
                    break
                prompt = attempt["recovery"]["message"]
                save()
            else:
                result.status = "attempt_limit"
        except KeyboardInterrupt:
            result.status = "interrupted"
        except Exception as exc:  # noqa: BLE001 - persist a terminal outcome before restoring caller state
            result.status = "controller_error"
            result.error_type = type(exc).__name__
            if result.attempts:
                result.attempts[-1]["controller_error_type"] = type(exc).__name__
        finally:
            self.agent.llm, self.agent.max_rounds = old_model, old_rounds
            self.agent.execution_check = old_check
            self.agent.event_log_path = old_log
            set_tracked_cwd(old_cwd)
            save()
        return result

    def _verify(self, number, timeout):
        """Compatibility adapter; all local checking lives in VerificationRunner."""
        return self.checker.verify(number, timeout)

    def _regress(self, label, timeout, baseline=None):
        return self.checker.regress(label, timeout, baseline)

    def _run_check(self, argv, output, timeout, failure_codes):
        return self.checker.run_check(argv, output, timeout, failure_codes)
