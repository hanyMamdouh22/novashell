from .ui import render_ai_response
import os
import sqlite3
import stat
import sys
from google.genai.errors import APIError
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from .agent import NovaAgent, clear_session_history
from .config import API_KEY, APP_NAME, AUDIT_DB, CONFIG_ENV, DEFAULT_MODEL, STRICT_MODE, load_config_env, save_config_env
from .guard import classify_command, init_db, parse_confirmation
from .plugins import load_plugins, show_plugins
from .rollback import export_audit_log, init_snapshots_db, show_audit_log, show_snapshots, undo_snapshot
from .tools import reset_task_approvals

console = Console()


def show_banner(agent: NovaAgent) -> None:
    plugin_count = len(agent.plugin_tools)
    console.print(
        Panel.fit(
            f"[bold cyan]{APP_NAME}[/bold cyan] [dim]v1.4 · Enterprise-Ready Ubuntu AI Operator[/dim]\n"
            f"[dim]Model: {agent.model} | Plugins: {plugin_count} | Commands: doctor · setup · --plan · plugins · audit · history · undo[/dim]",
            border_style="cyan",
        )
    )


def run_doctor() -> None:
    """Runs an enterprise readiness & security health check for NovaShell."""
    init_db()
    init_snapshots_db()
    table = Table(title=f"{APP_NAME} System & Security Doctor", border_style="cyan")
    table.add_column("Check", style="bold white")
    table.add_column("Status")
    table.add_column("Details", overflow="fold")

    # 1) API Key
    if API_KEY:
        table.add_row("Gemini API Key", "[bold green]OK[/bold green]", f"Loaded (****{API_KEY[-4:]})")
    else:
        table.add_row("Gemini API Key", "[bold red]MISSING[/bold red]", "Run `nova setup` or export GEMINI_API_KEY")

    # 2) Config file permissions
    if CONFIG_ENV.exists():
        mode = oct(stat.S_IMODE(os.stat(CONFIG_ENV).st_mode))[2:]
        status = "[bold green]OK[/bold green]" if mode == "600" else "[bold yellow]WARN[/bold yellow]"
        table.add_row("Config Security", status, f"{CONFIG_ENV} (mode {mode})")
    else:
        table.add_row("Config Security", "[bold green]OK[/bold green]", "Using shell environment (~/.bashrc)")

    # 3) Security Guard & Redaction
    table.add_row(
        "Zero-Trust Guard",
        "[bold green]ACTIVE[/bold green]",
        f"Strict Mode: {'ON' if STRICT_MODE else 'OFF'} · Secret Redaction: ENABLED",
    )

    # 4) Audit & Snapshots DB
    with sqlite3.connect(AUDIT_DB) as conn:
        audit_count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        snap_count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    table.add_row("Audit & Time-Machine", "[bold green]READY[/bold green]", f"{audit_count} audit records · {snap_count} snapshots")

    # 5) Plugins
    tools, _ = load_plugins(read_only_only=False)
    table.add_row("Dynamic Plugins", "[bold green]LOADED[/bold green]", f"{len(tools)} active plugin tool(s)")

    # 6) Default Model
    table.add_row("Primary Model", "[bold green]READY[/bold green]", f"{DEFAULT_MODEL} (Auto-Failover enabled)")

    console.print(table)


def run_setup_wizard() -> None:
    current = load_config_env()
    console.print(
        Panel.fit(
            f"[bold cyan]{APP_NAME} Interactive Setup[/bold cyan]\n"
            f"[dim]Saves strict-permission (0600) config to {CONFIG_ENV}[/dim]",
            border_style="cyan",
        )
    )
    existing_key = current.get("GEMINI_API_KEY", "")
    key_hint = "(press Enter to keep current)" if existing_key else "(paste your Google AI Studio key)"
    api_key = console.input(f"[bold green]Gemini API Key[/bold green] [dim]{key_hint}[/dim]: ", password=True).strip()
    model = console.input(f"[bold green]Default Model[/bold green] [dim][{DEFAULT_MODEL}][/dim]: ").strip() or DEFAULT_MODEL
    strict_default = "y" if STRICT_MODE else "n"
    strict_ans = console.input(
        f"[bold green]Strict Mode (require confirmation even for read-only commands)?[/bold green] [dim][{strict_default}][/dim]: "
    ).strip().lower()
    strict_val = "true" if (strict_ans in ("y", "yes") or (strict_ans == "" and STRICT_MODE)) else "false"

    payload = {"NOVA_MODEL": model, "NOVA_STRICT_MODE": strict_val}
    if api_key:
        payload["GEMINI_API_KEY"] = api_key

    saved_path = save_config_env(payload)
    console.print(
        Panel(
            f"[bold green]Configuration saved![/bold green]\n"
            f"• File: [cyan]{saved_path}[/cyan] (mode [bold]0600[/bold])\n"
            f"• Model: [cyan]{model}[/cyan]\n"
            f"• Strict Mode: [cyan]{strict_val}[/cyan]",
            border_style="green",
        )
    )


def render_api_error(exc: APIError) -> None:
    if exc.code == 429:
        msg = (
            "[bold yellow]API Rate Limit / Daily Quota Reached (429)[/bold yellow]\n\n"
            "• All configured Gemini models exhausted their current free-tier quota.\n"
            "• Wait a few minutes, switch models via [cyan]NOVA_MODEL=...[/cyan], or enable billing on Google AI Studio."
        )
    else:
        msg = f"[bold red]API Error ({exc.code}):[/bold red] {exc.message}"
    console.print(Panel(msg, title="[bold red]NovaShell Notice[/bold red]", border_style="red"))


