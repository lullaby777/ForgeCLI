"""Run pre/post checks, never run an agent or call a model.

Usage: PREPARED_VENV/bin/python check.py before|after
"""
import argparse
import json
import os
import re
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("before", "after"))
    parser.add_argument("--label", help="Separate log label, e.g. continuation-01")
    args = parser.parse_args()
    label = args.label or args.stage
    if not re.fullmatch(r"[A-Za-z0-9_-]+", label):
        parser.error("Label must contain only letters, digits, underscores or hyphens")
    base = Path("/tmp/corecoder-agent-isolation-baseline-01")
    material = Path(__file__).resolve().parent
    env = os.environ.copy()
    for name in ("CORECODER_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "CORECODER_LIVE_SMOKE"):
        env.pop(name, None)
    env["CORECODER_TARGET"] = str(base / "candidate")
    env["HOME"] = str(base / "test-home")
    Path(env["HOME"]).mkdir(exist_ok=True)
    jobs = {
        "reproduction": [sys.executable, str(material / "reproduce.py")],
        "acceptance": [sys.executable, "-m", "unittest", "discover", "-s", str(material), "-v"],
        "regression": [sys.executable, "-m", "pytest", "tests/", "-q", "--ignore=tests/test_live_smoke.py"],
    }
    results = {}
    for name in [*jobs, "checks"]:
        suffix = ".json" if name == "checks" else ".txt"
        path = base / f"{name}-{label}{suffix}"
        if path.exists():
            parser.error(f"Refusing to overwrite {path}; use a new --label")
    for name, command in jobs.items():
        logfile = base / f"{name}-{label}.txt"
        if logfile.exists():
            parser.error(f"Refusing to overwrite {logfile}; archive it before rerunning")
        with logfile.open("w") as out:
            process = subprocess.run(command, cwd=base / "candidate", env=env,
                                     stdout=out, stderr=subprocess.STDOUT)
        results[name] = {"exit_code": process.returncode, "log": str(logfile)}
        print(f"{name}: exit={process.returncode}, log={logfile}", flush=True)
    (base / f"checks-{label}.json").write_text(json.dumps(results, indent=2))
    return 1 if any(item["exit_code"] for item in results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
