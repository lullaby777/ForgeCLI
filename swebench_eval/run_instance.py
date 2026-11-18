"""Run one CoreCoder arm on one SWE-bench instance inside its Docker environment.

Design
- The agent process runs on the host; every shell command it issues is routed
  into the instance container with `docker exec` (file tools edit the host copy,
  which is bind-mounted into the container at /testbed AND at its own host path,
  so absolute paths mean the same thing on both sides).
- Both arms get the same issue text, tools, model, call budget and wall-clock budget.
- Hidden SWE-bench tests are never visible: scoring happens later with the official harness.
- Every model call and shell command is appended to trace.jsonl (replayable evidence).

Usage: python run_instance.py --arm original|improved --instance-id ID --run-id NAME
"""
import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ALLOWED_TOOLS = {"bash", "read_file", "write_file", "edit_file", "glob", "grep", "todo_write"}
ACTIVATE = "source /opt/miniconda3/bin/activate && conda activate testbed"
REPRO_DIR = "swe_repro"

TASK = """<issue>
{problem}
</issue>

The repository is at {ws}. It is checked out at the commit where this issue was reported and the
package is installed in editable mode. Shell commands run inside the project's own environment.

Resolve the issue by editing non-test source files. Do not modify or delete existing tests.
Keep the change minimal and focused. When you are done, reply with a short summary of the fix."""

REPRO_TASK = """Before fixing anything, reproduce the issue.
1. Create {ws}/{repro}/test_repro.py: a small pytest test file that FAILS on the current code
   because of this issue and should PASS once the issue is correctly fixed.
2. Create {ws}/{repro}/regression_targets.txt listing up to 3 EXISTING test files (paths relative
   to the repository root, one per line) that cover the code you expect to change.
Do not fix the issue yet. Reply "ready" when both files exist."""

FIX_TASK = """Now fix the issue in non-test source files.
Acceptance: the frozen reproduction test {repro}/test_repro.py must pass (you may not edit it).
Regression: the existing tests listed in {repro}/regression_targets.txt must not get new failures."""


def sh(argv, **kw):
    return subprocess.run(argv, check=True, capture_output=True, text=True, **kw)


class Trace:
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()

    def __call__(self, **event):
        event["t"] = round(time.time(), 3)
        with self.lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


class Budget(RuntimeError):
    pass


def build_llm(trace, max_calls, deadline):
    from corecoder.llm import LLM

    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("CORECODER_API_KEY")
    if not key:
        raise SystemExit("Set DEEPSEEK_API_KEY first")
    llm = LLM(model=os.environ.get("SWE_MODEL", "deepseek-flash"), api_key=key,
              base_url=os.environ.get("SWE_BASE_URL", "https://api.deepseek.com"),
              temperature=0.0, max_tokens=8192)

    class Counted:
        calls = 0

        def __getattr__(self, name):
            return getattr(llm, name)

        def chat(self, messages, **kw):
            if Counted.calls >= max_calls:
                raise Budget("model_call_limit")
            if time.monotonic() >= deadline:
                raise Budget("time_limit")
            Counted.calls += 1
            idx, started = Counted.calls, time.monotonic()
            before = (llm.total_prompt_tokens, llm.total_completion_tokens)
            trace(kind="llm_request", index=idx, n_messages=len(messages),
                  last_message=messages[-1] if messages else None,
                  request_hash=hashlib.sha256(json.dumps(messages, sort_keys=True, default=str).encode()).hexdigest())
            resp = llm.chat(messages, **kw)
            trace(kind="llm_response", index=idx, seconds=round(time.monotonic() - started, 2),
                  prompt_tokens=llm.total_prompt_tokens - before[0],
                  completion_tokens=llm.total_completion_tokens - before[1],
                  finish_reason=getattr(resp, "finish_reason", None), content=getattr(resp, "content", None),
                  tool_calls=[{"name": c.name, "arguments": c.arguments} for c in (getattr(resp, "tool_calls", None) or [])])
            return resp

    return Counted(), llm


def route_shell_into(container, trace):
    """Replace the bash tool's process spawner with `docker exec` into the instance container."""
    import corecoder.tools.bash as bash_mod

    def docker_run_shell(command, check=False, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=120, cwd=None, **_):
        inner = f"{ACTIVATE} && timeout -s KILL {int(timeout)} bash -c {shlex.quote(command)}"
        argv = ["docker", "exec", "-w", cwd or "/testbed", container, "bash", "-c", inner]
        started = time.monotonic()
        proc = subprocess.run(argv, check=False, capture_output=capture_output, text=text, encoding=encoding,
                              errors=errors, timeout=timeout + 15)
        trace(kind="shell", command=command, cwd=cwd, exit_code=proc.returncode,
              seconds=round(time.monotonic() - started, 2), output_tail=(proc.stdout or "")[-1500:])
        return proc

    bash_mod.run_shell = docker_run_shell


