"""Summarize a continuation without executing checks or models."""
import json
from pathlib import Path

from common import dump

SUITE=Path('/tmp/corecoder-resume-01')


def main():
    path=SUITE/'task04-staged/run/summary.json'
    if not path.exists():
        result={'status':'not_run','source':'v4 staged failed delivery'}
    else:
        record=json.loads(path.read_text()); ex=record['execution']
        controller=ex.get('controller',{})
        result={'functional_passed':record['functional_passed'],'execution':ex.get('execution_status'),
                'completed':ex.get('completed'),'acceptance':record['acceptance']['status'],
                'regression':record['regression']['status'],'new_regression_failures':record['regression'].get('new_failures'),
                'new_model_calls':ex.get('model_calls'),'v4_model_calls':ex.get('origin',{}).get('model_calls'),
                'total_calls_with_v4':ex.get('total_calls_with_v4'),'prompt_tokens':ex.get('prompt_tokens'),
                'completion_tokens':ex.get('completion_tokens'),'elapsed_seconds':ex.get('elapsed_seconds'),
                'attempts':len(controller.get('attempts',[])),
                'initial_feedback_usage':ex.get('initial_feedback_usage'),'retry_feedback_usage':ex.get('feedback_usage'),
                'result':str(path)}
    dump(SUITE/'continuation-summary.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
