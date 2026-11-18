"""Interactive REPL - the user-facing terminal interface."""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from . import __version__
from .agent import Agent
from .config import Config
from .hooks import load_hooks
from .llm import LLM, LiteLLM
from .mcp import load_mcp_tools
from .permissions import DEFAULT_STORE, Permission
from .session import list_sessions, load_session, load_task_link, save_session
from .tools import ALL_TOOLS

console = Console()
err_console = Console(stderr=True)


def _parse_args():
    p = argparse.ArgumentParser(
        prog="corecoder",
        description="Minimal AI coding agent. Works with any OpenAI-compatible LLM.",
    )
    p.add_argument("-m", "--model", help="Model name (default: $CORECODER_MODEL or gpt-5.5)")
    p.add_argument("--base-url", help="API base URL (default: $OPENAI_BASE_URL)")
    p.add_argument("--api-key", help="API key (default: $OPENAI_API_KEY)")
    p.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    p.add_argument("--yes", action="store_true", help="Auto-approve every tool call (for scripts and CI)")
    p.add_argument("--demo", action="store_true", help="Run the offline scripted demo (no API key needed)")
    p.add_argument("--repair-config", metavar="JSON", help="Independent verification config; requires -p")
    p.add_argument("-r", "--resume", metavar="ID", help="Resume a saved session")
    p.add_argument("--resume-task", metavar="DIR", help="Resume a durable repair task and its remaining budget")
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args()
    if args.repair_config and (not args.prompt or args.resume):
        p.error("--repair-config requires -p and cannot be combined with --resume")
    if args.resume_task and (args.prompt or args.resume or args.repair_config):
        p.error("--resume-task cannot be combined with -p, --resume or --repair-config")
    return args