def docker_argv(container, cwd, script):
    """Trusted verifier command: runs inside the container; extra args land in "$@"."""
    # `source activate` would swallow "$@" (it forwards positional args), so park them first.
    body = f'ARGS=("$@"); set --; {ACTIVATE} && set -- "${{ARGS[@]}}" && {script}'
    return ("docker", "exec", "-w", cwd, container, "bash", "-c", body, "bash")


def valid_targets(ws, text):
    out = []
    for line in text.splitlines():
        rel = line.strip().lstrip("./")
        if not rel or rel.startswith(REPRO_DIR) or ".." in Path(rel).parts or Path(rel).is_absolute():
            continue
        name = Path(rel).name
        if not (ws / rel).is_file() or not (name.startswith("test") and name.endswith(".py")):
            continue
        tracked = subprocess.run(["git", "-C", str(ws), "ls-files", "--error-unmatch", rel], capture_output=True)
        if tracked.returncode == 0 and rel not in out:
            out.append(rel)
    return out[:3]


def extract_patch(ws):
    git = ["git", "-C", str(ws), "-c", "core.fileMode=false"]
    subprocess.run(git + ["add", "-A", "--", ".", f":(exclude){REPRO_DIR}"], check=True, capture_output=True)
    return subprocess.run(git + ["diff", "--cached", "HEAD"], check=True, capture_output=True, text=True).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=("original", "improved", "improved_v2"), required=True)
    ap.add_argument("--instance-id", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--max-calls", type=int, default=40)
    ap.add_argument("--max-seconds", type=int, default=1500)
    ap.add_argument("--verifier-seconds", type=int, default=240)
    a = ap.parse_args()

    inst = next(json.loads(l) for l in open(HERE / "data/candidates.jsonl") if json.loads(l)["instance_id"] == a.instance_id)
    root = HERE / "runs" / a.run_id / a.arm / a.instance_id
    if root.exists():
        raise SystemExit(f"{root} exists; use a new --run-id or delete it")
    ws, out = root / "workspace", root / "out"
    out.mkdir(parents=True)
    trace = Trace(out / "trace.jsonl")
    image = f"sweb.eval.x86_64.{a.instance_id.lower()}:latest"
    container = f"cc-{a.run_id}-{a.arm}-{a.instance_id}".lower().replace("__", "-")

    # 1. copy the pristine /testbed out of the image, then mount it back (two paths, same files)
    tmp = sh(["docker", "create", "--platform", "linux/amd64", image]).stdout.strip()
    try:
        sh(["docker", "cp", f"{tmp}:/testbed", str(ws)])
    finally:
        subprocess.run(["docker", "rm", "-f", tmp], capture_output=True)
    # --network none: the agent must not download newer releases (= the upstream fix) or browse.
    sh(["docker", "run", "-d", "--platform", "linux/amd64", "--network", "none", "--name", container,
        "-v", f"{ws}:/testbed", "-v", f"{root}:{root}", image, "sleep", "infinity"])

    # 2. import the frozen arm and wire it up
    sys.path.insert(0, str(HERE / "arms" / a.arm))
    os.chdir(ws)
    from corecoder.agent import Agent
    from corecoder.permissions import Permission
    from corecoder.tools import ALL_TOOLS
    try:
        from corecoder.tools.bash import set_tracked_cwd
        set_tracked_cwd(str(ws))
    except ImportError:
        pass
    route_shell_into(container, trace)
    started = time.monotonic()
    deadline = started + a.max_seconds
    model, raw_llm = build_llm(trace, a.max_calls, deadline)
    tools = [t for t in ALL_TOOLS if t.name in ALLOWED_TOOLS]
    agent = Agent(model, tools=tools, max_rounds=a.max_calls, max_context_tokens=128_000,
                  permission=Permission(allow_all=True))
    task = TASK.format(problem=inst["problem_statement"], ws=ws)
    metrics = {"instance_id": a.instance_id, "arm": a.arm, "run_id": a.run_id,
               "model": os.environ.get("SWE_MODEL", "deepseek-flash"),
               "max_calls": a.max_calls, "max_seconds": a.max_seconds}
    trace(kind="start", **metrics)

    try:
        if a.arm == "original":
            try:
                agent.chat(task)
                metrics["stop"] = getattr(getattr(agent, "last_run", None), "status", None) or "agent_returned"
            except Budget as e:
                metrics["stop"] = str(e)
        else:
            metrics.update(run_improved(agent, task, ws, root, out, container, a, deadline, trace))
    except Exception as e:  # noqa: BLE001 - always record and still collect whatever patch exists
        metrics["stop"] = "harness_error"
        metrics["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    finally:
        metrics.update(model_calls=model.calls, prompt_tokens=raw_llm.total_prompt_tokens,
                       completion_tokens=raw_llm.total_completion_tokens,
                       seconds=round(time.monotonic() - started, 1))
        try:
            patch = extract_patch(ws)
        except Exception as e:  # noqa: BLE001
            patch, metrics["patch_error"] = "", str(e)[:300]
        metrics["patch_lines"] = patch.count("\n")
        (out / "patch.diff").write_text(patch)
        (out / "prediction.json").write_text(json.dumps(
            {"instance_id": a.instance_id, "model_name_or_path": f"corecoder-{a.arm}", "model_patch": patch}))
        (out / "metrics.json").write_text(json.dumps(metrics, indent=2))
        (out / "messages.json").write_text(json.dumps(agent.messages, ensure_ascii=False, default=str))
        trace(kind="end", **metrics)
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        print(json.dumps(metrics))


def run_improved(agent, task, ws, root, out, container, a, deadline, trace):
    """Reproduce first (frozen test), then hand off to the repair controller."""
    from corecoder.repair import RegressionSpec, RepairController, RepairLimits, VerificationSpec

    info = {"repro_status": "not_attempted"}
    repro = ws / REPRO_DIR / "test_repro.py"
    check = docker_argv(container, str(ws), f"timeout -s KILL 120 python -m pytest -x -q -p no:cacheprovider {REPRO_DIR}/test_repro.py")
    prompt = task + "\n\n" + REPRO_TASK.format(ws=ws, repro=REPRO_DIR)
    agent.max_rounds = 12
    for attempt in (1, 2):
        try:
            agent.chat(prompt)
        except Budget as e:
            info.update(stop=str(e), repro_status="budget")
            return info
        if not repro.is_file():
            prompt = f"{REPRO_DIR}/test_repro.py does not exist yet. Create it as described above."
            continue
        r = subprocess.run(list(check), capture_output=True, text=True, timeout=180)
        trace(kind="repro_check", attempt=attempt, exit_code=r.returncode, output_tail=r.stdout[-1500:])
        if r.returncode == 1:
            info["repro_status"] = "reproduced"
            break
        why = "it PASSES on the unfixed code, so it does not reproduce the issue" if r.returncode == 0 \
            else f"pytest exited with code {r.returncode} (collection/usage error)"
        prompt = f"The reproduction test is not valid: {why}.\nOutput tail:\n{r.stdout[-1500:]}\nRewrite {REPRO_DIR}/test_repro.py."
        info["repro_status"] = "invalid"
    info["repro_attempts"] = attempt

    targets_file = ws / REPRO_DIR / "regression_targets.txt"
    targets = valid_targets(ws, targets_file.read_text() if targets_file.is_file() else "")
    info["regression_targets"] = targets
    remaining_calls = a.max_calls - agent.llm.calls
    remaining_secs = deadline - time.monotonic()
    if remaining_calls < 2 or remaining_secs < 60:
        info["stop"] = "budget_after_repro"
        return info

    if info["repro_status"] != "reproduced":
        # Graceful degradation: no trusted acceptance check, so behave like the plain loop.
        agent.max_rounds = remaining_calls
        try:
            agent.chat(FIX_TASK.format(repro=REPRO_DIR).split("\n")[0])
            info["stop"] = "unverified_fix_returned"
        except Budget as e:
            info["stop"] = str(e)
        return info

    spec = VerificationSpec(check, protected_paths=(REPRO_DIR,), failure_codes=(1,))
    regression = None
    if targets:
        regression = RegressionSpec(docker_argv(container, str(ws),
                                                f"timeout -s KILL {a.verifier_seconds - 10} python -m pytest -q -p no:cacheprovider "
                                                + " ".join(targets) + ' "$@"'),
                                    protected_paths=tuple(targets))
    extra = {"max_protected_violations": 1} if a.arm == "improved_v2" else {}
    limits = RepairLimits(**extra, max_attempts=3, max_model_calls=remaining_calls, rounds_per_attempt=max(5, remaining_calls // 2),
                          max_seconds=remaining_secs, verifier_seconds=a.verifier_seconds, max_stagnant_attempts=2)
    result = RepairController(agent, ws, spec, out / "controller", limits, regression).run(FIX_TASK.format(repro=REPRO_DIR))
    info.update(stop=result.status, controller_attempts=len(result.attempts), acceptance=result.acceptance_status,
                regression=result.regression_status, completed=result.completed)
    return info


if __name__ == "__main__":
    main()
