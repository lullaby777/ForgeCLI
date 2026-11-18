"""Print current behavior without touching the user's saved conversations."""
from test_acceptance import session
from pathlib import Path
import tempfile


def main():
    with tempfile.TemporaryDirectory(prefix="corecoder-session-repro-") as tmp:
        session.SESSIONS_DIR = Path(tmp)
        session.save_session([{"role": "user", "content": "正常会话"}], "test-model", "good")
        (Path(tmp) / "bad.json").write_text("[]", encoding="utf-8")
        for name, operation in (("加载坏会话", lambda: session.load_session("bad")),
                                ("列出全部会话", session.list_sessions)):
            try:
                print(name, "=>", repr(operation()))
            except Exception as error:
                print(name, "=>", type(error).__name__, str(error))


if __name__ == "__main__":
    main()
