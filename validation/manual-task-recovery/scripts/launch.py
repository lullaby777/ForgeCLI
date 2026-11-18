"""Isolated interactive launch, with one explicit safe pause window for this demo."""
import argparse
import json
import os
import sys
from pathlib import Path

from common import digest, manifest

ROOT=Path(__file__).resolve().parents[1]


def session_id(root):
    matches=[]
    for p in (root/'home/.corecoder/sessions').glob('*.json'):
        data=json.loads(p.read_text())
        if data.get('task_checkpoint') and Path(data['task_checkpoint']).resolve()==(root/'task').resolve():
            matches.append(data['id'])
    if len(matches)!=1:
        raise ValueError('Expected exactly one linked task session; use --resume ID explicitly')
    return matches[0]


def launch(root=ROOT, resume=None):
    root=root.resolve()
    seal=json.loads((root/'seal.json').read_text())
    assert manifest(root/'runtime')==seal['runtime'], 'Runtime changed'
    assert manifest(root/'scripts')==seal['scripts'], 'Launch scripts changed'
    assert digest((root/'task.json').read_text())==seal['config'], 'Task configuration changed'
    assert manifest(root/'evaluation')==seal['evaluation'], 'Acceptance changed'
    if not (root/'task').exists():
        assert manifest(root/'workspace')==seal['workspace'], 'Initial workspace changed'
    if not os.environ.get('CORECODER_API_KEY'):
        raise SystemExit('Set CORECODER_API_KEY in this terminal. No task/model request has started.')
    os.environ.update(HOME=str(root/'home'),CORECODER_MODEL='deepseek-flash',OPENAI_BASE_URL='https://api.deepseek.com',
        CORECODER_MAX_TOKENS='8192',CORECODER_TEMPERATURE='0',CORECODER_MAX_CONTEXT='128000',CORECODER_PROVIDER='openai')
    os.chdir(root/'workspace')
    sys.path.insert(0,str(root/'runtime'))
    from corecoder import cli, config
    from corecoder.tasks import RepairTask
    config._load_dotenv=lambda:None  # explicit environment only, no unrelated .env
    model_class=cli.LLM
    def configured_model(*args,**kwargs):
        kwargs['timeout']=60
        model=model_class(*args,**kwargs)
        model.client=model.client.with_options(max_retries=0)
        return model
    cli.LLM=configured_model
    original=RepairTask._hook
    def pause_once(self,event,**fields):
        original(self,event,**fields)  # journal first; no model/tool is still in flight
        if event not in ('tools_finished','response_saved') or self.state['ledger']['model_calls']<1:
            return
        marker=root/'control/safe-pause.json'
        try:
            with marker.open('x') as f:
                json.dump({'task_id':self.state['id'],'calls':self.state['ledger']['model_calls'],
                           'event':event,'pending':self.state['pending']},f)
        except FileExistsError:
            return
        print('\n【安全暂停窗口】响应和工具结果已经保存，现在没有在途操作。')
        print('请现在按一次 Ctrl+C；回到 You > 后输入 /task 查看，再输入 quit 退出。')
        input('请按 Ctrl+C（不要回车）：')
    RepairTask._hook=pause_once
    if resume:
        print(f'Opening saved session {resume}. 输入“继续任务”才恢复执行。')
    else:
        print(f'进入后输入：/repair {root / "task.json"}')
    previous=sys.argv
    sys.argv=['corecoder','--yes']+(['-r',resume] if resume else [])
    try:
        cli.main()
    finally:
        sys.argv=previous
        RepairTask._hook=original
        cli.LLM=model_class


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--resume')
    mode.add_argument('--resume-last',action='store_true')
    args=parser.parse_args()
    launch(resume=session_id(ROOT) if args.resume_last else args.resume)


if __name__=='__main__':main()