def handle_builtin_subcommand(args: list[str]) -> bool:
    if not args:
        return False
    cmd = args[0].lower()
    if cmd in ("reset", "new") and len(args) == 1:
        count = clear_session_history()
        console.print(f"[bold green]Session memory cleared[/bold green] ({count} messages removed). Starting fresh.")
        return True
    if cmd == "doctor" and len(args) == 1:
        run_doctor()
        return True
    if cmd == "setup" and len(args) == 1:
        run_setup_wizard()
        return True
    if cmd == "audit":
        if len(args) >= 2 and args[1] == "--export":
            fmt = args[2] if len(args) >= 3 else "csv"
            out = args[3] if len(args) >= 4 else None
            export_audit_log(fmt, out)
            return True
        if len(args) == 1 or (len(args) == 2 and args[1].isdigit()):
            show_audit_log(int(args[1]) if len(args) == 2 else 20)
            return True
    if cmd == "history" and (len(args) == 1 or (len(args) == 2 and args[1].isdigit())):
        show_snapshots(int(args[1]) if len(args) == 2 else 15)
        return True
    if cmd == "undo" and (len(args) == 1 or (len(args) == 2 and args[1].isdigit())):
        undo_snapshot(int(args[1]) if len(args) == 2 else None)
        return True
    if cmd == "check" and len(args) >= 2:
        cmd_str = " ".join(args[1:])
        risk, reason = classify_command(cmd_str)
        colors = {"SAFE": "green", "CONFIRM": "yellow", "BLOCKED": "red"}
        titles = {"SAFE": "SAFE · AUTO-RUN", "CONFIRM": "CONFIRM REQUIRED", "BLOCKED": "BLOCKED BY SECURITY GUARD"}
        c = colors.get(risk, "white")
        console.print(Panel(f"[bold {c}]{cmd_str}[/bold {c}]\n\n[dim]{reason}[/dim]", title=f"[bold {c}]{titles.get(risk, risk)}[/bold {c}]", border_style=c))
        return True
    if cmd == "plugins" and len(args) == 1:
        show_plugins()
        return True
    return False


def run_plan_workflow(query: str) -> None:
    planner = NovaAgent(plan_mode=True)
    console.print("[bold magenta]PLAN MODE (Read-Only) · Inspecting system & drafting execution plan...[/bold magenta]")
    plan_reply = planner.ask(query)
    render_ai_response(plan_reply, f"[bold magenta]{APP_NAME} PLAN (Read-Only)[/bold magenta] [dim]({planner.model})[/dim]", style="magenta")

    proceed = console.input("[bold magenta]Approve and execute this plan now? \\[y/N]: [/bold magenta]")
    if parse_confirmation(proceed) == "no":
        console.print("[dim]Plan saved to screen only. No changes were made to your system.[/dim]")
        return

    reset_task_approvals()
    executor = NovaAgent(model=planner.model, plan_mode=False)
    console.print("[bold cyan]Transitioning to Execution Mode...[/bold cyan]")
    exec_reply = executor.ask(
        f"Original user request: {query}\n\n"
        f"The user approved the following plan. Execute it safely step-by-step and verify the final result:\n\n{plan_reply}"
    )
    render_ai_response(exec_reply, f"[bold cyan]{APP_NAME} EXECUTION REPORT[/bold cyan] [dim]({executor.model})[/dim]", style="blue")


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "ui":
        from .web_ui import start_web_ui
        port = int(sys.argv[2]) if len(sys.argv) >= 3 and sys.argv[2].isdigit() else 8765
        start_web_ui(port)
        return
    args = sys.argv[1:]
    if handle_builtin_subcommand(args):
        return

    plan_mode = False
    if args and args[0] == "--plan":
        plan_mode = True
        args = args[1:]

    try:
        if plan_mode and args:
            run_plan_workflow(" ".join(args))
            return

        agent = NovaAgent(plan_mode=False)
    except APIError as exc:
        render_api_error(exc)
        sys.exit(1)
    except Exception as exc:
        console.print(f"[bold red]Startup Error:[/bold red] {exc}")
        console.print("[dim]Tip: Run `nova setup` to configure your Gemini API key securely.[/dim]")
        sys.exit(1)

    if args:
        query = " ".join(args)
        console.print("[bold cyan]Thinking & inspecting system...[/bold cyan]")
        try:
            reset_task_approvals()
            reply = agent.ask(query)
            render_ai_response(reply, f"[bold cyan]{APP_NAME}[/bold cyan] [dim]({agent.model})[/dim]", style="blue")
        except APIError as exc:
            render_api_error(exc)
            sys.exit(1)
        return

    show_banner(agent)
    while True:
        try:
            user_input = console.input("\n[bold green]You › [/bold green]").strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "q"):
                console.print("[dim]Session closed.[/dim]")
                break
            if user_input.startswith("/plan "):
                run_plan_workflow(user_input[6:].strip())
                continue

            console.print("[bold cyan]Reasoning...[/bold cyan]")
            reset_task_approvals()
            reply = agent.ask(user_input)
            render_ai_response(reply, f"[bold cyan]{APP_NAME}[/bold cyan] [dim]({agent.model})[/dim]", style="blue")
        except KeyboardInterrupt:
            console.print("\n[dim]Interrupted. Type 'exit' to quit.[/dim]")
        except APIError as exc:
            render_api_error(exc)
        except Exception as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")


if __name__ == "__main__":
    main()
