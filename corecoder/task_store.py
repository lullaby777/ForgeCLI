"""Atomic, checksummed task records and a process lock. No model-owned state."""
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_record(path, value, *, exclusive=False):
    path = Path(path)
    data = json.dumps({'payload': value, 'checksum': checksum(value)}, ensure_ascii=False, indent=2)
    fd, temporary = tempfile.mkstemp(prefix='.task-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)  # never overwrite a frozen baseline
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_record(path):
    try:
        envelope = json.loads(Path(path).read_text(encoding='utf-8'))
        value = envelope['payload']
        if checksum(value) != envelope['checksum']:
            raise ValueError('Task checksum mismatch')
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError('Task record missing or corrupt; refusing to reset progress') from exc


@contextmanager
def task_lock(directory):
    # First version supports local POSIX processes; a crashed process releases
    # flock automatically. Never infer stale locks by deleting a PID file.
    try:
        import fcntl
    except ImportError as exc:
        raise ValueError('Durable task recovery currently requires POSIX') from exc
    with (Path(directory) / 'task.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('This task is already running') from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