def main():
    args = _parse_args()

    if args.demo:
        from .demo import run_demo
        raise SystemExit(run_demo())

    config = Config.from_env()

    # CLI args override env vars
    if args.model:
        config.model = args.model
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key

    if not config.api_key:
        console.print("[red bold]No API key found.[/]")
        console.print(
            "Set one of: OPENAI_API_KEY, DEEPSEEK_API_KEY, or CORECODER_API_KEY\n"
            "\nExamples:\n"
            "  # OpenAI\n"
            "  export OPENAI_API_KEY=sk-...\n"
            "\n"
            "  # DeepSeek\n"
            "  export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com\n"
            "\n"
            "  # Ollama (local)\n"
            "  export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 CORECODER_MODEL=qwen2.5-coder\n"
        )
        sys.exit(1)

    llm_cls = LiteLLM if config.provider == "litellm" else LLM
    llm = llm_cls(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    # consent layer: ask in the REPL, refuse in one-shot mode, --yes skips it
    if args.yes:
        permission = Permission(allow_all=True)
    elif args.prompt:
        permission = Permission()
    else:
        permission = Permission(ask=_ask_permission, persist_path=DEFAULT_STORE)
    agent = Agent(
        llm=llm,
        tools=[*ALL_TOOLS, *load_mcp_tools()],
        max_context_tokens=config.max_context_tokens,
        permission=permission,
        hooks=load_hooks(),
        event_log_path=Path.home() / ".corecoder" / "runs" / f"{uuid.uuid4().hex}.jsonl",
    )

    # resume saved session
    if args.resume:
        loaded = load_session(args.resume)
        if loaded:
            agent.messages, loaded_model = loaded
            agent.task_checkpoint = load_task_link(args.resume)
            agent.session_id = args.resume
            # restore the model from the saved session unless overridden by CLI
            if not args.model:
                agent.llm.model = loaded_model
                config.model = loaded_model
            console.print(f"[green]Resumed session: {args.resume} (model: {agent.llm.model})[/green]")
            if agent.task_checkpoint:
                _task_status(agent.task_checkpoint)
        else:
            console.print(f"[red]Session '{args.resume}' not found.[/red]")
            sys.exit(1)

    if args.resume_task:
        _continue_task(agent, args.resume_task, one_shot=True)
        return

    # one-shot mode
    if args.prompt:
        if getattr(agent, 'task_checkpoint', None):
            console.print('[red]该会话关联修复任务；请使用“继续任务”或 --resume-task 恢复。[/red]')
            raise SystemExit(1)
        if args.repair_config:
            _repair_once(agent, args.prompt, args.repair_config)
            return
        _run_once(agent, args.prompt)
        return

    # interactive REPL
    _repl(agent, config)


def _ask_permission(tool_name: str, arguments: dict) -> str:
    """REPL consent prompt. Anything but a clear yes counts as a no."""
    console.print(f"\n[bold yellow]permission requested:[/] [cyan]{tool_name}[/cyan]({_brief(arguments)})")
    try:
        answer = pt_prompt("  [y] allow once  [a] always allow this tool  [n] deny: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("[dim]denied[/dim]")
        return "deny"
    if answer in ("y", "yes"):
        return "once"
    if answer in ("a", "always"):
        return "always"
    return "deny"


def _repair_once(agent, prompt, config_path):
    from .repair import RegressionSpec, RepairController, RepairLimits, VerificationSpec

    try:
        path = Path(config_path).expanduser().resolve()
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get('checkpoint_dir'):
            _start_task(agent, path, prompt, one_shot=True)
            return
        verification = dict(data["verification"])
        verification["protected_paths"] = [*verification.get("protected_paths", ["tests"]), str(path)]
        controller = RepairController(
            agent, path.parent / data["workspace"], VerificationSpec(**verification),
            path.parent / data["result_dir"], RepairLimits(**data.get("limits", {})),
            regression=RegressionSpec(**data["regression"]) if "regression" in data else None,
        )
        result = controller.run(prompt)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        console.print(f"[red]Repair setup error: {type(exc).__name__}[/red]")
        raise SystemExit(1) from exc
    console.print(f"Verification: {result.status}; attempts: {len(result.attempts)}; model calls: {result.model_calls}")
    console.print(f"Acceptance: {result.acceptance_status}; regression: {result.regression_status}; scope: {result.completion_scope}")
    console.print(f"Results: {controller.result_dir / 'result.json'}")
    if result.status != "passed":
        raise SystemExit(1)


def _task_status(directory):
    from .tasks import RepairTask
    state = RepairTask.info(directory)
    ledger, limits = state['ledger'], state['config']['limits']
    console.print(f"Task: {state['status']}; phase: {state['phase']}; "
                  f"calls: {ledger['model_calls']}/{limits['max_model_calls']}; "
                  f"attempts: {ledger['attempts']}/{limits['max_attempts']}")
    console.print(f"Checkpoint: {directory}")
    if state['completed']:
        console.print('[green]任务已完成，无需再次调用模型。[/green]')
    elif state['pending'] and state['pending']['kind'] in ('tools', 'check'):
        console.print('[yellow]操作结果不明，自动恢复会停止；第一版不自动重放，也不自动清除此标记。[/yellow]')
    else:
        console.print('输入“继续任务”或 /continue 恢复')


def _continue_task(agent, directory=None, one_shot=False):
    from .tasks import RepairTask, TaskStorageError
    directory = directory or getattr(agent, 'task_checkpoint', None)
    if not directory:
        console.print('[yellow]没有关联的修复任务。使用 /repair 配置文件 开始。[/yellow]')
        return
    task = RepairTask(agent, directory)
    try:
        result = task.run()
    except TaskStorageError as exc:
        raise ValueError('检查点写入失败，执行已停止；请检查存储后核对任务记录。') from exc
    agent.task_checkpoint = str(task.directory)
    agent.session_id = save_session(agent.messages, getattr(agent.llm, 'model', '?'),
        getattr(agent, 'session_id', None), task_checkpoint=task.directory)
    _task_status(task.directory)
    console.print(f"Completed: {result['completed']}; resume session: corecoder -r {agent.session_id}")
    if one_shot and not result['completed']:
        raise SystemExit(1)


def _start_task(agent, config_path, prompt=None, one_shot=False):
    from .repair import RegressionSpec, RepairLimits, VerificationSpec
    from .tasks import RepairTask
    path = Path(config_path).expanduser().resolve()
    data = json.loads(path.read_text(encoding='utf-8'))
    if getattr(agent, 'task_checkpoint', None):
        raise ValueError('This session already has a task; open a new session to start another')
    verification = dict(data['verification'])
    verification['protected_paths'] = [*verification.get('protected_paths', ['tests']), str(path)]
    task = RepairTask.create(agent, path.parent / data['checkpoint_dir'], prompt or data['task'],
        path.parent / data['workspace'], VerificationSpec(**verification), RegressionSpec(**data['regression']),
        RepairLimits(**data.get('limits', {})))
    agent.task_checkpoint = str(task.directory)
    agent.session_id = save_session(agent.messages, getattr(agent.llm, 'model', '?'),
        getattr(agent, 'session_id', None), task_checkpoint=task.directory)
    console.print(f"Task linked. Resume with: corecoder -r {agent.session_id}")
    _continue_task(agent, one_shot=one_shot)


def _run_once(agent: Agent, prompt: str):
    """Non-interactive: run one prompt and exit."""
    perm = agent.permission
    if perm is not None and perm.ask is None and not perm.allow_all:
        console.print("[dim]one-shot mode: mutating tools are refused unless you pass --yes[/dim]")

    def on_token(tok):
        print(tok, end="", flush=True)

    def on_tool(name, kwargs):
        console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

    def on_reasoning(tok):
        # thinking goes to stderr so piped stdout stays the plain answer
        err_console.print(f"[dim]{tok}[/dim]", end="")

    try:
        response = agent.chat(prompt, on_token=on_token, on_tool=on_tool, on_reasoning=on_reasoning)
        _show_execution_status(agent, response)
        result = getattr(agent, "last_run", None)
        if result is not None and result.status != "responded":
            sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        # one-shot mode: print whatever went wrong and exit non-zero
        console.print(f"\n[red]Error: {e}[/red]")
        sys.exit(1)
    print()


def _show_execution_status(agent, response):
    """Always show non-streamed terminal outcomes, even after reasoning output."""
    result = getattr(agent, "last_run", None)
    if result is None:
        return
    if result.status != "responded":
        console.print(f"\n[yellow]{response}[/yellow]")
    console.print(f"\n[dim]Execution: {result.status}; task not independently verified.[/dim]")
    path = getattr(agent, "event_log_path", None)
    if path is not None:
        console.print(f"[dim]Execution log: {path}[/dim]")


def _repl(agent: Agent, config: Config):
    """Interactive read-eval-print loop."""
    perm = agent.permission
    mode = "auto-approve every tool call (--yes)" if (perm and perm.allow_all) else "ask before mutating tools"
    mcp_count = sum(1 for t in agent.tools if t.name.startswith("mcp__"))
    console.print(Panel(
        f"[bold]CoreCoder[/bold] v{__version__}\n"
        f"Model: [cyan]{config.model}[/cyan]"
        + (f"  Base: [dim]{config.base_url}[/dim]" if config.base_url else "")
        + f"\nPermissions: [cyan]{mode}[/cyan]"
        + (f"\nHooks: [cyan]{len(agent.hooks.pre)} pre, {len(agent.hooks.post)} post[/cyan]"
           " from ~/.corecoder/hooks.json" if agent.hooks else "")
        + (f"\nMCP: [cyan]{mcp_count} tools[/cyan] from ~/.corecoder/mcp.json" if mcp_count else "")
        + "\nType [bold]/help[/bold] for commands, [bold]Ctrl+C[/bold] to cancel, [bold]quit[/bold] to exit.",
        border_style="blue",
    ))

    hist_path = os.path.expanduser("~/.corecoder_history")
    history = FileHistory(hist_path)

    # Enter submits, Escape+Enter inserts a newline (for pasting code blocks etc.)
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    while True:
        try:
            user_input = pt_prompt(
                "You (plan) > " if agent.plan_mode else "You > ",
                history=history,
                multiline=True,
                key_bindings=kb,
                prompt_continuation="...  ",
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            break

        if not user_input:
            continue

        if getattr(agent, 'task_checkpoint', None) and user_input.split()[0] in ('/compact', '/model', '/plan', '/reset'):
            console.print('[yellow]任务配置和预算由检查点管理；请用 /task 或“继续任务”，另开会话进行普通聊天。[/yellow]')
            continue

        if user_input in ('继续任务', '/continue') or user_input == '/task' or user_input.startswith('/repair '):
            try:
                if user_input == '/task':
                    directory = getattr(agent, 'task_checkpoint', None)
                    if directory:
                        _task_status(directory)
                    else:
                        console.print('[dim]没有关联的修复任务。[/dim]')
                elif user_input.startswith('/repair '):
                    _start_task(agent, user_input[len('/repair '):].strip())
                else:
                    _continue_task(agent)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                console.print(f'[red]Task stopped: {exc}[/red]')
            continue

        if getattr(agent, 'task_checkpoint', None) and not user_input.startswith('/') and user_input.lower() not in ('quit', 'exit'):
            console.print('[yellow]修复任务会话请使用“继续任务”或 /task；新任务请另开会话。[/yellow]')
            continue

        # built-in commands
        if user_input.lower() in ("quit", "exit", "/quit", "/exit"):
            break
        if user_input == "/help":
            _show_help()
            continue
        if user_input == "/reset":
            agent.reset()
            console.print("[yellow]Conversation reset.[/yellow]")
            continue
        if user_input == "/plan":
            agent.plan_mode = not agent.plan_mode
            if agent.plan_mode:
                console.print(
                    "[yellow]Plan mode on.[/yellow] The agent can look but not touch: it will "
                    "investigate read-only and present a plan. Type [bold]approve[/bold] to "
                    "accept the plan, or [bold]/plan[/bold] again to exit."
                )
            else:
                console.print("[yellow]Plan mode off.[/yellow]")
            continue
        if agent.plan_mode and user_input.lower() in ("approve", "/approve"):
            agent.plan_mode = False
            console.print("[yellow]Plan mode off.[/yellow]")
            user_input = "approve"  # the approval itself goes to the model, which then executes
        if user_input == "/tokens":
            p = agent.llm.total_prompt_tokens
            c = agent.llm.total_completion_tokens
            line = f"Tokens: [cyan]{p}[/cyan] prompt + [cyan]{c}[/cyan] completion = [bold]{p+c}[/bold] total"
            cost = agent.llm.estimated_cost
            if cost is not None:
                line += f"  (~${cost:.4f})"
            console.print(line)
            continue
        if user_input == "/model" or user_input.startswith("/model "):
            new_model = user_input[7:].strip() if user_input.startswith("/model ") else ""
            if new_model:
                agent.llm.model = new_model
                config.model = new_model
                console.print(f"Switched to [cyan]{new_model}[/cyan]")
            else:
                console.print(f"Current model: [cyan]{config.model}[/cyan]")
            continue
        if user_input == "/compact":
            from .context import estimate_tokens
            before = estimate_tokens(agent.messages)
            compressed = agent.context.maybe_compress(agent.messages, agent.llm)
            after = estimate_tokens(agent.messages)
            if compressed:
                console.print(f"[green]Compressed: {before} → {after} tokens ({len(agent.messages)} messages)[/green]")
            else:
                console.print(f"[dim]Nothing to compress ({before} tokens, {len(agent.messages)} messages)[/dim]")
            continue
        if user_input == "/save":
            sid = save_session(agent.messages, config.model,
                               getattr(agent, 'session_id', None) if getattr(agent, 'task_checkpoint', None) else None,
                               task_checkpoint=getattr(agent, 'task_checkpoint', None))
            agent.session_id = sid
            console.print(f"[green]Session saved: {sid}[/green]")
            console.print(f"Resume with: corecoder -r {sid}")
            continue
        if user_input == "/diff":
            from .tools.edit import _changed_files
            if not _changed_files:
                console.print("[dim]No files modified this session.[/dim]")
            else:
                console.print(f"[bold]Files modified this session ({len(_changed_files)}):[/bold]")
                for f in sorted(_changed_files):
                    console.print(f"  [cyan]{f}[/cyan]")
            continue
        if user_input == "/undo":
            from .checkpoints import pending, undo
            console.print(undo())
            left = pending()
            if left:
                console.print(f"[dim]{left} more checkpoint(s) on the stack.[/dim]")
            continue
        if user_input == "/sessions":
            sessions = list_sessions()
            if not sessions:
                console.print("[dim]No saved sessions.[/dim]")
            else:
                for s in sessions:
                    task_label = f" [task: {s['task_status']}]" if s.get('task_status') else ''
                    console.print(f"  [cyan]{s['id']}[/cyan] ({s['model']}, {s['saved_at']}){task_label} {s['preview']}")
            continue
        if user_input == "/mcp" or user_input.startswith("/mcp "):
            _mcp(user_input[4:].strip())
            continue

        # an unknown /command shouldn't be sent to the model as a prompt
        if user_input.startswith("/"):
            console.print(f"[yellow]Unknown command: {user_input.split()[0]} (try /help)[/yellow]")
            continue

        # call the agent
        streamed: list[str] = []

        def on_token(tok, streamed=streamed):
            streamed.append(tok)
            print(tok, end="", flush=True)

        def on_tool(name, kwargs):
            console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

        def on_reasoning(tok, streamed=streamed):
            # dim the thinking inline; mark the stream non-empty so the final
            # newline logic below still fires even if the reply itself is empty
            streamed.append(tok)
            console.print(f"[dim]{tok}[/dim]", end="")

        try:
            response = agent.chat(user_input, on_token=on_token, on_tool=on_tool,
                                  on_reasoning=on_reasoning)
            if streamed:
                print()  # newline after streamed tokens
            elif getattr(getattr(agent, "last_run", None), "status", "responded") == "responded":
                # response wasn't streamed (came after tool calls)
                console.print(Markdown(response))
            _show_execution_status(agent, response)
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception as e:  # noqa: BLE001
            # keep the REPL alive no matter what chat() throws
            console.print(f"\n[red]Error: {e}[/red]")


def _mcp(arg: str):
    """The /mcp command: server states, or `reconnect <name>` to revive one."""
    from . import mcp
    parts = arg.split()
    if not parts:
        if not mcp._live_clients:
            console.print("[dim]No MCP servers running (configure ~/.corecoder/mcp.json).[/dim]")
        for client in mcp._live_clients:
            if client.error is None:
                console.print(f"  [cyan]{client.name}[/cyan]: alive, {len(client.tools)} tools")
            else:
                console.print(
                    f"  [cyan]{client.name}[/cyan]: [red]dead[/red] ({client.error})"
                    f", revive with /mcp reconnect {client.name}"
                )
        return
    if len(parts) == 2 and parts[0] == "reconnect":
        client = next((c for c in mcp._live_clients if c.name == parts[1]), None)
        if client is None:
            console.print(f"[red]No MCP server named '{parts[1]}'.[/red]")
            return
        try:
            client.reconnect()
        except (mcp.MCPError, OSError) as e:
            console.print(f"[red]Reconnect failed:[/red] {e}")
        else:
            console.print(f"[green]Reconnected:[/green] [cyan]{client.name}[/cyan], {len(client.tools)} tools")
        return
    console.print("[yellow]Usage: /mcp [reconnect <name>][/yellow]")


def _show_help():
    console.print(Panel(
        "[bold]Commands:[/bold]\n"
        "  /help          Show this help\n"
        "  /reset         Clear conversation history\n"
        "  /model         Show current model\n"
        "  /model <name>  Switch model mid-conversation\n"
        "  /tokens        Show token usage\n"
        "  /compact       Compress conversation context\n"
        "  /diff          Show files modified this session\n"
        "  /undo          Revert the most recent file change\n"
        "  /plan          Toggle plan mode: read-only, then a plan to approve\n"
        "  /save          Save session to disk\n"
        "  /repair <json> Start a durable repair task (task + checkpoint_dir required)\n"
        "  /task          Show linked task progress\n"
        "  /continue      Resume task; also accepts 继续任务\n"
        "  /sessions      List saved sessions\n"
        "  /mcp           MCP server status; /mcp reconnect <name> revives a dead one\n"
        "  quit           Exit CoreCoder\n"
        "\n"
        "[bold]Input:[/bold]\n"
        "  Enter          Submit message\n"
        "  Esc+Enter      Insert newline (for pasting code)",
        title="CoreCoder Help",
        border_style="dim",
    ))


def _brief(kwargs: dict, maxlen: int = 80) -> str:
    s = ", ".join(f"{k}={repr(v)[:40]}" for k, v in kwargs.items())
    return s[:maxlen] + ("..." if len(s) > maxlen else "")
