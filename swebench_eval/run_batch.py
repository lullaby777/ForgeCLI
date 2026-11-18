"""Run both arms over the selected instances, then score with the official SWE-bench harness.

python run_batch.py --run-id r1 [--arms original improved] [--ids ID ...] [--workers 3]
Re-running with the same --run-id skips instances that already have a prediction.
"""
import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent


def default_ids():
    f = HERE / "data/selected_ids.txt"
    if not f.exists():
        raise SystemExit("data/selected_ids.txt missing: run `python filter_gold.py` after the gold check")
    return f.read_text().split()


def run_one(run_id, arm, iid, extra):
    out = HERE / "runs" / run_id / arm / iid / "out"
    if (out / "prediction.json").exists():
        return arm, iid, "skipped (done)"
    if out.parent.exists():
        return arm, iid, "skipped (partial run dir exists; delete it to retry)"
    log = HERE / "runs" / run_id / f"{arm}__{iid}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as f:
        code = subprocess.call([sys.executable, str(HERE / "run_instance.py"), "--arm", arm,
                                "--instance-id", iid, "--run-id", run_id, *extra], stdout=f, stderr=subprocess.STDOUT)
    return arm, iid, f"exit {code}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--arms", nargs="+", default=["original", "improved"])
    ap.add_argument("--ids", nargs="+")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--no-eval", action="store_true")
    a, extra = ap.parse_known_args()
    ids = a.ids or default_ids()
    jobs = [(arm, iid) for iid in ids for arm in a.arms]
    print(f"{len(jobs)} jobs, {a.workers} in parallel")
    t0 = time.time()
    with ThreadPoolExecutor(a.workers) as pool:
        futs = [pool.submit(run_one, a.run_id, arm, iid, extra) for arm, iid in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            arm, iid, status = fut.result()
            print(f"[{i}/{len(jobs)}] {time.time() - t0:6.0f}s  {arm:9s} {iid}: {status}", flush=True)

    if a.no_eval:
        return
    for arm in a.arms:
        preds = HERE / "runs" / a.run_id / f"predictions_{arm}.jsonl"
        rows = [json.loads(p.read_text()) for p in sorted((HERE / "runs" / a.run_id / arm).glob("*/out/prediction.json"))]
        preds.write_text("".join(json.dumps(r) + "\n" for r in rows))
        print(f"\nScoring {arm}: {len(rows)} predictions")
        subprocess.call([sys.executable, "-m", "swebench.harness.run_evaluation",
                         "--dataset_name", "princeton-nlp/SWE-bench_Lite", "--split", "test",
                         "--predictions_path", str(preds), "--max_workers", "4",
                         "--namespace", "", "--cache_level", "instance",
                         "--run_id", f"{a.run_id}_{arm}", "--report_dir", str(HERE / "runs" / a.run_id)], cwd=HERE)
    print(f"\nDone. Next: python report.py --run-id {a.run_id}")


if __name__ == "__main__":
    main()
