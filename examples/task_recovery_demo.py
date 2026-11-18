"""Offline task checkpoint demo: pause with a regression, then resume its budget."""
import argparse
import json
import sys
from pathlib import Path

from corecoder import checkpoints
from corecoder.agent import Agent
from corecoder.llm import LLMResponse, ToolCall
from corecoder.repair import RegressionSpec, RepairLimits, VerificationSpec
from corecoder.tasks import RepairTask
from corecoder.tools.edit import _changed_files
from corecoder.tools.write import WriteFileTool


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)

    def chat(self, *args, **kwargs):
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


def run_demo(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    workspace = directory / 'workspace'
    workspace.mkdir()
    (workspace / 'answer.txt').write_text('wrong')
    (workspace / 'legacy.txt').write_text('ok')
    (workspace / 'tests').mkdir()
    (workspace / 'tests/test_old.py').write_text(
        "from pathlib import Path\ndef test_legacy():\n assert Path('legacy.txt').read_text() == 'ok'\n")
    verifier = directory / 'accept.py'
    verifier.write_text("from pathlib import Path\nassert Path('answer.txt').read_text() == 'fixed'\n")
    def write(call_id, name, content):
        return ToolCall(call_id, 'write_file', {'file_path': str(workspace / name), 'content': content})
    old = checkpoints.CHECKPOINTS_FILE, checkpoints._stack, checkpoints._loaded
    changed = _changed_files.copy()
    try:
        checkpoints.CHECKPOINTS_FILE = directory / 'undo.json'
        checkpoints._stack, checkpoints._loaded = [], False
        first_model = ScriptedModel([
            LLMResponse(tool_calls=[write('target', 'answer.txt', 'fixed'), write('regression', 'legacy.txt', 'broken')]),
            KeyboardInterrupt(),
        ])
        task = RepairTask.create(Agent(first_model, tools=[WriteFileTool()]), directory / 'task', 'Fix answer and preserve legacy',
                                 workspace, VerificationSpec((sys.executable, str(verifier))),
                                 RegressionSpec((sys.executable, '-m', 'pytest', 'tests', '-q')),
                                 RepairLimits(rounds_per_attempt=2))
        paused = task.run()
        second_model = ScriptedModel([
            LLMResponse(tool_calls=[write('repair', 'legacy.txt', 'ok')]), LLMResponse(content='ready'),
        ])
        done = RepairTask(Agent(second_model, tools=[WriteFileTool()]), directory / 'task').run()
        assert paused['status'] == 'paused' and done['completed']
        assert paused['ledger']['model_calls'] == 2 and done['ledger']['model_calls'] == 4
        assert done['ledger']['attempts'] == 2
        assert done['checks']['acceptance']['status'] == done['checks']['regression']['status'] == 'passed'
        print(json.dumps({'paused_calls': paused['ledger']['model_calls'], 'final_calls': done['ledger']['model_calls'],
                          'attempts': done['ledger']['attempts'], 'completed': done['completed'],
                          'checkpoint': str(directory / 'task/state.json'), 'real_model_calls': 0}, indent=2))
        return done
    finally:
        checkpoints.CHECKPOINTS_FILE, checkpoints._stack, checkpoints._loaded = old
        _changed_files.clear()
        _changed_files.update(changed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='New directory; never overwrite previous evidence')
    run_demo(parser.parse_args().directory)
