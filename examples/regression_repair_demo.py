"""Offline: fix the target, detect a broken old test, then repair the regression."""
import argparse
import json
import sys
from pathlib import Path

from corecoder import checkpoints
from corecoder.agent import Agent
from corecoder.llm import LLMResponse, ToolCall
from corecoder.repair import RegressionSpec, RepairController, RepairLimits, VerificationSpec
from corecoder.tools.edit import _changed_files
from corecoder.tools.write import WriteFileTool


def run_demo(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    workspace = output / 'workspace'
    workspace.mkdir()
    (workspace / 'answer.txt').write_text('wrong')
    (workspace / 'legacy.txt').write_text('ok')
    (workspace / 'tests').mkdir()
    (workspace / 'tests/test_old.py').write_text(
        "from pathlib import Path\ndef test_legacy():\n assert Path('legacy.txt').read_text() == 'ok', 'legacy broke'\n")
    verifier = output / 'accept.py'
    verifier.write_text("from pathlib import Path\nassert Path('answer.txt').read_text() == 'fixed'\n")

    def write(call_id, name, content):
        return ToolCall(call_id, 'write_file', {'file_path': str(workspace / name), 'content': content})

    class ScriptedModel:
        def __init__(self):
            self.responses = iter([
                LLMResponse(tool_calls=[write('target', 'answer.txt', 'fixed'), write('break', 'legacy.txt', 'broken')]),
                LLMResponse(tool_calls=[write('restore', 'legacy.txt', 'ok')]),
            ])

        def chat(self, messages, **kwargs):
            return next(self.responses)

    old = checkpoints.CHECKPOINTS_FILE, checkpoints._stack, checkpoints._loaded
    changed = _changed_files.copy()
    try:
        checkpoints.CHECKPOINTS_FILE = output / 'checkpoints.json'
        checkpoints._stack, checkpoints._loaded = [], False
        result = RepairController(
            Agent(ScriptedModel(), tools=[WriteFileTool()]), workspace,
            VerificationSpec((sys.executable, str(verifier)), (str(verifier),)), output / 'result',
            RepairLimits(rounds_per_attempt=1),
            regression=RegressionSpec((sys.executable, '-m', 'pytest', 'tests', '-q')),
        ).run('Fix answer.txt while preserving legacy behavior')
    finally:
        checkpoints.CHECKPOINTS_FILE, checkpoints._stack, checkpoints._loaded = old
        _changed_files.clear()
        _changed_files.update(changed)
    print(json.dumps({'status': result.status, 'completed': result.completed,
                      'acceptance': result.acceptance_status, 'regression': result.regression_status,
                      'model_calls': result.model_calls, 'attempts': len(result.attempts),
                      'result': str(output / 'result/result.json')}, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='New output directory; never overwrite an old run')
    run_demo(parser.parse_args().output)
