"""Exercise the actual CLI/session/continue flow with a fake provider only."""
import builtins
import os
import sys
from pathlib import Path

from common import dump


def main():
    root=Path(sys.argv[1]).resolve()
    os.environ['CORECODER_API_KEY']='offline-placeholder-not-a-real-key'
    os.environ['HOME']=str(root/'home')
    sys.path.insert(0,str(root/'runtime'))
    import launch
    from check import inspect

    from corecoder import cli
    from corecoder.llm import LLMResponse, ToolCall
    from corecoder.tasks import RepairTask
    class Client:
        base_url='https://api.deepseek.com'
        def with_options(self,**kw):return self
    class Fake:
        count=0
        def __init__(self,**kw):
            self.model=kw['model'];self.max_tokens=kw['max_tokens'];self.temperature=kw['temperature']
            self.client=Client()
        def chat(self,*args,**kw):
            Fake.count+=1
            if Fake.count==1:
                return LLMResponse(tool_calls=[ToolCall('read-first','read_file',
                    {'file_path':str(root/'workspace/corecoder/tools/bash.py')})],finish_reason='tool_calls',prompt_tokens=10,completion_tokens=2)
            return LLMResponse(content='Ready for verification.',finish_reason='stop',prompt_tokens=10,completion_tokens=2)
    cli.LLM=Fake
    inputs=iter([f'/repair {root / "task.json"}','quit'])
    cli.pt_prompt=lambda *a,**kw:next(inputs)
    def pause(*a,**kw):raise KeyboardInterrupt
    builtins.input=pause
    launch.launch(root)
    paused=inspect(root,'paused')
    assert paused['ledger']['model_calls']==1 and paused['pending'] is None
    sid=launch.session_id(root)
    inputs=iter(['/task','继续任务','quit'])
    def resumed_prompt(*a,**kw):
        value=next(inputs)
        if value=='/task':inspect(root,'restored')
        return value
    cli.pt_prompt=resumed_prompt
    launch.launch(root,sid)
    final=inspect(root,'finished')
    assert Fake.count==final['ledger']['model_calls']==3
    assert final['status']=='no_progress' and not final['completed']
    assert final['task_id']==paused['task_id'] and final['original_failures']==paused['original_failures']
    assert len(final['session_ids'])==1 and sid in final['session_ids']
    assert RepairTask.info(root/'task')['ledger']['attempts']==2
    dump(root/'offline-flow.json',{'real_model_calls':0,'simulated_calls':3,'paused_calls':1,
        'session_restored_before_continue':True,'ledger_unchanged_on_selection':True,
        'baseline_preserved':True,'same_task_id':True,'safe_ctrl_c':True,'stopped':'no_progress',
        'task_was_not_fixed_by_mock':True})


if __name__=='__main__':main()
