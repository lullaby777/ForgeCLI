"""Isolated execution process. Live calls require an explicit --live flag."""
import argparse
import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from common import dump


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("case", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    case = args.case.resolve()
    settings = json.loads((case / "case.json").read_text())
    workspace, runtime = case / "workspace", case / "runtime"
    output = case / "run"
    os.chdir(workspace)
    sys.path.insert(0, str(runtime))
    from corecoder.agent import Agent
    from corecoder.llm import LLM, LLMResponse
    from corecoder.permissions import Permission
    from corecoder.tools import ALL_TOOLS
    from corecoder.tools.bash import set_tracked_cwd

    config, limits = settings["model"], settings["limits"]
    if args.live:
        key = os.environ.get("CORECODER_API_KEY")
        if not key:
            raise SystemExit("Set CORECODER_API_KEY before starting a live run")
        llm = LLM(model=config["name"], api_key=key, base_url=config["base_url"],
                  temperature=config["temperature"], max_tokens=config["max_tokens"], timeout=config["request_seconds"])
        llm.client = llm.client.with_options(max_retries=0)
    else:
        class OfflineModel:
            total_prompt_tokens = total_completion_tokens = 0
            estimated_cost = None

            def __init__(self):
                self.count = 0

            def chat(self, *a, **kw):
                self.count += 1
                return LLMResponse(finish_reason="length")
        llm = OfflineModel()

    deadline = time.monotonic() + limits["max_seconds"]
    count = 0
    lock = threading.Lock()
    def event(data):
        with (output / "model-events.jsonl").open("a") as stream:
            stream.write(json.dumps(data) + "\n")

    class MeasuredModel:
        def __getattr__(self, name):
            return getattr(llm, name)

        def chat(self, messages, **kwargs):
            nonlocal count
            with lock:
                if time.monotonic() >= deadline:
                    raise RuntimeError("experiment_time_limit")
                if count >= limits["max_model_calls"]:
                    raise RuntimeError("experiment_model_call_limit")
                count += 1
                index = count
                normalized = json.dumps({"messages": messages, "tools": kwargs.get("tools")}, ensure_ascii=False, sort_keys=True)
                for prefix in (str(case), str(args.case), str(case).replace('/private/tmp/', '/tmp/')):
                    normalized = normalized.replace(prefix, '<CASE>')
                event({"event": "requested", "index": index,
                       "request_hash_normalized": hashlib.sha256(normalized.encode()).hexdigest()})
            started = time.monotonic()
            try:
                response = llm.chat(messages, **kwargs)
            except BaseException as exc:
                with lock:
                    event({"event": "request_error", "index": index, "error_type": type(exc).__name__})
                raise
            with lock:
                event({"event": "response", "index": index, "elapsed_seconds": time.monotonic() - started,
                       "prompt_tokens": getattr(response, "prompt_tokens", None),
                       "completion_tokens": getattr(response, "completion_tokens", None),
                       "finish_reason": getattr(response, "finish_reason", None)})
            return response

    model = MeasuredModel()
    agent = Agent(model, tools=ALL_TOOLS, max_rounds=limits["rounds_per_attempt"],
                  max_context_tokens=config["context_tokens"], permission=Permission(allow_all=True))
    set_tracked_cwd(str(workspace))
    task = (case / "task.md").read_text()
    summary = {"arm": settings["arm"], "model": config, "limits": limits,
               "behavior": settings.get("behavior"), "recovery_strategy": settings["recovery_strategy"], "live": args.live}
    started = time.monotonic()
    try:
        from corecoder import repair
        from corecoder.repair import RepairController, RepairLimits, VerificationSpec
        if settings["recovery_strategy"] == "generic":
            # Only replace feedback construction. Both arms run identical
            # controller, facts, verification, limits and stop rules.
            def generic_feedback(original_task, facts, verification):
                message = (f"Original task: {original_task}\nIndependent verification failed. Continue without changing "
                           "acceptance criteria or protected tests.\n" + verification["feedback"])
                return {"kind": "generic", "next_action": "Continue from verification failure",
                        "failure_excerpt": verification["feedback"], "message": message, "message_chars": len(message)}
            repair._recovery_feedback = generic_feedback
        elif settings["recovery_strategy"] != "staged":
            raise ValueError("Unknown recovery strategy")
        spec = VerificationSpec(tuple(settings["verification_argv"]), tuple(settings["protected_paths"]))
        result = RepairController(agent, workspace, spec, output / "controller", RepairLimits(**limits)).run(task)
        summary.update(execution_status=result.status, controller=asdict(result), recovery_strategy=settings["recovery_strategy"])
    except KeyboardInterrupt:
        summary.update(execution_status="interrupted")
    except Exception as exc:  # noqa: BLE001 - record failure without logging credentials from exception text
        summary.update(execution_status="execution_error", error_type=type(exc).__name__)
    finally:
        summary.update(model_calls=count, elapsed_seconds=time.monotonic() - started,
                       prompt_tokens=getattr(llm, "total_prompt_tokens", None),
                       completion_tokens=getattr(llm, "total_completion_tokens", None),
                       estimated_cost_usd=getattr(llm, "estimated_cost", None))
        dump(output / "worker.json", summary)
        dump(output / "messages.json", agent.messages)


if __name__ == "__main__":
    main()
