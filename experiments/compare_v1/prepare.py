"""Prepare sealed cases only. No model client is constructed."""
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from common import digest, dump, manifest

PROJECT = Path(__file__).resolve().parents[2]
SUITE = Path("/tmp/corecoder-compare-01")


def freeze_runner():
    destination = SUITE / 'runner'
    destination.mkdir(exist_ok=False)
    for path in Path(__file__).parent.iterdir():
        if path.suffix == '.py' or path.name == 'README.md':
            shutil.copy2(path, destination / path.name)
    for case in sorted(SUITE.glob('task*-*')):
        settings = json.loads((case / 'case.json').read_text())
        settings['protected_paths'] = [str(destination) if p == str(Path(__file__).parent) else p
                                       for p in settings['protected_paths']]
        settings['behavior'] = {'chat_attempts': 1 if settings['arm'] == 'original' else 3,
                                'empty_response_retry': 0 if settings['arm'] == 'original' else 1,
                                'verification_feedback': settings['arm'] == 'improved'}
        dump(case / 'case.json', settings)
        seal = json.loads((case / 'seal.json').read_text())
        seal.update(settings=digest(settings), harness=manifest(destination))
        dump(case / 'seal.json', seal)


def main():
    SUITE.mkdir(exist_ok=False)
    first = Path("/tmp/corecoder-session-baseline-01")
    original = SUITE / "original-source"
    original.mkdir()
    archive = subprocess.check_output(["git", "archive", (first / "start-commit.txt").read_text().strip()],
                                      cwd=first / "candidate")
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(original, filter="data")
    assert manifest(original) == json.loads((first / "source-manifest.json").read_text())
    improved = SUITE / "improved-source"
    skip = {"baseline", "validation", "experiments", ".git", ".vscode", ".codex", ".agents", ".aws",
            ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
    shutil.copytree(PROJECT, improved, ignore=lambda p, names: [n for n in names if n in skip or n.startswith('.env')])
    for name in ("corecoder/tools/edit.py", "corecoder/tools/bash.py"):
        assert (original / name).read_bytes() == (improved / name).read_bytes(), "Task fix accidentally included in runtime"
    requirements = [line for line in (first / "dependencies.txt").read_text().splitlines()
                    if '==' in line and not line.startswith('#')]
    (SUITE / "requirements.txt").write_text('\n'.join(requirements) + '\n')
    subprocess.run([sys.executable, "-m", "venv", str(SUITE / "venv")], check=True)
    model = {"name": "deepseek-flash", "base_url": "https://api.deepseek.com", "temperature": 0.0,
             "max_tokens": 4096, "context_tokens": 128000, "request_seconds": 60, "sdk_retries": 0}
    limits = {"max_attempts": 3, "max_model_calls": 30, "rounds_per_attempt": 30,
              "max_seconds": 900, "verifier_seconds": 60, "max_stagnant_attempts": 2}
    for task, material in (("task02", "newline_preservation"), ("task04", "bash_cwd")):
        evaluation = SUITE / "evaluation" / task
        evaluation.mkdir(parents=True)
        for name in ("test_acceptance.py", "reproduce.py"):
            shutil.copy2(PROJECT / "baseline" / material / name, evaluation / name)
        task_text = (PROJECT / "baseline" / material / "task.md").read_text()
        task_text += ("\n实验约束：仅操作指定 workspace。不要读取运行时、实验配置、外部验收、其他实验目录或历史修复补丁。"
                      "不改删已有测试，可新增开发测试；不新增 conftest.py/pytest.ini 等改变验收行为的配置。\n")
        for arm, source in (("original", original), ("improved", improved)):
            case = SUITE / f"{task}-{arm}"
            case.mkdir()
            shutil.copytree(original, case / "workspace")
            shutil.copytree(source, case / "runtime")
            for cmd in (["git", "init", "-q", "-b", f"codex/compare-{task}-{arm}"], ["git", "add", "."],
                        ["git", "-c", "user.name=Baseline", "-c", "user.email=baseline@example.invalid",
                         "commit", "-q", "-m", "Controlled experiment starting snapshot"]):
                subprocess.run(cmd, cwd=case / "workspace", check=True)
            protected = [p for p in manifest(original) if p.startswith("tests/") or p == "pyproject.toml"]
            absent = ["pytest.ini", "conftest.py", "setup.cfg", "tox.ini"]
            protected_paths = protected + absent + [str(case / "runtime"), str(evaluation), str(case / "case.json"),
                                                     str(case / "task.md"), str(Path(__file__).parent)]
            settings = {"task": task, "arm": arm, "model": model, "limits": limits,
                        "behavior": {"chat_attempts": 1 if arm == "original" else 3,
                                     "empty_response_retry": 0 if arm == "original" else 1,
                                     "verification_feedback": arm == "improved"},
                        "evaluation": str(evaluation), "protected_paths": protected_paths, "absent_protected": absent,
                        "verification_argv": [str(SUITE / "venv/bin/python"), "-m", "unittest", "discover", "-s", str(evaluation), "-v"],
                        "known_regression_failures": ["tests/test_core.py::test_readme_line_counts_are_current"],
                        "start_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=case / "workspace", text=True).strip()}
            dump(case / "case.json", settings)
            (case / "task.md").write_text(task_text)
            dump(case / "seal.json", {"workspace": manifest(case / "workspace"), "runtime": manifest(case / "runtime"),
                                     "evaluation": manifest(evaluation), "settings": digest(settings), "task": digest(task_text),
                                     "protected_workspace": {p: manifest(original)[p] for p in protected}})
    dump(SUITE / "plan.json", {"model": model, "limits": limits,
                               "comparison": "original measured runtime vs ISSUE-001 + independent verification bundle",
                               "real_model_calls_during_preparation": 0})
    freeze_runner()
    print(SUITE)


if __name__ == "__main__":
    main()
