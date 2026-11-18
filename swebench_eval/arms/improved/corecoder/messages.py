"""Narrow compatibility repair for empty assistant turns, not a protocol validator."""

import logging

log = logging.getLogger(__name__)


def clean_history(messages: list[dict]) -> list[dict]:
    """Drop only assistant turns with no text AND no tool calls.

    Work on a new list; never rewrite the source session or remove tool results.
    A null content with tool_calls is a legitimate assistant turn.
    """
    if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
        raise ValueError("Session messages must be a list of objects")
    cleaned = [m for m in messages if not (
        m.get("role") == "assistant" and not m.get("tool_calls")
        and (m.get("content") is None or isinstance(m.get("content"), str) and not m["content"].strip())
    )]
    if len(cleaned) != len(messages):
        log.warning("history_repaired removed_empty_assistant=%s", len(messages) - len(cleaned))
    return cleaned
