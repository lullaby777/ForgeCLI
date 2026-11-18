"""External acceptance tests; load the real session.py without model dependencies.

Run: python3 -m unittest discover -s baseline/session_corruption -v
Set CORECODER_TARGET to test a separate candidate repository.
"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(os.environ.get("CORECODER_TARGET", Path(__file__).resolve().parents[2])).resolve()
spec = importlib.util.spec_from_file_location("session_under_test", ROOT / "corecoder/session.py")
session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session)


class SessionAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corecoder-session-test-")
        self.addCleanup(self.tmp.cleanup)
        previous = session.SESSIONS_DIR
        self.addCleanup(setattr, session, "SESSIONS_DIR", previous)
        session.SESSIONS_DIR = Path(self.tmp.name)
        self.messages = [{"role": "user", "content": "请修复问题"}]
        self.good_id = session.save_session(self.messages, "test-model", "good")

    def write_bad(self, raw):
        path = session.SESSIONS_DIR / "zz_bad.json"
        path.write_bytes(raw)
        return path

    def assert_invalid(self, raw):
        path = self.write_bad(raw)
        self.assertIsNone(session.load_session("zz_bad"))
        listed = session.list_sessions()
        self.assertEqual([item["id"] for item in listed], [self.good_id])
        self.assertEqual(path.read_bytes(), raw, "Invalid files must not be changed/deleted")
        self.assertEqual(session.load_session(self.good_id), (self.messages, "test-model"))

    def test_valid_unicode_roundtrip_and_preview(self):
        self.assertEqual(session.load_session(self.good_id), (self.messages, "test-model"))
        self.assertEqual(session.list_sessions()[0]["preview"], "请修复问题")

    def test_empty_conversation_is_valid(self):
        sid = session.save_session([], "test-model", "empty")
        self.assertEqual(session.load_session(sid), ([], "test-model"))
        item = next(s for s in session.list_sessions() if s["id"] == sid)
        self.assertEqual(item["preview"], "")

    def test_missing_session(self):
        self.assertIsNone(session.load_session("missing"))

    def test_invalid_json_and_encoding(self):
        for raw in (b"{broken", b"\xff\xfe"):
            with self.subTest(raw=raw):
                self.assert_invalid(raw)

    def test_invalid_top_level(self):
        for value in ([], None, 42, "text", True):
            with self.subTest(value=value):
                self.assert_invalid(json.dumps(value).encode())

    def test_listing_skips_each_invalid_structure(self):
        for value in ([], None, 42, "text", {}, {"model": "m"},
                      {"model": "m", "messages": "text"},
                      {"model": "m", "messages": [None]}):
            with self.subTest(value=value):
                self.write_bad(json.dumps(value).encode())
                self.assertEqual([s["id"] for s in session.list_sessions()], [self.good_id])

    def test_invalid_required_fields(self):
        cases = [{}, {"model": "m"}, {"messages": []},
                 {"model": None, "messages": []},
                 {"model": "", "messages": []},
                 {"model": "m", "messages": "text"},
                 {"model": "m", "messages": None}]
        for value in cases:
            with self.subTest(value=value):
                self.assert_invalid(json.dumps(value).encode())

    def test_invalid_message_entries(self):
        for message in (None, 42, "text", {}, {"role": 42, "content": "x"},
                        {"role": "user", "content": 42}):
            with self.subTest(message=message):
                self.assert_invalid(json.dumps({"model": "m", "messages": [message]}).encode())

    def test_tool_call_message_with_null_content_is_valid(self):
        # Valid model history must not be rejected by an over-strict fix.
        messages = [{"role": "user", "content": "check"},
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "call-1", "type": "function", "function":
                         {"name": "glob", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "call-1", "content": "a.py"}]
        sid = session.save_session(messages, "m", "tools")
        self.assertEqual(session.load_session(sid), (messages, "m"))
        self.assertIn(sid, [s["id"] for s in session.list_sessions()])

    def test_path_normalization_still_works(self):
        sid = session.save_session(self.messages, "m", "../../outside")
        self.assertEqual(sid, "outside")
        self.assertTrue((session.SESSIONS_DIR / "outside.json").exists())
        self.assertEqual(session.load_session("../../outside"), (self.messages, "m"))


if __name__ == "__main__":
    unittest.main()
