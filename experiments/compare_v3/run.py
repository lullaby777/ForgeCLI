"""Launch one controlled experiment and independently score its final workspace."""
import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from common import command, digest, dump, local_env, manifest

SUITE = Path("/tmp/corecoder-compare-03")


def run_case(case, live):
    if live and not os.environ.get("CORECODER_API_KEY"):
        raise SystemExit("Set CORECODER_API_KEY first; no case has been consumed")
    settings = json.loads((case / "case.json").read_text())
    seal = json.loads((case / "seal.json").read_text())
    for name in ("workspace", "runtime"):
        if manifest(case / name) != seal[name]:
            raise SystemExit(f"{name} changed before run; prepare a fresh case")
    if digest(settings) != seal["settings"] or digest((case / "task.md").read_text()) != seal["task"]:
        raise SystemExit("Frozen configuration/task changed")
    evaluation = Path(settings["evaluation"])
    if manifest(evaluation) != seal["evaluation"]:
        raise SystemExit("External acceptance changed")
    harness_before = manifest(Path(__file__).parent)
    if "harness" in seal and seal["harness"] != harness_before:
        raise SystemExit("Frozen measurement scripts changed")
    workspace = case / "workspace"
    output = case / "run"
    output.mkdir(exist_ok=False)  # never reuse a partly or fully executed case
    dump(output / "settings.json", settings)
    env = local_env(workspace, case / "agent-home")
    if live:
        env["CORECODER_API_KEY"] = os.environ["CORECODER_API_KEY"]
    launched = command([sys.executable, str(Path(__file__).with_name("worker.py")), str(case),
                        "--live" if live else "--offline"], workspace, output / "terminal.txt", env,
                       settings["limits"]["max_seconds"])
    judge_env = local_env(workspace, case / "judge-home")
    acceptance = command(settings["verification_argv"], workspace, output / "acceptance.txt", judge_env, 60)
    regression = command([sys.executable, "-m", "pytest", "tests/", "-q", "--ignore=tests/test_live_smoke.py",
                          "--junitxml", str(output / "regression.xml")], workspace,
                         output / "regression.txt", judge_env, 120)
    failures = []
    if (output / "regression.xml").exists():
        for test in ET.parse(output / "regression.xml").iter("testcase"):
            if test.find("failure") is not None or test.find("error") is not None:
                failures.append(test.get("classname", "").replace(".", "/") + ".py::" + test.get("name", ""))
    current = manifest(workspace)
    protected_changed = [p for p, h in seal["protected_workspace"].items() if current.get(p) != h]
    protected_changed += [p for p in settings["absent_protected"] if (workspace / p).exists()]
    runtime_changed = manifest(case / "runtime") != seal["runtime"]
    acceptance_changed = manifest(evaluation) != seal["evaluation"]
    harness_changed = manifest(Path(__file__).parent) != harness_before
    new_failures = sorted(set(failures) - set(settings["known_regression_failures"]))
    valid_regression = (bool((output / "regression.xml").exists()) and regression["exit_code"] in (0, 1)
                        and not regression["timeout"] and not new_failures)
    passed = (acceptance["exit_code"] == 0 and not acceptance["timeout"] and valid_regression
              and not protected_changed and not runtime_changed and not acceptance_changed and not harness_changed)
    result = {"task": settings["task"], "arm": settings["arm"], "live": live,
              "functional_passed": passed, "execution_process": launched, "acceptance": acceptance,
              "regression": regression, "new_regression_failures": new_failures,
              "known_regression_failures_present": sorted(set(failures) & set(settings["known_regression_failures"])),
              "protected_tests_changed": protected_changed, "runtime_changed": runtime_changed,
              "acceptance_changed": acceptance_changed, "harness_changed": harness_changed}
    if (output / "worker.json").exists():
        result["execution"] = json.loads((output / "worker.json").read_text())
    else:
        result["execution"] = {"execution_status": "worker_did_not_finalize", "usage": "unknown"}
    dump(output / "summary.json", result)
    patch = subprocess.check_output(["git", "diff", "--binary", settings["start_commit"]], cwd=workspace)
    (output / "change.patch").write_bytes(patch)
    (output / "git-status.txt").write_bytes(subprocess.check_output(["git", "status", "--short"], cwd=workspace))
    print(json.dumps({"task": settings["task"], "arm": settings["arm"], "functional_passed": passed,
                      "execution": result["execution"].get("execution_status"), "summary": str(output / "summary.json")}, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", choices=("task04",))
    parser.add_argument("arm", choices=("generic", "staged"))
    parser.add_argument("--live", action="store_true", required=True, help="Explicitly authorize this model run")
    args = parser.parse_args()
    if Path(sys.prefix).resolve() != (SUITE / "venv").resolve():
        raise SystemExit("Use /tmp/corecoder-compare-03/venv/bin/python for identical dependencies")
    run_case(SUITE / f"{args.task}-{args.arm}", live=True)


if __name__ == "__main__":
    main()
