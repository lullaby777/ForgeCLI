"""Checkpoint fault-injection experiment.

For each instance, two independent durable runs:
  nofault : run to the end (control; also the cost of a "restart from scratch")
  kill    : after the durable task starts, wait for K more model responses (K ~ U[1,6]) plus a
            random delay (U[0,8]s), SIGKILL the whole process group (no cleanup code runs),
            snapshot what the checkpoint says it was doing, then resume in a NEW process.
Kill points are seeded per instance, so the experiment is reproducible.

python fault_inject.py --run-id f1 [--ids ...] [--workers 3] [--seed 0]
"""
import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARM = "improved_v2"  # overridden by --arm
# solved by at least one arm in r2/r3; never-solved tasks say nothing about recovery
DEFAULT_IDS = """pallets__flask-4992 pytest-dev__pytest-11143 pytest-dev__pytest-5103 pytest-dev__pytest-5221
pytest-dev__pytest-5227 pytest-dev__pytest-5495 pytest-dev__pytest-5692 pytest-dev__pytest-6116 pytest-dev__pytest-7168
pytest-dev__pytest-7373 pytest-dev__pytest-7432 pytest-dev__pytest-7490 pytest-dev__pytest-8365 pytest-dev__pytest-8906
pytest-dev__pytest-9359""".split()


def events(trace):
    if not trace.exists():
        return []
    out = []
    for line in trace.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # a SIGKILL can cut the last line
    return out


def pending_kind(task_dir):
    from corecoder.task_store import read_record
    try:
        return (read_record(task_dir / "state.json")["pending"] or {}).get("kind")
    except Exception:  # noqa: BLE001 - file mid-replace or not yet created
        return None


def checkpoint_view(task_dir):
    sys.path.insert(0, str(HERE / "arms" / ARM))
    from corecoder.task_store import read_record
    try:
        s = read_record(task_dir / "state.json")
        return {"phase": s["phase"], "pending": (s["pending"] or {}).get("kind"), "ledger_calls": s["ledger"]["model_calls"],
                "attempts": s["ledger"]["attempts"], "status": s["status"]}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def durable(run_id, iid, resume=False, log_suffix=""):
    cmd = [sys.executable, str(HERE / "durable_instance.py"), "--arm", ARM, "--instance-id", iid, "--run-id", run_id]
    if resume:
        cmd.append("--resume")
        if ARM == "improved_v3":
            cmd.append("--interrupted-tools-terminated")
    log = HERE / "runs" / run_id / f"{iid}{log_suffix}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    return cmd, log


def run_nofault(run_id, iid):
    cmd, log = durable(f"{run_id}-nofault", iid)
    if (HERE / "runs" / f"{run_id}-nofault" / ARM / iid).exists():
        return {"iid": iid, "mode": "nofault", "skipped": True}
    with log.open("w") as f:
        code = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=HERE)
    return {"iid": iid, "mode": "nofault", "exit": code}


def run_kill(run_id, iid, seed, target="random"):
    rid = f"{run_id}-kill"
    root = HERE / "runs" / rid / ARM / iid
    if root.exists():
        return {"iid": iid, "mode": "kill", "skipped": True}
    rng = random.Random(f"{seed}:{iid}")
    k, delay = rng.randint(1, 6), round(rng.uniform(0, 8), 2)
    rec = {"iid": iid, "mode": "kill", "target": target, "k_after_task": k,
           "delay_s": delay if target == "random" else 0}
    cmd, log = durable(rid, iid)
    trace = root / "out" / "trace.jsonl"
    with log.open("w") as f:
        p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=HERE, start_new_session=True)
        t0 = time.monotonic()
        while p.poll() is None:
            ev = events(trace)
            idx = next((i for i, e in enumerate(ev) if e["kind"] == "task_created"), None)
            if idx is not None and sum(e["kind"] == "llm_response" for e in ev[idx:]) >= k:
                if target == "tools":  # wait until the checkpoint says a tool batch is running
                    while p.poll() is None and pending_kind(root / "out" / "task") != "tools":
                        time.sleep(0.05)
                else:
                    time.sleep(delay)
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()
                    rec.update(killed=True, killed_at_s=round(time.monotonic() - t0, 1))
                break
            time.sleep(0.5)
        p.wait()
    if not rec.get("killed"):
        rec.update(killed=False, note="process finished (or never created a durable task) before the kill point")
        return rec
    ev = events(trace)
    last = next((e for e in reversed(ev) if e["kind"] in ("llm_request", "llm_response", "shell", "repro_check")), {})
    rec["last_trace_event"] = last.get("kind")
    rec["checkpoint_at_kill"] = checkpoint_view(root / "out" / "task")
    cmd, log = durable(rid, iid, resume=True, log_suffix=".resume")
    t1 = time.monotonic()
    with log.open("w") as f:
        rec["resume_exit"] = subprocess.call(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=HERE)
    rec["resume_seconds"] = round(time.monotonic() - t1, 1)
    segs = json.loads((root / "out" / "segments.json").read_text()) if (root / "out" / "segments.json").exists() else []
    rec["resume_stop"] = segs[-1].get("stop") if segs else None
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--ids", nargs="+", default=DEFAULT_IDS)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-eval", action="store_true")
    ap.add_argument("--arm", default="improved_v2")
    ap.add_argument("--target", choices=("random", "tools"), default="random",
                    help="tools = kill exactly while a tool batch is executing")
    ap.add_argument("--modes", nargs="+", default=["nofault", "kill"])
    a = ap.parse_args()
    global ARM
    ARM = a.arm
    sys.path.insert(0, str(HERE / "arms" / ARM))
    jobs = [(m, i) for m in a.modes for i in a.ids]
    results, t0 = [], time.time()
    with ThreadPoolExecutor(a.workers) as pool:
        futs = [pool.submit(run_nofault, a.run_id, i) if m == "nofault" else pool.submit(run_kill, a.run_id, i, a.seed, a.target)
                for m, i in jobs]
        for n, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            print(f"[{n}/{len(jobs)}] {time.time() - t0:6.0f}s {r['mode']:7s} {r['iid']}: "
                  + json.dumps({k: v for k, v in r.items() if k not in ('iid', 'mode')})[:220], flush=True)
    out = HERE / "runs" / f"{a.run_id}-kill" / "fault_log.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(out.read_text()) if out.exists() else []
    out.write_text(json.dumps(old + [r for r in results if not r.get("skipped")], indent=2))
    if a.no_eval:
        return
    for mode in a.modes:
        rid = f"{a.run_id}-{mode}"
        rows = [json.loads(p.read_text()) for p in sorted((HERE / "runs" / rid / ARM).glob("*/out/prediction.json"))]
        preds = HERE / "runs" / rid / "predictions.jsonl"
        preds.write_text("".join(json.dumps(r) + "\n" for r in rows))
        subprocess.call([sys.executable, "-m", "swebench.harness.run_evaluation", "--dataset_name",
                         "princeton-nlp/SWE-bench_Lite", "--split", "test", "--predictions_path", str(preds),
                         "--max_workers", "4", "--namespace", "", "--cache_level", "instance",
                         "--run_id", rid, "--report_dir", str(HERE / "runs" / rid)], cwd=HERE)
    print(f"\nDone. Next: python fault_report.py --run-id {a.run_id}")


if __name__ == "__main__":
    main()
