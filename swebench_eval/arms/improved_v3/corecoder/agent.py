"""Core agent loop.

This is the heart of CoreCoder.  The pattern is simple:

    user message -> LLM (with tools) -> tool calls? -> execute -> loop
                                      -> text reply? -> return to user

A text response ends this turn, not independent task verification. Empty
responses and execution limits have explicit outcomes instead of success.
"""

import concurrent.futures
import inspect
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .context import ContextManager
from .llm import LLM
from .messages import clean_history
from .permissions import Permission
from .prompt import PLAN_MODE_PROMPT, system_prompt
from .tools import ALL_TOOLS
from .tools.agent import AgentTool
from .tools.base import Tool
from .tools.todo import TodoWriteTool

log = logging.getLogger(__name__)
_EVENT_LOCK = threading.Lock()


@dataclass
class RunResult:
    """Execution outcome only: a response is not proof that the task passed."""

    status: str = "idle"
    rounds: int = 0
    empty_retries: int = 0
    finish_reason: str | None = None
    error_type: str | None = None


class Agent:
    def __init__(
        self,
        llm: LLM,
        tools: list[Tool] | None = None,
        max_context_tokens: int = 128_000,
        max_rounds: int = 50,
        permission=None,
        hooks=None,
        max_empty_retries: int = 1,
        event_log_path: str | Path | None = None,
        execution_check=None,
    ):
        self.llm = llm
        self.tools = tools if tools is not None else ALL_TOOLS
        self.permission = permission
        self.hooks = hooks
        self._tool_by_name = {t.name: t for t in self.tools}
        self.messages: list[dict] = []
        self.context = ContextManager(max_tokens=max_context_tokens)
        self.max_rounds = max_rounds
        self._system = system_prompt(self.tools)
        self.plan_mode = False  # toggled by /plan; while on, mutating tools are refused
        if not isinstance(max_empty_retries, int) or max_empty_retries < 0:
            raise ValueError("max_empty_retries must be a non-negative integer")
        self.max_empty_retries = max_empty_retries
        self.event_log_path = Path(event_log_path) if event_log_path is not None else None
        self.last_run = RunResult()
        self._run_id = None
        self.execution_check = execution_check
        self.checkpoint_hook = None  # optional durable task observer; errors stop execution

        # wire up sub-agent capability
        for t in self.tools:
            if isinstance(t, AgentTool):
                t._parent_agent = self

        self._todo = next((t for t in self.tools if isinstance(t, TodoWriteTool)), None)

    def _full_messages(self) -> list[dict]:
        system = self._system
        # re-injected every round, like the task list below, so a toggle made
        # between turns takes effect on the very next request
        if self.plan_mode:
            system += "\n\n" + PLAN_MODE_PROMPT
        # the task list is re-injected every round, so the model always sees the
        # current state rather than a stale copy buried in old tool results
        if self._todo is not None:
            rendered = self._todo.render()
            if rendered:
                system += "\n\n# Current task list\n" + rendered
        return [{"role": "system", "content": system}] + self.messages

    def _tool_schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools]

    def chat(self, user_input: str, on_token=None, on_tool=None, on_reasoning=None) -> str:
        """Keep the string API; expose explicit execution status via last_run."""
        self.last_run = RunResult(status="running")
        self._run_id = uuid.uuid4().hex
        self._event("run_started", model=getattr(self.llm, "model", None),
                    max_rounds=self.max_rounds, max_empty_retries=self.max_empty_retries)
        try:
            return self._chat(user_input, on_token, on_tool, on_reasoning)
        except KeyboardInterrupt:
            self.last_run.status = "interrupted"
            raise
        except Exception as exc:
            self.last_run.status = "error"
            # Log the type, not exception text that might contain credentials.
            self.last_run.error_type = type(exc).__name__
            raise
        finally:
            self._event("run_ended", **asdict(self.last_run))

    def _event(self, event: str, **fields):
        record = {"event": event, "run_id": self._run_id, "time": time.time(), **fields}
        log.info("agent_event %s", json.dumps(record))
        if self.event_log_path is not None:
            try:
                self.event_log_path.parent.mkdir(parents=True, exist_ok=True)
                with _EVENT_LOCK, self.event_log_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record) + "\n")
            except OSError:
                log.warning("event_log_unavailable")

    def _chat(self, user_input: str, on_token=None, on_tool=None, on_reasoning=None) -> str:
        """Process one user message. May involve multiple LLM/tool rounds."""
        cleaned = clean_history(self.messages)
        removed = len(self.messages) - len(cleaned)
        self.messages[:] = cleaned
        if removed:
            self._event("history_repaired", removed=removed)
        self.messages.append({"role": "user", "content": user_input})
        self.context.maybe_compress(self.messages, self.llm)

        for round_index in range(self.max_rounds):
            self._checkpoint("round_started")
            self.last_run.rounds = round_index + 1
            started = time.monotonic()
            self._event("model_requested", round=self.last_run.rounds)
            resp = self.llm.chat(
                messages=self._full_messages(),
                tools=self._tool_schemas(),
                on_token=on_token,
                on_reasoning=on_reasoning,
            )

            self.last_run.finish_reason = getattr(resp, "finish_reason", None)
            self._event("model_response", round=self.last_run.rounds,
                        elapsed_seconds=time.monotonic() - started,
                        finish_reason=self.last_run.finish_reason,
                        prompt_tokens=getattr(resp, "prompt_tokens", None),
                        completion_tokens=getattr(resp, "completion_tokens", None),
                        has_text=bool(resp.content and resp.content.strip()), tool_calls=len(resp.tool_calls))
            # Never execute a potentially truncated tool call or treat partial
            # text as a normal final answer. Preserve only safe partial text.
            if self.last_run.finish_reason in {"length", "content_filter"}:
                self.last_run.status = "output_limit" if self.last_run.finish_reason == "length" else "filtered"
                if resp.content and resp.content.strip():
                    self.messages.append({"role": "assistant", "content": resp.content})
                self._checkpoint("response_saved")
                return resp.content or "(model response incomplete; see execution status)"

            if not resp.tool_calls and not (resp.content and resp.content.strip()):
                if self.last_run.empty_retries < self.max_empty_retries and round_index + 1 < self.max_rounds:
                    self.last_run.empty_retries += 1
                    self._event("empty_response_retry", retry=self.last_run.empty_retries)
                    continue  # no history append, no side-effect tools replayed
                self.last_run.status = "empty_response"
                self._checkpoint("response_saved")
                return "(model returned no text or tool calls; task has not been verified)"

            # A normal response ends this turn, not independent task validation.
            if not resp.tool_calls:
                self.messages.append(resp.message)
                self.last_run.status = "responded"
                self._checkpoint("response_saved")
                return resp.content

            # tool calls -> execute (parallel when multiple, like Claude Code's
            # StreamingToolExecutor which runs independent tools concurrently)
            self.messages.append(resp.message)
            self._checkpoint("tools_planned")

            try:
                self._checkpoint("tools_started", calls=[{"id": tc.id, "name": tc.name} for tc in resp.tool_calls])
                if len(resp.tool_calls) == 1:
                    tc = resp.tool_calls[0]
                    if on_tool:
                        on_tool(tc.name, tc.arguments)
                    result = self._pre_hooks(tc) or self._permit(tc)
                    if result is None:
                        result = self._exec_tool(tc)
                        self._post_hooks(tc, result)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                else:
                    # parallel execution for multiple tool calls
                    results = self._exec_tools_parallel(resp.tool_calls, on_tool)
                    for tc, result in zip(resp.tool_calls, results):
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        })
                self._checkpoint("tools_finished")
            except KeyboardInterrupt:
                # Ctrl+C mid-execution would leave the assistant tool_calls
                # message without replies, poisoning the next request; backfill
                self._answer_pending_tool_calls(resp.tool_calls)
                raise
            except Exception:
                # Budget/callback failures may stop a batch after some effects
                # happened. Keep protocol pairs valid without replaying tools.
                self._answer_pending_tool_calls(resp.tool_calls, "[batch stopped; result unavailable; do not blindly replay]")
                raise

            # compress if tool outputs are big
            self.context.maybe_compress(self.messages, self.llm)

        self.last_run.status = "round_limit"
        return "(reached maximum tool-call rounds)"

    def _checkpoint(self, event, **fields):
        if self.checkpoint_hook is not None:
            self.checkpoint_hook(event, **fields)

    def _pre_hooks(self, tc) -> str | None:
        """PreToolUse hooks, fired before consent. A string return blocks the
        call and becomes the tool result the model sees; None lets it through."""
        if self.hooks is None:
            return None
        return self.hooks.run_pre(tc.name, tc.arguments)

    def _post_hooks(self, tc, result: str):
        """PostToolUse hooks observe a finished call; they can never block."""
        if self.hooks is not None:
            self.hooks.run_post(tc.name, tc.arguments, result)

    def _permit(self, tc) -> str | None:
        """Consent check for one call. None means go ahead; a string is the
        refusal, returned as the tool result instead of executing."""
        # plan mode outranks consent, even --yes: while it's on nothing mutates
        if self.plan_mode and tc.name not in Permission.READ_ONLY:
            return (
                "Plan mode is on, so this call was refused: plan mode is "
                "read-only. Do not retry it. Keep investigating with the "
                "read-only tools, then present the plan and stop. The user can "
                'approve it by typing "approve", or exit plan mode with /plan.'
            )
        if self.permission is None:
            return None
        return self.permission.check(tc.name, tc.arguments)

    def _exec_tool(self, tc) -> str:
        if self.execution_check is not None:
            self.execution_check()
        started = time.monotonic()
        self._event("tool_started", tool=tc.name)
        try:
            result = self._execute_tool(tc)
            self._event("tool_returned", tool=tc.name, elapsed_seconds=time.monotonic() - started)
            return result
        except BaseException:
            self._event("tool_interrupted", tool=tc.name, elapsed_seconds=time.monotonic() - started)
            raise

    def _execute_tool(self, tc) -> str:
        """Execute a single tool call, returning the result string."""
        tool = self._tool_by_name.get(tc.name)
        if tool is None:
            return f"Error: unknown tool '{tc.name}'"
        # validate arguments first so a TypeError raised *inside* the tool isn't
        # mislabelled as a bad-arguments error from the caller
        try:
            inspect.signature(tool.execute).bind(**tc.arguments)
        except TypeError as e:
            return f"Error: bad arguments for {tc.name}: {e}"
        # a tool that blows up gets reported back as text, never kills the loop
        try:
            return tool.execute(**tc.arguments)
        except Exception as e:  # noqa: BLE001
            return f"Error executing {tc.name}: {e}"

    def _exec_tools_parallel(self, tool_calls, on_tool=None) -> list[str]:
        """Run multiple tool calls concurrently using threads.

        This is inspired by Claude Code's StreamingToolExecutor which starts
        executing tools while the model is still generating.  We simplify to:
        when the model returns N tool calls at once, run them in parallel.
        """
        from .tools.bash import get_tracked_cwd, set_tracked_cwd

        for tc in tool_calls:
            if on_tool:
                on_tool(tc.name, tc.arguments)

        # hooks and consent are settled up front on this thread: prompting
        # from pool workers would interleave several prompts on one terminal
        results = [self._pre_hooks(tc) or self._permit(tc) for tc in tool_calls]
        # the tracked cwd is thread-local, so pool workers would otherwise
        # start from the launch directory: hand the session cwd in, and merge
        # any cd back in call order so the batch behaves like a sequence
        session_cwd = get_tracked_cwd()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            def run(i, tc, base_cwd):
                is_bash = tc.name == "bash"
                if is_bash and base_cwd is not None:
                    set_tracked_cwd(base_cwd)
                out = self._exec_tool(tc)
                if not is_bash:
                    return i, out, None
                cwd_after = get_tracked_cwd()
                # only a worker that actually moved the dir reports back; a
                # sibling that merely inherited the base must not revert a cd
                return i, out, cwd_after if cwd_after != base_cwd else None

            futures = {
                i: pool.submit(run, i, tc, session_cwd)
                for i, tc in enumerate(tool_calls)
                if results[i] is None
            }
            for i, future in futures.items():
                i, results[i], worker_cwd = future.result()
                if worker_cwd is not None:
                    session_cwd = worker_cwd
                    set_tracked_cwd(worker_cwd)
        for i in futures:
            self._post_hooks(tool_calls[i], results[i])
        return results

    def _answer_pending_tool_calls(self, tool_calls, content="[interrupted]"):
        """Backfill a tool reply for every call that didn't get one.

        OpenAI-compatible APIs reject a request where an assistant message has
        tool_calls without a matching tool reply for each id, so this keeps the
        history valid when execution is interrupted partway through.
        """
        answered = {m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"}
        for tc in tool_calls:
            if tc.id not in answered:
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": content,
                })

    def reset(self):
        """Clear conversation history."""
        self.messages.clear()
        self.last_run = RunResult()
        # a user-visible reset must not leak the dead conversation's checklist
        # into the next system prompt
        todo = self._tool_by_name.get("todo_write")
        if isinstance(todo, TodoWriteTool):
            todo._tasks = []
