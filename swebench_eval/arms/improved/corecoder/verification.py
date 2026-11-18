"""Local acceptance and regression checks, independent of agent orchestration.

This service executes trusted commands and reports evidence. It does not call a
model, own a retry budget, capture a baseline automatically, or decide task flow.
"""

import os
import signal
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def _tail(path, limit=4000):
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - limit))
        return stream.read().decode("utf-8", errors="replace")


class VerificationRunner:
    def __init__(self, workspace, verification, result_dir, regression=None, *, executor=None):
        self.workspace = Path(workspace).resolve()
        self.verification = verification
        self.result_dir = Path(result_dir).resolve()
        self.regression = regression
        # The optional trusted executor keeps legacy controller overrides usable.
        # New callers normally use this service's run_check implementation.
        self._executor = executor
        if not self.workspace.is_dir():
            raise ValueError("workspace must exist")
        if self.result_dir == self.workspace or self.workspace in self.result_dir.parents:
            raise ValueError("result_dir must be outside workspace")

    def _execute(self, argv, output, timeout, failure_codes):
        execute = self._executor or self.run_check
        return execute(argv, output, timeout, failure_codes)

    def verify(self, number, timeout):
        output = self.result_dir / f"verification-{number}.txt"
        return self._execute(self.verification.argv, output, timeout, self.verification.failure_codes)

    def regress(self, label, timeout, baseline=None):
        output = self.result_dir / f"regression-{label}.txt"
        report = self.result_dir / f"regression-{label}.xml"
        checked = self._execute((*self.regression.argv, "--junitxml", str(report)), output, timeout, (1,))
        checked.update(phase="regression", report=str(report))
        if checked["status"] not in {"passed", "failed"}:
            return checked
        try:
            tests = {}
            failures = {}
            for test in ET.parse(report).iter("testcase"):
                key = test.get("classname", "") + "::" + test.attrib["name"]
                if key in tests:
                    raise ValueError("duplicate test identifiers")
                problems = [*test.findall("failure"), *test.findall("error")]
                tests[key] = "failed" if problems else "skipped" if test.find("skipped") is not None else "passed"
                if problems:
                    failures[key] = (problems[0].get("message") or problems[0].text or "Test failed")[:600]
            if not tests or not any(outcome != "skipped" for outcome in tests.values()):
                raise ValueError("no executed tests")
            if (checked["exit_code"] == 1) != bool(failures):
                raise ValueError("exit code and report disagree")
        except (OSError, ET.ParseError, KeyError, ValueError) as exc:
            checked.update(status="error", error_type=type(exc).__name__, feedback="Regression report missing or invalid")
            return checked
        checked.update(tests=tests, failures=sorted(failures))
        known = set(baseline["failures"]) if baseline else set(failures)
        new = sorted(set(failures) - known)
        lost = sorted(key for key, outcome in (baseline or {}).get("tests", {}).items()
                      if key not in tests or (outcome != "skipped" and tests[key] == "skipped"))
        checked.update(status="failed" if new or lost else "passed", new_failures=new, lost_tests=lost,
                       known_failures_present=sorted(set(failures) & known))
        if baseline is not None:
            # Include a stable failure identifier and useful evidence, rather
            # than treating all nonzero exits (including old failures) alike.
            checked["feedback"] = "\n".join(
                ["Task acceptance passed. Regression comparison against the frozen pre-edit baseline:"]
                + [f"FAILED {key[:200]}: {failures[key][:240]}" for key in new[:8]]
                + [f"FAILED missing or skipped baseline test: {key[:200]}" for key in lost[:8]])
        return checked

    def run_check(self, argv, output, timeout, failure_codes):
        env = os.environ.copy()
        for key in ("CORECODER_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "CORECODER_LIVE_SMOKE"):
            env.pop(key, None)
        env["CORECODER_TARGET"] = str(self.workspace)
        env["PYTHONPATH"] = str(self.workspace) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTEST_ADDOPTS"] = ""
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        try:
            with output.open("wb") as stream:
                process = subprocess.Popen(argv, cwd=self.workspace, shell=False,
                                           stdout=stream, stderr=subprocess.STDOUT, env=env,
                                           start_new_session=os.name != "nt")
                try:
                    code = process.wait(timeout=timeout)
                    status = "passed" if code == 0 else "failed" if code in failure_codes else "error"
                except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                    if os.name != "nt":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                    process.wait()
                    if isinstance(exc, KeyboardInterrupt):
                        raise
                    code, status = process.returncode, "timeout"
        except OSError as exc:
            return {"status": "error", "exit_code": None, "error_type": type(exc).__name__, "feedback": "Verifier could not start"}
        return {"status": status, "exit_code": code, "feedback": f"Exit code: {code}\n{_tail(output)}", "output": str(output)}
