"""Keep only candidates whose gold patch resolves on this machine (environment sanity check)."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ids = (HERE / "data/candidate_ids.txt").read_text().split()
reports = [p for d in (HERE, HERE / "results/official_reports") for p in d.glob("*gold_check*.json")]
if not reports:
    raise SystemExit("No gold_check report json found in swebench_eval/")
rep = json.loads(reports[0].read_text())
ok = set(rep.get("resolved_ids", []))
keep = [i for i in ids if i in ok]
drop = [i for i in ids if i not in ok]
(HERE / "data/selected_ids.txt").write_text(" ".join(keep) + "\n")
(HERE / "data/dropped_ids.json").write_text(json.dumps({"dropped": drop, "report": reports[0].name}, indent=2))
print(f"kept {len(keep)}/{len(ids)}; dropped: {drop}")
