"""Join official resolution results with per-run metrics into a scorecard."""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()
    root = HERE / "runs" / a.run_id
    rows, summary = [], {}
    for arm_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        arm = arm_dir.name
        reports = [p for d in (root, HERE, HERE / "results/official_reports") for p in d.glob(f"*{a.run_id}_{arm}.json")]
        resolved = set(json.loads(reports[0].read_text()).get("resolved_ids", [])) if reports else set()
        ms = [json.loads(p.read_text()) for p in sorted(arm_dir.glob("*/out/metrics.json"))]
        for m in ms:
            m["resolved"] = m["instance_id"] in resolved
            rows.append(m)
        n = len(ms) or 1
        tok = [m["prompt_tokens"] + m["completion_tokens"] for m in ms]
        summary[arm] = {
            "instances": len(ms), "resolved": sum(m["resolved"] for m in ms),
            "resolve_rate": round(sum(m["resolved"] for m in ms) / n, 3),
            "empty_patch": sum(m.get("patch_lines", 0) == 0 for m in ms),
            "avg_model_calls": round(sum(m["model_calls"] for m in ms) / n, 1),
            "avg_tokens": round(sum(tok) / n), "avg_seconds": round(sum(m["seconds"] for m in ms) / n, 1),
            "tokens_per_resolved": round(sum(tok) / max(1, sum(m["resolved"] for m in ms))),
            "stops": {s: sum(m.get("stop") == s for m in ms) for s in sorted({str(m.get("stop")) for m in ms})},
            "scored": bool(reports),
        }
    (root / "scorecard.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    lines = ["| arm | resolved | rate | empty patch | avg calls | avg tokens | tokens/resolved | avg s |", "|" + "---|" * 8]
    for arm, s in summary.items():
        lines.append(f"| {arm} | {s['resolved']}/{s['instances']} | {s['resolve_rate']:.0%} | {s['empty_patch']} | "
                     f"{s['avg_model_calls']} | {s['avg_tokens']:,} | {s['tokens_per_resolved']:,} | {s['avg_seconds']} |")
    ids = sorted({r["instance_id"] for r in rows})
    by = {(r["arm"], r["instance_id"]): r for r in rows}
    lines += ["", "| instance | " + " | ".join(summary) + " |", "|---|" + "---|" * len(summary)]
    for i in ids:
        cells = []
        for arm in summary:
            r = by.get((arm, i))
            cells.append("-" if not r else ("✅" if r["resolved"] else "❌") + f" {r.get('stop')}")
        lines.append(f"| {i} | " + " | ".join(cells) + " |")
    (root / "scorecard.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
