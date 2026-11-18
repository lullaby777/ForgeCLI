"""Summarize the checkpoint fault-injection experiment."""
import argparse
import collections
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARM = "improved_v2"


def load(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip().startswith("{")] if path.exists() else []


def tokens(evs):
    return sum((e.get("prompt_tokens") or 0) + (e.get("completion_tokens") or 0) for e in evs if e["kind"] == "llm_response")


def resolved_ids(rid):
    rep = [p for d in (HERE / "runs" / rid, HERE, HERE / "results/official_reports") for p in d.glob(f"*.{rid}.json")]
    return set(json.loads(rep[0].read_text()).get("resolved_ids", [])) if rep else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--arm", default="improved_v2")
    ap.add_argument("--control", help="run id whose -nofault runs are the control (default: same run id)")
    a = ap.parse_args()
    global ARM
    ARM = a.arm
    nf_id, k_id = f"{a.control or a.run_id}-nofault", f"{a.run_id}-kill"
    nf_res, k_res = resolved_ids(nf_id), resolved_ids(k_id)
    log = {r["iid"]: r for r in json.loads((HERE / "runs" / k_id / "fault_log.json").read_text())}
    rows = []
    for iid, rec in sorted(log.items()):
        nf_dir = HERE / "runs" / nf_id
        nf_trace = load(next(nf_dir.glob(f"*/{iid}/out/trace.jsonl"), nf_dir / "missing"))
        k_trace = load(HERE / "runs" / k_id / ARM / iid / "out" / "trace.jsonl")
        starts = [i for i, e in enumerate(k_trace) if e["kind"] == "process_start"]
        cut = starts[1] if len(starts) > 1 else len(k_trace)
        before, after = k_trace[:cut], k_trace[cut:]
        lost = sum(e["kind"] == "llm_request" for e in before) - sum(e["kind"] == "llm_response" for e in before)
        first_req = next((e for e in after if e["kind"] == "llm_request"), None)
        nf_tokens, pre_kill, post = tokens(nf_trace), tokens(before), tokens(after)
        stop = rec.get("resume_stop")
        segs_file = HERE / "runs" / k_id / ARM / iid / "out" / "segments.json"
        err = (json.loads(segs_file.read_text())[-1].get("error") or "") if segs_file.exists() else ""
        if stop == "harness_error" and "Protected tests or verification configuration changed" in err:
            stop = "refused_protected_inputs_changed"  # the agent had tampered with protected inputs before the kill
        rows.append({
            "instance_id": iid, "killed": rec.get("killed"),
            "landed_in": ((rec.get("checkpoint_at_kill") or {}).get("pending") or "between_steps") if rec.get("killed") else "not_killed",
            "phase_at_kill": (rec.get("checkpoint_at_kill") or {}).get("phase"),
            "resume_stop": stop,
            "resumed_with_history_msgs": first_req["n_messages"] if first_req else None,
            "lost_inflight_requests": max(0, lost),
            "kill_resolved": None if k_res is None else iid in k_res,
            "nofault_resolved": None if nf_res is None else iid in nf_res,
            "tokens_kill_total": pre_kill + post, "tokens_pre_kill": pre_kill,
            "tokens_restart_estimate": pre_kill + nf_tokens, "tokens_nofault": nf_tokens,
        })
    killed = [r for r in rows if r["killed"]]
    blocked = [r for r in killed if str(r["resume_stop"]).endswith("_outcome_unknown")]
    refused = [r for r in killed if r["resume_stop"] == "refused_protected_inputs_changed"]
    continued = [r for r in killed if r not in blocked and r not in refused and r["resume_stop"] not in (None, "harness_error")]
    summary = {
        "instances": len(rows), "killed": len(killed),
        "kill_landed_in": dict(collections.Counter(r["landed_in"] for r in killed)),
        "resume_outcome": dict(collections.Counter(str(r["resume_stop"]) for r in killed)),
        "resumed_and_continued": len(continued), "blocked_by_unknown_side_effect": len(blocked),
        "refused_agent_tampered_protected_inputs": len(refused),
        "resolved_nofault": None if nf_res is None else sum(bool(r["nofault_resolved"]) for r in rows),
        "resolved_kill_resume": None if k_res is None else sum(bool(r["kill_resolved"]) for r in killed),
        "tokens_kill_resume": sum(r["tokens_kill_total"] for r in killed),
        "tokens_restart_from_scratch_est": sum(r["tokens_restart_estimate"] for r in killed),
    }
    if summary["tokens_restart_from_scratch_est"]:
        summary["token_saving_vs_restart"] = round(1 - summary["tokens_kill_resume"] / summary["tokens_restart_from_scratch_est"], 3)
    (HERE / "runs" / k_id / "fault_report.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
    print(json.dumps(summary, indent=2))
    print("\n| instance | landed in | resume stop | history msgs | kill+resume | no-fault |\n|---|---|---|---|---|---|")
    for r in rows:
        f = lambda v: "-" if v is None else ("✅" if v else "❌")
        print(f"| {r['instance_id']} | {r['landed_in']} | {r['resume_stop']} | {r['resumed_with_history_msgs']} | "
              f"{f(r['kill_resolved'])} | {f(r['nofault_resolved'])} |")


if __name__ == "__main__":
    main()
