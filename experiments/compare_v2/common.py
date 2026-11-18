"""Measurement helpers shared by both experiment arms (no model calls here)."""
import hashlib
import json
import os
import signal
import subprocess
from pathlib import Path

SKIP = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}


def manifest(root):
    root = Path(root)
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not any(s in SKIP for s in p.relative_to(root).parts)}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def dump(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def local_env(workspace, home):
    env = os.environ.copy()
    for key in ("CORECODER_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "CORECODER_LIVE_SMOKE"):
        env.pop(key, None)
    Path(home).mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(home), CORECODER_TARGET=str(workspace), PYTHONPATH=str(workspace), PYTHONDONTWRITEBYTECODE="1",
               PYTHONHASHSEED="0", PYTEST_ADDOPTS="", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return env


def command(argv, cwd, output, env, timeout):
    with Path(output).open("wb") as log:
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            return {"exit_code": None, "timeout": False, "error_type": type(exc).__name__}
        try:
            return {"exit_code": process.wait(timeout=timeout), "timeout": False}
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            return {"exit_code": process.returncode, "timeout": True, "interrupted": isinstance(exc, KeyboardInterrupt)}
