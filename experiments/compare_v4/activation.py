"""Observe feedback submission separately from actual model entry and return."""
from common import digest


def feedback_usage(controller, events):
    attempts = controller.get('attempts', [])
    requested = [e for e in events if e['event'] == 'requested']
    responses = {e['index'] for e in events if e['event'] == 'response'}
    rows = []
    for pos, attempt in enumerate(attempts):
        recovery = attempt.get('recovery')
        if not recovery:
            continue
        following = attempts[pos+1] if pos+1 < len(attempts) else {}
        lower = following.get('model_calls_before', 0)
        upper = following.get('model_calls_after', lower)
        matching = [e['index'] for e in requested
                    if following and lower < e['index'] <= upper
                    and e.get('last_user_message_hash') == digest(recovery['message'])]
        rows.append({'source_attempt': attempt['number'], 'kind': recovery['kind'],
                     'message_hash': digest(recovery['message']),
                     'submitted_to_agent': bool(attempt.get('feedback_submitted_to_agent', False)),
                     'model_requested_with_feedback': bool(matching),
                     'response_returned_with_feedback': bool(set(matching) & responses),
                     'request_indexes': matching,
                     'response_indexes': sorted(set(matching) & responses),
                     'next_attempt': following.get('number'),
                     'next_attempt_model_calls': (following.get('model_calls_after', 0) - following.get('model_calls_before', 0)) if following else 0,
                     'next_verification': following.get('verification', {}).get('status'),
                     'next_files_changed': following.get('facts', {}).get('files_changed')})
    return {'definition': 'Used = model entry requested with this feedback as the latest user message; not proof of model comprehension.',
            'feedback_generated_count': len(rows),
            'feedback_submitted_count': sum(r['submitted_to_agent'] for r in rows),
            'feedback_used_count': sum(r['model_requested_with_feedback'] for r in rows),
            'feedback_response_count': sum(r['response_returned_with_feedback'] for r in rows),
            'records': rows}
