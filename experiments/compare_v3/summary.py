"""Read existing results only; never execute a model or verifier."""
import json
from pathlib import Path

from common import dump

SUITE = Path('/tmp/corecoder-compare-03')


def main():
    rows = []
    for task in ('task04',):
        for arm in ('generic', 'staged'):
            path = SUITE / f'{task}-{arm}' / 'run/summary.json'
            if not path.exists():
                rows.append({'task': task, 'arm': arm, 'status': 'not_run'})
                continue
            result = json.loads(path.read_text())
            execution = result['execution']
            events_path = path.parent / 'model-events.jsonl'
            events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.exists() else []
            requests = [e for e in events if e['event'] == 'requested']
            rows.append({'task': task, 'arm': arm, 'status': execution.get('execution_status'),
                         'functional_passed': result['functional_passed'], 'live': result['live'],
                         'model_calls_requested': len(requests), 'prompt_tokens': execution.get('prompt_tokens'),
                         'completion_tokens': execution.get('completion_tokens'),
                         'initial_request_hash': requests[0]['request_hash_normalized'] if requests else None,
                         'result': str(path)})
    matches = {}
    for task in ('task04',):
        pair = [row.get('initial_request_hash') for row in rows if row['task'] == task]
        matches[task] = pair[0] == pair[1] if all(pair) else None
    result = {'results': rows, 'initial_requests_match': matches}
    dump(SUITE / 'comparison-summary.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
