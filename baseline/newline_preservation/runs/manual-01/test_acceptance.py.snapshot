"""External byte-level acceptance tests. Run with the prepared venv.

CORECODER_TARGET points to a separate candidate; no model calls are made.
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(os.environ.get("CORECODER_TARGET", Path(__file__).resolve().parents[2])).resolve()
sys.path.insert(0, str(ROOT))
from corecoder import checkpoints
from corecoder.tools.edit import EditFileTool, _changed_files


class NewlineAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="corecoder-newline-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.previous_path = checkpoints.CHECKPOINTS_FILE
        self.previous_stack = checkpoints._stack[:]
        self.previous_loaded = checkpoints._loaded
        self.previous_changed = _changed_files.copy()
        self.addCleanup(self.restore_state)
        checkpoints.CHECKPOINTS_FILE = self.root / "checkpoints.json"
        checkpoints._stack.clear()
        checkpoints._loaded = False
        _changed_files.clear()
        self.tool = EditFileTool()

    def restore_state(self):
        checkpoints._stack[:] = self.previous_stack
        checkpoints._loaded = self.previous_loaded
        checkpoints.CHECKPOINTS_FILE = self.previous_path
        _changed_files.clear()
        _changed_files.update(self.previous_changed)

    def file(self, raw):
        path = self.root / "sample.txt"
        path.write_bytes(raw)
        return path

    def edited(self, before, old, new, expected):
        path = self.file(before)
        result = self.tool.execute(str(path), old, new)
        self.assertTrue(result.startswith("Edited "), result)
        self.assertEqual(path.read_bytes(), expected)
        return path, result

    def test_crlf_single_word(self):
        self.edited(b"alpha\r\nbeta\r\ngamma\r\n", "beta", "BETA",
                    b"alpha\r\nBETA\r\ngamma\r\n")

    def test_lf_single_word(self):
        self.edited(b"alpha\nbeta\ngamma\n", "beta", "BETA", b"alpha\nBETA\ngamma\n")

    def test_crlf_multiline_with_lf_arguments(self):
        self.edited(b"head\r\nalpha\r\nbeta\r\ntail\r\n", "alpha\nbeta", "A\nB\nC",
                    b"head\r\nA\r\nB\r\nC\r\ntail\r\n")

    def test_crlf_multiline_with_crlf_arguments(self):
        self.edited(b"head\r\nalpha\r\nbeta\r\ntail\r\n", "alpha\r\nbeta", "A\r\nB",
                    b"head\r\nA\r\nB\r\ntail\r\n")

    def test_lf_multiline(self):
        self.edited(b"head\nalpha\nbeta\ntail\n", "alpha\nbeta", "A\nB\nC",
                    b"head\nA\nB\nC\ntail\n")

    def test_no_final_newline_preserved(self):
        for before, expected in ((b"alpha\r\nbeta", b"alpha\r\nBETA"),
                                 (b"alpha\nbeta", b"alpha\nBETA"), (b"beta", b"BETA")):
            with self.subTest(before=before):
                self.edited(before, "beta", "BETA", expected)

    def test_unicode_and_blank_lines(self):
        self.edited("标题\r\n\r\n旧内容\r\n结尾\r\n".encode(), "旧内容", "新内容",
                    "标题\r\n\r\n新内容\r\n结尾\r\n".encode())

    def test_missing_match_does_not_change_file_or_checkpoint(self):
        raw = b"alpha\r\nbeta\r\n"
        path = self.file(raw)
        self.assertTrue(self.tool.execute(str(path), "missing", "X").startswith("Error:"))
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(checkpoints.pending(), 0)
        self.assertNotIn(str(path.resolve()), _changed_files)

    def test_ambiguous_match_does_not_change_file_or_checkpoint(self):
        raw = b"beta\r\nbeta\r\n"
        path = self.file(raw)
        self.assertTrue(self.tool.execute(str(path), "beta", "X").startswith("Error:"))
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(checkpoints.pending(), 0)

    def test_non_utf8_is_rejected_without_modification(self):
        raw = b"alpha\r\n\xff\r\n"
        path = self.file(raw)
        self.assertTrue(self.tool.execute(str(path), "alpha", "X").startswith("Error:"))
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(checkpoints.pending(), 0)

    def test_undo_after_restart_restores_original_bytes(self):
        raw = b"alpha\r\nbeta\r\n"
        path = self.file(raw)
        result = self.tool.execute(str(path), "beta", "BETA")
        self.assertTrue(result.startswith("Edited "), result)
        self.assertEqual(checkpoints.pending(), 1)
        checkpoints._stack.clear()
        checkpoints._loaded = False
        self.assertEqual(checkpoints.pending(), 1)
        self.assertTrue(checkpoints.undo().startswith("Restored "))
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(checkpoints.pending(), 0)

    def test_diff_reports_only_the_changed_line(self):
        path = self.file(b"alpha\r\nbeta\r\ngamma\r\n")
        result = self.tool.execute(str(path), "beta", "BETA")
        self.assertTrue(result.startswith("Edited "), result)
        changes = [line for line in result.splitlines()
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
        self.assertEqual(changes, ["-beta", "+BETA"])


if __name__ == "__main__":
    unittest.main()
