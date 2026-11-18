"""Session persistence - save and resume conversations.

Claude Code maintains session state via QueryEngine (1295 lines).
CoreCoder distills this to: JSON dump of messages + model config.
"""

import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from .messages import clean_history

SESSIONS_DIR = Path.home() / ".corecoder" / "sessions"
_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_SESSION_ID_LEN = 100  # keep filenames comfortably under the OS limit


def _normalize_session_id(session_id: str | None) -> str:
    if not session_id:
        return _new_session_id()

    name = session_id.strip().replace("\\", "/").split("/")[-1]
    name = _SAFE_SESSION_RE.sub("-", name).strip(".-_")
    if len(name) > _MAX_SESSION_ID_LEN:
        name = name[:_MAX_SESSION_ID_LEN].strip(".-_")
    return name or _new_session_id()


def _new_session_id() -> str:
    return f"session_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _session_path(session_id: str) -> Path:
    path = (SESSIONS_DIR / f"{_normalize_session_id(session_id)}.json").resolve()
    root = SESSIONS_DIR.resolve()
    if root != path.parent:
        raise ValueError("Invalid session id")
    return path


def save_session(messages: list[dict], model: str, session_id: str | None = None, *, task_checkpoint=None) -> str:
    """Save conversation to disk. Returns the session ID."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    session_id = _normalize_session_id(session_id)

    data = {
        "id": session_id,
        "model": model,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "messages": clean_history(messages),
    }
    if task_checkpoint is not None:
        data["task_checkpoint"] = str(Path(task_checkpoint).expanduser().resolve())

    path = _session_path(session_id)
    # Atomic writing while preserving the legacy plain session JSON format.
    # Sessions carry only a link; the separately checksummed task is authoritative.
    fd, temporary = tempfile.mkstemp(prefix='.session-', dir=SESSIONS_DIR)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return session_id


def load_task_link(session_id):
    """A session selects a task; it cannot reset the task's checkpoint ledger."""
    data = json.loads(_session_path(session_id).read_text(encoding='utf-8'))
    path = data.get('task_checkpoint')
    return str(Path(path).expanduser().resolve()) if isinstance(path, str) and path else None


def load_session(session_id: str) -> tuple[list[dict], str] | None:
    """Load a saved session. Returns (messages, model) or None."""
    path = _session_path(session_id)
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return clean_history(data["messages"]), data["model"]
    except (ValueError, UnicodeDecodeError, KeyError, OSError, TypeError):
        # a corrupt or truncated session file shouldn't crash resume
        return None


def list_sessions() -> list[dict]:
    """List available sessions, newest first."""
    if not SESSIONS_DIR.exists():
        return []

    sessions = []
    for f in sorted(SESSIONS_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            # grab first user message as preview
            preview = ""
            for m in data.get("messages", []):
                if m.get("role") == "user" and m.get("content"):
                    preview = m["content"][:80]
                    break
            task_path = data.get('task_checkpoint')
            task_status = None
            if task_path:
                from .tasks import RepairTask
                try:
                    task = RepairTask.info(task_path)
                    task_status = task['status']
                    preview = task['task'][:80]
                except (ValueError, KeyError, TypeError):
                    task_status = 'checkpoint_unavailable'
            sessions.append({
                "id": data.get("id", f.stem),
                "model": data.get("model", "?"),
                "saved_at": data.get("saved_at", "?"),
                "preview": preview,
                "task_status": task_status,
            })
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
            continue

    return sessions[:20]  # cap at 20
