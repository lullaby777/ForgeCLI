"""Read-only checkpoint inspection; optional append-only milestone snapshots."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'runtime'))
from corecoder.task_store import read_record


def inspect(root=ROOT, snapshot=None):
    root=root.resolve()
    state_path=root/'task/state.json'
    if not state_path.exists():
        result={'status':'not_started','task_created':False}
        print(json.dumps(result,ensure_ascii=False,indent=2));return result
    state=read_record(state_path)
    baseline_path=root/'task/baseline.json'
    baseline=read_record(baseline_path)
    failures=baseline['result']['failures']
    result={'task_id':state['id'],'status':state['status'],'phase':state['phase'],'completed':state['completed'],
        'ledger':state['ledger'],'limits':state['config']['limits'],'config_hash':state['config_hash'],
        'baseline_hash':state['baseline_hash'],'baseline_file_sha256':hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        'original_failures':failures,'pending':state['pending'],
        'acceptance':state['checks'].get('acceptance',{}).get('status'),
        'regression':state['checks'].get('regression',{}).get('status'),
        'new_failures':state['checks'].get('regression',{}).get('new_failures'),
        'session_ids':[]}
    for p in (root/'home/.corecoder/sessions').glob('*.json'):
        data=json.loads(p.read_text())
        if data.get('task_checkpoint') and Path(data['task_checkpoint']).resolve()==(root/'task').resolve():
            result['session_ids'].append(data['id'])
    expected=json.loads((root/'preflight/baseline.json').read_text())['failures']
    assert failures==expected,'Original failure list differs from the prepared clean baseline'
    for name in ('model_calls','attempts'):
        limit='max_model_calls' if name=='model_calls' else 'max_attempts'
        assert 0<=state['ledger'][name]<=state['config']['limits'][limit]
    assert 0<=state['ledger']['rounds']<=state['config']['limits']['rounds_per_attempt']
    result['baseline_matches_preflight']=True
    if snapshot:
        if snapshot=='paused':
            assert state['status']=='paused' and state['pending'] is None,'Not a clean Ctrl+C pause; inspect before continuing'
            assert state['ledger']['model_calls']>=1
        else:
            before=json.loads((root/'records/paused.json').read_text())
            for key in ('task_id','config_hash','baseline_hash','baseline_file_sha256','original_failures'):
                assert result[key]==before[key],key+' changed across recovery'
            if snapshot=='restored':
                assert result['ledger']==before['ledger'],'Selecting a session must not change the task ledger'
            else:
                for key in ('model_calls','attempts','active_seconds','stagnant'):
                    # Stagnation may reset only when real progress occurs.
                    if key!='stagnant':assert result['ledger'][key]>=before['ledger'][key],key+' reset'
                if result['ledger']['attempts']==before['ledger']['attempts']:
                    assert result['ledger']['rounds']>=before['ledger']['rounds']
            result['comparison_to_paused']='passed'
        with (root/f'records/{snapshot}.json').open('x') as f:
            json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',choices=('paused','restored','finished'))
    args=parser.parse_args()
    inspect(snapshot=args.snapshot)
