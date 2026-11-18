"""Run (or resume) one SWE-bench instance through CoreCoder's durable RepairTask.

Fresh run:  reproduce first (same as run_instance.py), then RepairTask.create(...).run().
--resume:   a NEW process rebuilds the identical agent and calls RepairTask(agent, dir).run();
            the checkpoint decides what may continue (budget, history, phase, pending side effects).
The container keeps the same name and bind mounts; on resume it is restarted so no orphaned
command from the killed process can keep touching the workspace.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from run_instance import (ALLOWED_TOOLS, FIX_TASK, HERE, REPRO_DIR, REPRO_TASK, TASK, Budget, Trace,
                          build_llm, docker_argv, extract_patch, route_shell_into, sh, valid_targets)


def ensure_container(container, image, ws, root, resume):
    if resume:
        if subprocess.run(["docker", "restart", "-t", "0", container], capture_output=True).returncode == 0:
            return "restarted"
    sh(["docker", "run", "-d", "--platform", "linux/amd64", "--network", "none", "--name", container,
        "-v", f"{ws}:/testbed", "-v", f"{root}:{root}", image, "sleep", "infinity"])
    return "started"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="improved_v2")
    ap.add_argument("--instance-id", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--interrupted-tools-terminated", action="store_true",
                    help="resume promise: the container was restarted, so no earlier command is still running")
    ap.add_argument("--max-calls", type=int, default=40)
    ap.add_argument("--max-seconds", type=int, default=1500)
    ap.add_argument("--verifier-seconds", type=int, default=240)
    a = ap.parse_args()

    inst = next(json.loads(l) for l in open(HERE / "data/candidates.jsonl") if json.loads(l)["instance_id"] == a.instance_id)
    root = HERE / "runs" / a.run_id / a.arm / a.instance_id
    ws, out = root / "workspace", root / "out"
    task_dir = out / "task"
    image = f"sweb.eval.x86_64.{a.instance_id.lower()}:latest"
    container = f"cc-{a.run_id}-{a.arm}-{a.instance_id}".lower().replace("__", "-")
    if a.resume:
        if not (task_dir / "state.json").exists():
            raise SystemExit("nothing to resume: no task checkpoint")
    else:
        if root.exists():
            raise SystemExit(f"{root} exists")
        out.mkdir(parents=True)
        tmp = sh(["docker", "create", "--platform", "linux/amd64", image]).stdout.strip()
        try:
            sh(["docker", "cp", f"{tmp}:/testbed", str(ws)])
        finally:
            subprocess.run(["docker", "rm", "-f", tmp], capture_output=True)
    trace = Trace(out / "trace.jsonl")
    segment = {"process": "resume" if a.resume else "fresh", "pid": os.getpid(),
               "container": ensure_container(container, image, ws, root, a.resume)}
    trace(kind="process_start", **segment)

    sys.path.insert(0, str(HERE / "arms" / a.arm))
    os.chdir(ws)
    from corecoder.agent import Agent
    from corecoder.permissions import Permission
    from corecoder.repair import RegressionSpec, RepairLimits, VerificationSpec
    from corecoder.tasks import RepairTask
    from corecoder.tools import ALL_TOOLS
    from corecoder.tools.bash import set_tracked_cwd
    set_tracked_cwd(str(ws))
    route_shell_into(container, trace)
    started = time.monotonic()
    # The durable task ledger owns the budget; this outer cap is only a runaway guard.
    model, raw_llm = build_llm(trace, a.max_calls + 20, started + 3 * a.max_seconds)
    tools = [t for t in ALL_TOOLS if t.name in ALLOWED_TOOLS]
    agent = Agent(model, tools=tools, max_rounds=a.max_calls, max_context_tokens=128_000,
                  permission=Permission(allow_all=True))
    check = docker_argv(container, str(ws), f"timeout -s KILL 120 python -m pytest -x -q -p no:cacheprovider {REPRO_DIR}/test_repro.py")

    try:
        if not a.resume:
            task = TASK.format(problem=inst["problem_statement"], ws=ws)
            agent.messages = []
            first = task + "\n\n" + REPRO_TASK.format(ws=ws, repro=REPRO_DIR)
            agent.max_rounds = 12
            try:
                info = reproduce_first(agent, first, ws, check, trace)
            except Budget as e:
                info = {"repro_status": "budget", "stop": str(e)}
            targets_file = ws / REPRO_DIR / "regression_targets.txt"
            targets = valid_targets(ws, targets_file.read_text() if targets_file.is_file() else "")
            info.update(regression_targets=targets, phase0_calls=model.calls)
            (out / "phase0.json").write_text(json.dumps(info, indent=2))
            if info["repro_status"] != "reproduced":
                segment["stop"] = "no_durable_task:" + info["repro_status"]
                return
            # Durable tasks require a regression gate; with no valid targets, use the repro file
            # itself (its pre-edit failure is the frozen baseline, so it can never be "new").
            reg_files = targets or [f"{REPRO_DIR}/test_repro.py"]
            regression = RegressionSpec(docker_argv(container, str(ws),
                f"timeout -s KILL {a.verifier_seconds - 10} python -m pytest -q -p no:cacheprovider "
                + " ".join(reg_files) + ' "$@"'), protected_paths=tuple(reg_files))
            spec = VerificationSpec(check, protected_paths=(REPRO_DIR,), failure_codes=(1,))
            remaining = a.max_calls - model.calls
            limits = RepairLimits(max_attempts=3, max_model_calls=max(1, remaining),
                                  rounds_per_attempt=max(5, remaining // 2),
                                  max_seconds=max(60, a.max_seconds - (time.monotonic() - started)),
                                  verifier_seconds=a.verifier_seconds, max_stagnant_attempts=2)
            RepairTask.create(agent, task_dir, FIX_TASK.format(repro=REPRO_DIR), ws, spec, regression, limits)
            trace(kind="task_created")
        kw = {"interrupted_tools_terminated": True} if a.resume and a.interrupted_tools_terminated else {}
        state = RepairTask(agent, task_dir, **kw).run()
        segment.update(stop=state["status"], phase=state["phase"], completed=state["completed"],
                       ledger=state["ledger"])
    except Exception as e:  # noqa: BLE001
        segment.update(stop="harness_error", error=f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        segment.update(seconds=round(time.monotonic() - started, 1), process_model_calls=model.calls,
                       process_prompt_tokens=raw_llm.total_prompt_tokens,
                       process_completion_tokens=raw_llm.total_completion_tokens)
        trace(kind="process_end", **segment)
        segs_file = out / "segments.json"
        segs = json.loads(segs_file.read_text()) if segs_file.exists() else []
        segs.append(segment)
        segs_file.write_text(json.dumps(segs, indent=2))
        patch = extract_patch(ws)
        (out / "patch.diff").write_text(patch)
        (out / "prediction.json").write_text(json.dumps(
            {"instance_id": a.instance_id, "model_name_or_path": f"corecoder-{a.arm}-durable", "model_patch": patch}))
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        print(json.dumps(segment))


def reproduce_first(agent, first_prompt, ws, check, trace):
    repro = ws / REPRO_DIR / "test_repro.py"
    prompt = first_prompt
    status = "not_created"
    for attempt in (1, 2):
        agent.chat(prompt)
        if not repro.is_file():
            prompt = f"{REPRO_DIR}/test_repro.py does not exist yet. Create it as described above."
            continue
        r = subprocess.run(list(check), capture_output=True, text=True, timeout=180)
        trace(kind="repro_check", attempt=attempt, exit_code=r.returncode, output_tail=r.stdout[-1500:])
        if r.returncode == 1:
            return {"repro_status": "reproduced", "repro_attempts": attempt}
        status = "invalid"
        prompt = (f"The reproduction test is not valid (pytest exit code {r.returncode}; it must FAIL on the "
                  f"unfixed code).\nOutput tail:\n{r.stdout[-1500:]}\nRewrite {REPRO_DIR}/test_repro.py.")
    return {"repro_status": status}


if __name__ == "__main__":
    main()
