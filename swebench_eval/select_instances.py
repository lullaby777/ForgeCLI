"""Deterministically select SWE-bench Lite instances from small repos.

Rule (fixed, not cherry-picked): every SWE-bench Lite test instance whose
repo is in REPOS. Instances whose gold patch fails on this machine are
dropped later by filter_gold.py (environment problems, not agent problems).
"""
import json
from pathlib import Path

from datasets import load_dataset

REPOS = {"pytest-dev/pytest", "psf/requests", "pallets/flask"}
OUT = Path(__file__).parent / "data"


def main():
    ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
    rows = sorted((r for r in ds if r["repo"] in REPOS), key=lambda r: r["instance_id"])
    OUT.mkdir(exist_ok=True)
    with open(OUT / "candidates.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(dict(r)) + "\n")
    (OUT / "candidate_ids.txt").write_text(" ".join(r["instance_id"] for r in rows) + "\n")
    by_repo = {}
    for r in rows:
        by_repo[r["repo"]] = by_repo.get(r["repo"], 0) + 1
    print(f"{len(rows)} candidates: {by_repo}")
    print(f"written to {OUT}")


if __name__ == "__main__":
    main()
