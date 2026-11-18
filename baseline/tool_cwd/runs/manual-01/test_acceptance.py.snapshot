"""Offline path-consistency acceptance; all file effects are temporary."""
import concurrent.futures
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(os.environ.get("CORECODER_TARGET", Path(__file__).resolve().parents[2])).resolve()
sys.path.insert(0, str(ROOT))
from corecoder import checkpoints
from corecoder.agent import Agent
from corecoder.tools.agent import AgentTool
from corecoder.tools.bash import BashTool, get_tracked_cwd, set_tracked_cwd
from corecoder.tools.edit import EditFileTool, _changed_files
from corecoder.tools.glob_tool import GlobTool
from corecoder.tools.grep import GrepTool
from corecoder.tools.read import ReadFileTool
from corecoder.tools.write import WriteFileTool


class Call:
    def __init__(self, ident, name, arguments):
        self.id, self.name, self.arguments = ident, name, arguments


class ToolCwdAcceptance(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="corecoder-tool-cwd-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        for name, content in (("", "ROOT VALUE"), ("backend", "SUB VALUE"), ("other", "OTHER VALUE")):
            folder = self.root / name
            folder.mkdir(exist_ok=True)
            (folder / "note.txt").write_text(content, encoding="utf-8")
        (self.root / "backend/assets").mkdir()
        (self.root / "backend/assets/asset.txt").write_text("ASSET", encoding="utf-8")
        prior_cwd, prior_tracked = os.getcwd(), get_tracked_cwd()
        self.addCleanup(os.chdir, prior_cwd)
        self.addCleanup(set_tracked_cwd, prior_tracked)
        prior_checkpoint = checkpoints.CHECKPOINTS_FILE
        prior_stack, prior_loaded = checkpoints._stack[:], checkpoints._loaded
        prior_changed = _changed_files.copy()

        def restore():
            checkpoints.CHECKPOINTS_FILE = prior_checkpoint
            checkpoints._stack[:] = prior_stack
            checkpoints._loaded = prior_loaded
            _changed_files.clear()
            _changed_files.update(prior_changed)

        self.addCleanup(restore)
        checkpoints.CHECKPOINTS_FILE = self.root / "undo.json"
        checkpoints._stack.clear()
        checkpoints._loaded = False
        os.chdir(self.root)  # Fixture only; implementations must not do this.
        set_tracked_cwd(str(self.root))
        self.bash, self.read, self.edit, self.write = BashTool(), ReadFileTool(), EditFileTool(), WriteFileTool()
        self.grep, self.glob = GrepTool(), GlobTool()
        self.bash.execute("cd backend")  # Successful cd only: does not depend on task 4.

    def test_read_matches_shell_after_cd(self):
        self.assertEqual(self.bash.execute("cat note.txt"), "SUB VALUE")
        self.assertIn("SUB VALUE", self.read.execute("note.txt"))
        self.assertNotIn("ROOT VALUE", self.read.execute("note.txt"))

    def test_edit_changes_only_session_file(self):
        result = self.edit.execute("note.txt", "VALUE", "EDITED")
        self.assertTrue(result.startswith("Edited "), result)
        self.assertEqual((self.root / "backend/note.txt").read_text(), "SUB EDITED")
        self.assertEqual((self.root / "note.txt").read_text(), "ROOT VALUE")

    def test_write_overwrites_only_session_file(self):
        self.write.execute("note.txt", "NEW")
        self.assertEqual((self.root / "backend/note.txt").read_text(), "NEW")
        self.assertEqual((self.root / "note.txt").read_text(), "ROOT VALUE")

    def test_write_creates_nested_file_in_session_directory(self):
        self.write.execute("new/deep.txt", "created")
        self.assertTrue((self.root / "backend/new/deep.txt").is_file())
        self.assertFalse((self.root / "new").exists())

    def test_glob_default_searches_only_session_directory(self):
        result = self.glob.execute("*.txt")
        self.assertEqual({Path(line).resolve() for line in result.splitlines()}, {self.root / "backend/note.txt"})

    def test_grep_default_searches_only_session_directory(self):
        result = self.grep.execute("VALUE")
        self.assertIn("SUB VALUE", result)
        self.assertNotIn("ROOT VALUE", result)
        self.assertNotIn("OTHER VALUE", result)

    def test_grep_relative_filename_uses_session_directory(self):
        self.assertIn("SUB VALUE", self.grep.execute("VALUE", path="note.txt"))

    def test_explicit_relative_search_directory(self):
        self.assertIn("ASSET", self.grep.execute("ASSET", path="assets"))
        result = self.glob.execute("*.txt", path="assets")
        self.assertEqual(Path(result.strip()).resolve(), self.root / "backend/assets/asset.txt")

    def test_absolute_paths_keep_their_meaning(self):
        file = self.root / "note.txt"
        self.assertIn("ROOT VALUE", self.read.execute(str(file)))
        self.assertIn("ROOT VALUE", self.grep.execute("VALUE", path=str(file)))
        self.assertIn(str(file), self.glob.execute("*.txt", path=str(self.root)))
        self.edit.execute(str(file), "VALUE", "ABS")
        self.write.execute(str(self.root / "absolute.txt"), "ABSOLUTE")
        self.assertEqual(file.read_text(), "ROOT ABS")
        self.assertEqual((self.root / "absolute.txt").read_text(), "ABSOLUTE")
        self.assertEqual((self.root / "backend/note.txt").read_text(), "SUB VALUE")

    def test_tilde_still_uses_home(self):
        with patch.dict(os.environ, {"HOME": str(self.root / "other")}):
            self.assertIn("OTHER VALUE", self.read.execute("~/note.txt"))

    def test_unset_session_directory_falls_back_to_process_directory(self):
        set_tracked_cwd(None)
        self.assertIn("ROOT VALUE", self.read.execute("note.txt"))

    def test_parallel_file_tools_inherit_batch_directory(self):
        agent = Agent(llm=object(), tools=[self.read, self.write])
        results = agent._exec_tools_parallel([
            Call("read", "read_file", {"file_path": "note.txt"}),
            Call("write", "write_file", {"file_path": "batch.txt", "content": "batch"}),
        ])
        self.assertIn("SUB VALUE", results[0])
        self.assertEqual((self.root / "backend/batch.txt").read_text(), "batch")
        self.assertFalse((self.root / "batch.txt").exists())
        self.assertEqual(Path(os.getcwd()), self.root)

    def test_same_batch_uses_start_directory_then_merges_cd(self):
        agent = Agent(llm=object(), tools=[self.bash, self.read])
        results = agent._exec_tools_parallel([
            Call("move", "bash", {"command": "cd ../other"}),
            Call("read", "read_file", {"file_path": "note.txt"}),
        ])
        self.assertIn("SUB VALUE", results[1])
        self.assertEqual(Path(get_tracked_cwd()).resolve(), self.root / "other")
        self.assertIn("OTHER VALUE", self.read.execute("note.txt"))

    def test_threads_do_not_share_path_context_or_change_process_cwd(self):
        barrier = threading.Barrier(2)

        def run(folder):
            previous = get_tracked_cwd()
            try:
                set_tracked_cwd(str(self.root / folder))
                barrier.wait(timeout=5)
                return ReadFileTool().execute("note.txt")
            finally:
                set_tracked_cwd(previous)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(run, "backend"), pool.submit(run, "other")
            self.assertIn("SUB VALUE", a.result(timeout=10))
            self.assertIn("OTHER VALUE", b.result(timeout=10))
        self.assertEqual(Path(os.getcwd()), self.root)
        self.assertEqual(Path(get_tracked_cwd()).resolve(), self.root / "backend")

    def test_child_directory_does_not_leak_into_parent(self):
        tool = AgentTool()
        parent = Agent(llm=object(), tools=[self.bash, self.read, tool])

        def offline_child_chat(child, text, **kwargs):
            child._tool_by_name["bash"].execute("cd ../other")
            return child._tool_by_name["read_file"].execute("note.txt")

        with patch.object(Agent, "chat", offline_child_chat):
            result = tool.execute(task="offline fixture")
        self.assertIn("OTHER VALUE", result)
        self.assertEqual(Path(get_tracked_cwd()).resolve(), self.root / "backend")
        self.assertIn("SUB VALUE", self.read.execute("note.txt"))
        self.assertEqual(Path(os.getcwd()), self.root)


if __name__ == "__main__":
    unittest.main()
