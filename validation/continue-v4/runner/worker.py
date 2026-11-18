"""Continue copied v4 history with the new regression gate; --live is explicit."""
import argparse
import copy
import json
import os
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from activation import feedback_usage
from common import digest, dump
from probe import probe


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('case',type=Path)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--live',action='store_true')
    modes.add_argument('--offline',choices=('stay','fix'))
    args = parser.parse_args()
    case = args.case.resolve()
    settings = json.loads((case/'case.json').read_text())
    output = case/'run'
    os.chdir(case/'workspace')
    sys.path.insert(0,str(case/'runtime'))
    from corecoder.agent import Agent
    from corecoder.llm import LLM, LLMResponse, ToolCall
    from corecoder.messages import clean_history
    from corecoder.permissions import Permission
    from corecoder.repair import RegressionSpec, RepairController, RepairLimits, VerificationSpec
    from corecoder.tools import ALL_TOOLS
    started = time.monotonic()
    events, count, lock = [], 0, threading.Lock()
    summary = {'live':args.live,'origin':settings['origin'],'model':settings['model'],'limits':settings['limits']}
    agent = None
    def event(record):
        events.append(record)
        with (output/'model-events.jsonl').open('a') as f: f.write(json.dumps(record)+'\n')
    try:
        frozen_baseline = json.loads(Path(settings['baseline_file']).read_text())
        baseline, initial = probe(case,output/'initial',frozen_baseline)
        initial_message = initial['feedback']['message']
        config = settings['model']
        if args.live:
            key = os.environ.get('CORECODER_API_KEY')
            if not key: raise ValueError('Missing key')
            llm = LLM(model=config['name'],api_key=key,base_url=config['base_url'],temperature=config['temperature'],
                      max_tokens=config['max_tokens'],timeout=config['request_seconds'])
            llm.client = llm.client.with_options(max_retries=0)
        else:
            class Scripted:
                total_prompt_tokens = total_completion_tokens = 0
                estimated_cost = None
                def __init__(self): self.count=0
                def chat(self,*a,**kw):
                    self.count+=1
                    if args.offline=='fix' and self.count==2:
                        # Test fixture correction only, on a disposable copy.
                        lines=len((case/'workspace/corecoder/tools/bash.py').read_text().splitlines())
                        return LLMResponse(tool_calls=[ToolCall('fixture-doc','edit_file',{
                            'file_path':str(case/'workspace/article/02-tools.md'),
                            'old_string':'`bash.py` 是 203 行','new_string':f'`bash.py` 是 {lines} 行'})],finish_reason='tool_calls')
                    return LLMResponse(content='Ready for independent checks.',finish_reason='stop')
            llm=Scripted()
        class Measured:
            def __getattr__(self,name):return getattr(llm,name)
            def chat(self,messages,**kwargs):
                nonlocal count
                with lock:
                    if count>=settings['limits']['max_model_calls']:raise RuntimeError('model_call_limit')
                    if time.monotonic()-started>=settings['limits']['max_seconds']:raise RuntimeError('time_limit')
                    count+=1
                    index=count
                    last=next((m.get('content') for m in reversed(messages) if m.get('role')=='user'),None)
                    event({'event':'requested','index':index,'last_user_message_hash':digest(last),
                           'contains_initial_feedback':isinstance(last,str) and initial_message in last})
                began=time.monotonic()
                try: response=llm.chat(messages,**kwargs)
                except BaseException as exc:
                    with lock:event({'event':'request_error','index':index,'error_type':type(exc).__name__})
                    raise
                with lock:event({'event':'response','index':index,'finish_reason':response.finish_reason,
                                  'elapsed_seconds':time.monotonic()-began,'prompt_tokens':response.prompt_tokens,
                                  'completion_tokens':response.completion_tokens})
                return response
        agent=Agent(Measured(),tools=ALL_TOOLS,max_rounds=settings['limits']['rounds_per_attempt'],
                    max_context_tokens=config['context_tokens'],permission=Permission(allow_all=True))
        agent.messages=clean_history(json.loads(Path(settings['history']).read_text()))
        summary['restored_messages']=len(agent.messages)
        class ContinuationController(RepairController):
            def _regress(self,label,timeout,baseline=None):
                if label=='baseline':
                    # The current failed delivery is never used as its own allowance.
                    return copy.deepcopy(frozen_baseline)
                return super()._regress(label,timeout,baseline)
        limits=dict(settings['limits'])
        limits['max_seconds']-=time.monotonic()-started
        if limits['max_seconds']<=0:raise TimeoutError('initial_checks_used_deadline')
        controller=ContinuationController(agent,case/'workspace',
            VerificationSpec(tuple(settings['verification_argv']),tuple(settings['protected_paths'])),
            output/'controller',RepairLimits(**limits),
            regression=RegressionSpec(tuple(settings['regression_argv']),tuple(settings['protected_paths'])))
        result=controller.run(initial_message)
        summary.update(execution_status=result.status,completed=result.completed,controller=asdict(result))
        summary['feedback_usage']=feedback_usage(asdict(result),events)
    except KeyboardInterrupt:summary.update(execution_status='interrupted',completed=False)
    except Exception as exc:  # noqa: BLE001 - record failure type without credential-bearing text
        summary.update(execution_status='execution_error',completed=False,error_type=type(exc).__name__)
    finally:
        requested=[e for e in events if e['event']=='requested' and e['contains_initial_feedback']]
        returned={e['index'] for e in events if e['event']=='response'}
        summary['initial_feedback_usage']={'requested':bool(requested),
            'response_returned':any(e['index'] in returned for e in requested),
            'request_indexes':[e['index'] for e in requested]}
        summary.update(model_calls=count,total_calls_with_v4=settings['origin']['model_calls']+count,
                       elapsed_seconds=time.monotonic()-started,
                       prompt_tokens=getattr(locals().get('llm'),'total_prompt_tokens',None),
                       completion_tokens=getattr(locals().get('llm'),'total_completion_tokens',None))
        dump(output/'worker.json',summary)
        if agent is not None:dump(output/'messages.json',agent.messages)


if __name__=='__main__':main()
