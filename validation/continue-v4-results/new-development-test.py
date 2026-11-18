"""Working-directory tracking for composed shell commands.

`cd` only moves the shell when its statement really runs, and the exit status
of the whole command line says nothing about which statements ran. These tests
pin the two failure shapes down: a `cd` that ran before a later failure must be
remembered, and a `cd` skipped by `&&` short-circuiting must not be.
"""

import os
import sys

import pytest

from corecoder.tools.bash import BashTool, get_tracked_cwd, set_tracked_cwd


def same_dir(a: str | None, b) -> bool:
    """Compare two paths as directories (symlinks and spellings may differ)."""
    return a is not None and os.path.realpath(a) == os.path.realpath(str(b))


@pytest.fixture
def shell(tmp_path):
    """A sandbox with `sub`, `a/b` and a space-bearing dir, tracked as root."""
    for rel in ("sub", "a/b", "my dir"):
        (tmp_path / rel).mkdir(parents=True)
    bash = BashTool()
    prev = get_tracked_cwd()
    set_tracked_cwd(str(tmp_path))
    try:
        yield bash, tmp_path
    finally:
        set_tracked_cwd(prev)


# --- the two reproductions ---

def test_cd_succeeds_then_command_fails_still_moves(shell):
    """`cd sub && false` fails overall, but the shell did move to sub."""
    bash, root = shell
    out = bash.execute(command="cd sub && false")
    assert same_dir(get_tracked_cwd(), root / "sub")
    # the user-visible contract is untouched: failed status, no internal noise
    assert "[exit code: 1]" in out
    assert str(root) not in out
    # ...and the next call really runs there
    assert os.path.realpath(str(root / "sub")) in os.path.realpath(
        bash.execute(command="pwd").strip()
    )


def test_skipped_cd_is_not_tracked(shell):
    """`false && cd sub; true` succeeds overall, but the cd never ran."""
    bash, root = shell
    out = bash.execute(command="false && cd sub; true")
    assert same_dir(get_tracked_cwd(), root)
    assert "[exit code" not in out
    assert os.path.realpath(str(root)) == os.path.realpath(bash.execute(command="pwd").strip())


def test_second_of_two_cds_skipped_by_failure(shell):
    """A chain that moved once and then short-circuits keeps the first move."""
    bash, root = shell
    bash.execute(command="cd a && false && cd b")
    assert same_dir(get_tracked_cwd(), root / "a")


# --- chains ---

def test_multiple_cds_resolve_sequentially(shell):
    """`cd a && cd b` lands in a/b, each target relative to the previous one."""
    bash, root = shell
    bash.execute(command="cd a && cd b")
    assert same_dir(get_tracked_cwd(), root / "a" / "b")


def test_later_cd_failure_keeps_reached_dir(shell):
    """A failing `cd` at the end of a chain leaves the shell where it was."""
    bash, root = shell
    out = bash.execute(command="cd a; cd nope")
    assert same_dir(get_tracked_cwd(), root / "a")
    assert "[exit code: 1]" in out


def test_failing_cd_does_not_move(shell):
    """`cd` to a missing dir fails and the tracked dir must not change."""
    bash, root = shell
    out = bash.execute(command="cd nope && pwd")
    assert same_dir(get_tracked_cwd(), root)
    assert "[exit code" in out


# --- what must not be mistaken for a cd ---

def test_subshell_cd_does_not_leak(shell):
    """`( cd sub )` only moves a subshell, so later calls stay put."""
    bash, root = shell
    bash.execute(command="( cd sub ) && pwd")
    assert same_dir(get_tracked_cwd(), root)
    assert os.path.realpath(str(root)) == os.path.realpath(bash.execute(command="pwd").strip())


def test_quoted_cd_text_is_not_a_command(shell):
    """`echo "cd sub"` prints text; it must not be read as a directory change."""
    bash, root = shell
    out = bash.execute(command='echo "cd sub"')
    assert same_dir(get_tracked_cwd(), root)
    assert "cd sub" in out


def test_quoted_dir_with_spaces(shell):
    """A space-bearing path is one target, and later calls run inside it."""
    bash, root = shell
    bash.execute(command='cd "my dir"')
    assert same_dir(get_tracked_cwd(), root / "my dir")
    assert os.path.realpath(str(root / "my dir")) in os.path.realpath(
        bash.execute(command="pwd").strip()
    )


# --- preserved output contract ---

def test_output_streams_and_status_are_preserved(shell):
    """stdout, stderr and the exit code keep their old shape and separators."""
    bash, root = shell
    out = bash.execute(command='cd sub && echo out; echo err >&2; false')
    assert "out" in out
    assert "[stderr]" in out and "err" in out
    assert "[exit code: 1]" in out
    # nothing about the tracked directory is injected into the transcript
    assert "corecoder-cwd" not in out
    assert same_dir(get_tracked_cwd(), root / "sub")


def test_no_probe_files_are_left_behind(shell):
    """The private channel that reports the final dir is cleaned up."""
    import tempfile

    bash, _ = shell
    before = set(os.listdir(tempfile.gettempdir()))
    bash.execute(command="cd sub && false")
    leftovers = {p for p in set(os.listdir(tempfile.gettempdir())) - before
                 if p.startswith("corecoder-cwd-")}
    assert leftovers == set()


def test_timeout_and_dangerous_checks_still_apply(shell):
    """The new tracking must not swallow the timeout or safety paths."""
    bash, root = shell
    assert "timed out" in bash.execute(command="sleep 5", timeout=1)
    assert "Blocked" in bash.execute(command="rm -rf /")
    assert "exit code: 42" in bash.execute(command="exit 42")


def test_truncation_still_happens(shell):
    bash, _ = shell
    out = bash.execute(command=f'"{sys.executable}" -c "print(chr(120) * 20000)"')
    assert "truncated" in out


# --- process-wide and batch contracts ---

def test_global_process_cwd_is_untouched(shell):
    """Tracking lives in the thread-local, never in os.chdir."""
    bash, _ = shell
    before = os.getcwd()
    bash.execute(command="cd sub && false")
    assert os.getcwd() == before


def test_parallel_batch_merges_failed_command_move(tmp_path):
    """A batch's cd survives even when that call exits non-zero."""
    from corecoder.agent import Agent
    from corecoder.llm import LLM

    target = tmp_path / "proj"
    target.mkdir()
    bash = BashTool()
    agent = Agent(llm=LLM.__new__(LLM), tools=[bash])

    class _TC:
        def __init__(self, i, cmd):
            self.name, self.id, self.arguments = "bash", str(i), {"command": cmd}

    prev = get_tracked_cwd()
    set_tracked_cwd(str(tmp_path))
    try:
        results = agent._exec_tools_parallel([_TC(1, "cd proj && false"), _TC(2, "pwd")])
        assert "[exit code: 1]" in results[0]
        # siblings still start from the pre-batch dir (parallel, not serial)
        assert os.path.realpath(str(tmp_path)) == os.path.realpath(results[1].strip())
        # but the move itself is handed to the session
        assert same_dir(get_tracked_cwd(), target)
    finally:
        set_tracked_cwd(prev)
