"""Offline demo: scripted model writes a wrong answer, then fixes it after verification."""
import argparse
import json
import sys
from pathlib import Path

from corecoder import checkpoints
from corecoder.agent import Agent
from corecoder.llm import LLMResponse, ToolCall
from corecoder.repair import RepairController, RepairLimits, VerificationSpec
from corecoder.tools.edit import _changed_files
from corecoder.tools.write import WriteFileTool


def run_demo(output):
    # Keep the demo's undo records out of the real user's home directory.
    old = checkpoints.CHECKPOINTS_FILE, checkpoints._stack, checkpoints._loaded
    changed = _changed_files.copy()
    try:
        checkpoints.CHECKPOINTS_FILE = Path(output).resolve() / "checkpoints.json"
        checkpoints._stack, checkpoints._loaded = [], False
        return _run_demo(output)
    finally:
        checkpoints.CHECKPOINTS_FILE, checkpoints._stack, checkpoints._loaded = old
        _changed_files.clear()
        _changed_files.update(changed)


def _run_demo(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    workspace = output / "workspace"
    workspace.mkdir()
    target = workspace / "answer.txt"
    verifier = output / "verify.py"
    verifier.write_text("from pathlib import Path\nassert Path('answer.txt').read_text() == 'fixed', 'answer must be fixed'\n")

    class ScriptedModel:
        def __init__(self):
            self.responses = iter([
                LLMResponse(tool_calls=[ToolCall("wrong", "write_file", {"file_path": str(target), "content": "wrong"})]),
                LLMResponse(content="First attempt ready"),
                LLMResponse(tool_calls=[ToolCall("fix", "write_file", {"file_path": str(target), "content": "fixed"})]),
                LLMResponse(content="Second attempt ready"),
            ])

        def chat(self, messages, **kwargs):
            return next(self.responses)

    agent = Agent(ScriptedModel(), tools=[WriteFileTool()])
    result = RepairController(agent, workspace, VerificationSpec((sys.executable, str(verifier)),
                              protected_paths=(str(verifier),)), output / "result",
                              RepairLimits(rounds_per_attempt=2)).run("Fix the answer")
    print(json.dumps({"status": result.status, "attempts": len(result.attempts),
                      "verification": [a["verification"]["status"] for a in result.attempts],
                      "model_calls": result.model_calls, "result": str(output / "result/result.json")}, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="A new directory; existing evidence will never be overwritten")
    run_demo(parser.parse_args().output)
