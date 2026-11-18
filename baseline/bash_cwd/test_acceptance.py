"""Real local-shell acceptance; temporary files only, no model calls."""
import concurrent.futures
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(os.environ.get("CORECODER_TARGET", Path(__file__).resolve().parents[2])).resolve()
sys.path.insert(0, str(ROOT))
from corecoder.agent import Agent
from corecoder.tools.bash import BashTool, get_tracked_cwd, set_tracked_cwd


class BashCwdAcceptance(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="corecoder-cwd-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "sub" / "nested").mkdir(parents=True)
        (self.root / "other").mkdir()
        (self.root / "space dir").mkdir()
        previous = get_tracked_cwd()
        self.addCleanup(set_tracked_cwd, previous)
        set_tracked_cwd(str(self.root))
        self.bash = BashTool()

    def assert_directory(self, expected):
        self.assertEqual(Path(get_tracked_cwd()).resolve(), expected.resolve())
        self.assertEqual(Path(self.bash.execute("pwd").strip()).resolve(), expected.resolve())

    def test_successful_cd_followed_by_failed_and_command(self):
        result = self.bash.execute("cd sub && false")
        self.assertIn("[exit code: 1]", result)
        self.assert_directory(self.root / "sub")

    def test_successful_cd_followed_by_failed_semicolon_command(self):
        result = self.bash.execute("cd sub; false")
        self.assertIn("[exit code: 1]", result)
        self.assert_directory(self.root / "sub")

    def test_skipped_cd_with_failed_overall_status(self):
        result = self.bash.execute("false && cd sub")
        self.assertIn("[exit code: 1]", result)
        self.assert_directory(self.root)

    def test_skipped_cd_with_successful_overall_status(self):
        self.bash.execute("false && cd sub; true")
        self.assert_directory(self.root)

    def test_failed_cd_does_not_enable_later_and_cd(self):
        self.bash.execute("cd missing-directory && cd sub; true")
        self.assert_directory(self.root)

    def test_nested_changes_survive_later_failure(self):
        result = self.bash.execute("cd sub && cd nested && false")
        self.assertIn("[exit code: 1]", result)
        self.assert_directory(self.root / "sub" / "nested")

    def test_later_cd_failure_preserves_earlier_success(self):
        self.bash.execute("cd sub && cd missing-directory")
        self.assert_directory(self.root / "sub")

    def test_successful_chain_still_works(self):
        self.bash.execute("cd sub && cd nested")
        self.assert_directory(self.root / "sub" / "nested")

    def test_subshell_does_not_change_parent_directory(self):
        self.bash.execute("(cd sub && false); true")
        self.assert_directory(self.root)

    def test_quoted_text_is_not_executed_cd(self):
        self.bash.execute("printf '%s' 'cd sub; cd other'")
        self.assert_directory(self.root)

    def test_directory_with_spaces_and_failed_tail(self):
        self.bash.execute('cd "space dir" && false')
        self.assert_directory(self.root / "space dir")

    def test_stdout_stderr_and_exit_status_preserved(self):
        result = self.bash.execute("cd sub; printf 'visible-out'; printf 'visible-err' >&2; false")
        self.assertIn("visible-out", result)
        self.assertIn("visible-err", result)
        self.assertIn("[exit code: 1]", result)
        # Internal cwd reporting must not appear in ordinary successful output.
        self.assertEqual(self.bash.execute("printf 'payload-only'"), "payload-only")

    def test_thread_directories_do_not_cross_or_change_process_cwd(self):
        barrier = threading.Barrier(2)
        process_cwd = os.getcwd()

        def run(target):
            prior = get_tracked_cwd()
            try:
                set_tracked_cwd(str(self.root))
                result = BashTool().execute(f"cd {target} && false")
                barrier.wait(timeout=5)
                return result, Path(get_tracked_cwd()).resolve(), BashTool().execute("pwd")
            finally:
                set_tracked_cwd(prior)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, target) for target in ("sub", "other")]
            for target, future in zip(("sub", "other"), futures):
                result, recorded, actual = future.result(timeout=10)
                self.assertIn("[exit code: 1]", result)
                self.assertEqual(recorded, self.root / target)
                self.assertEqual(Path(actual.strip()).resolve(), self.root / target)
        self.assertEqual(os.getcwd(), process_cwd)
        self.assert_directory(self.root)

    def test_parallel_agent_batch_inherits_and_merges_failed_cd(self):
        # Execute the original tool batch directly, without invoking Agent.chat.
        agent = Agent(llm=object(), tools=[self.bash])

        class Call:
            name = "bash"

            def __init__(self, ident, command):
                self.id = ident
                self.arguments = {"command": command}

        result = agent._exec_tools_parallel([Call("move", "cd sub && false"), Call("read", "pwd")])
        self.assertIn("[exit code: 1]", result[0])
        self.assertEqual(Path(result[1].strip()).resolve(), self.root)
        self.assert_directory(self.root / "sub")


if __name__ == "__main__":
    unittest.main()
