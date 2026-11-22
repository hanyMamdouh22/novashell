import difflib
import os
import re
import shutil
import subprocess
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from .context import collect_system_context
from .guard import classify_command, log_audit, parse_confirmation, redact_secrets
from .rollback import (
    create_command_snapshot,
    create_file_snapshot,
    detect_reverse_command,
)

console = Console()
IS_WINDOWS = os.name == "nt"
_AUTO_APPROVE_TASK = False
_APPROVED_SUDO_READS: set[str] = set()

FORBIDDEN_READ_FILES = {"/etc/shadow", "/etc/gshadow"}
FORBIDDEN_PATH_HINTS = (
    "/etc/shadow",
    "/etc/gshadow",
    "system32/config/sam",
    "system32/config/security",
    "system32/config/system",
    "ntds.dit",
)


def _is_forbidden_path(path: Path) -> bool:
    norm = str(path).replace("\\", "/").lower()
    if str(path) in FORBIDDEN_READ_FILES:
        return True
    return any(hint in norm for hint in FORBIDDEN_PATH_HINTS)


def _split_sed_expression(expr: str) -> tuple[str, str, str] | None:
    """Parses basic s/pattern/replacement/flags expressions for Windows fallback."""
    if len(expr) < 4 or expr[0] != "s":
        return None
    delim = expr[1]
    parts: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in expr[2:]:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
            buf.append(ch)
        elif ch == delim and len(parts) < 2:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def _apply_python_sed(original: str, sed_expression: str) -> tuple[str | None, str | None]:
    parsed = _split_sed_expression(sed_expression.strip())
    if not parsed:
        return None, "On Windows, sed fallback supports standard s/old/new/g expressions. Use patch_file_text instead."
    pattern, replacement, flags_str = parsed
    flags = re.IGNORECASE if "i" in flags_str.lower() else 0
    count = 0 if "g" in flags_str.lower() else 1
    try:
        regex = re.compile(pattern, flags)
        new_lines = [regex.sub(replacement, line, count=count) for line in original.splitlines(keepends=True)]
        return "".join(new_lines), None
    except re.error as exc:
        return None, f"Invalid regex in sed expression: {exc}"


def reset_task_approvals() -> None:
    global _AUTO_APPROVE_TASK
    _AUTO_APPROVE_TASK = False
    _APPROVED_SUDO_READS.clear()


def get_system_snapshot() -> dict:
    """Returns live OS details, current directory, user permissions, and available tools."""
    return collect_system_context()


def read_file_content(file_path: str, max_lines: int = 120) -> dict:
    """Safely reads the first N lines of a text file (supports per-task cached sudo read on Linux)."""
    global _AUTO_APPROVE_TASK
    path = Path(file_path).expanduser().resolve()
    path_key = str(path)
    if _is_forbidden_path(path):
        return {"status": "blocked", "message": f"Reading {path} is blocked by NovaShell security policy."}

    try:
        if not path.exists() or not path.is_file():
            return {"status": "error", "message": f"File not found: {path}"}
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except PermissionError:
        if IS_WINDOWS:
            return {
                "status": "error",
                "message": f"Permission denied reading {path}. Re-run PowerShell as Administrator to access this file.",
            }
        cached = path_key in _APPROVED_SUDO_READS or _AUTO_APPROVE_TASK
        if cached:
            console.print(f"[dim]SUDO READ · CACHED APPROVAL for {path}[/dim]")
        else:
            console.print(
                Panel(
                    f"[bold yellow]{path}[/bold yellow]\n[dim]Root permissions required to read this file.[/dim]",
                    title="[bold yellow]SUDO READ REQUEST[/bold yellow]",
                    border_style="yellow",
                )
            )
            ans = console.input("[bold yellow]Read this root-owned file via sudo? \\[y/N]: [/bold yellow]")
            decision = parse_confirmation(ans)
            if decision == "no":
                log_audit(f"sudo cat {path}", "CONFIRM", "DENIED", None)
                return {"status": "denied", "message": "User declined sudo file read."}
            if decision == "all":
                _AUTO_APPROVE_TASK = True
            _APPROVED_SUDO_READS.add(path_key)

        proc = subprocess.run(["sudo", "cat", str(path)], capture_output=True, text=True)
        log_audit(f"sudo cat {path}", "CONFIRM", "EXECUTED", proc.returncode)
        if proc.returncode != 0:
            return {"status": "error", "message": proc.stderr.strip()}
        lines = proc.stdout.splitlines()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    return {
        "status": "ok",
        "path": path_key,
        "total_lines": len(lines),
        "content": redact_secrets("\n".join(lines[:max_lines]))[0],
    }


def patch_file_text(file_path: str, old_text: str, new_text: str, explanation: str) -> dict:
    """Cross-platform (Windows & Linux) exact text replacement with live unified diff preview and Time-Machine snapshot."""
    path = Path(file_path).expanduser().resolve()
    if _is_forbidden_path(path):
        return {"status": "blocked", "message": f"Modifying {path} is blocked by NovaShell security policy."}
    if not path.exists() or not path.is_file():
        return {"status": "error", "message": f"File does not exist: {path}"}

    try:
        original = path.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        return {"status": "error", "message": f"Permission denied reading {path}. Elevate privileges first."}

    if old_text not in original:
        return {"status": "no_change", "message": "Target text not found in file; no changes made."}

    updated = original.replace(old_text, new_text, 1)
    if updated == original:
        return {"status": "no_change", "message": "File would remain unchanged."}

    diff_text = "\n".join(
        difflib.unified_diff(
            original.splitlines(),
            updated.splitlines(),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
            lineterm="",
        )
    )
    cmd_preview = f"patch_file_text '{path}'"

    console.print(
        Panel(
            Syntax(diff_text, "diff", theme="monokai", word_wrap=True),
            title="[bold yellow]LIVE DIFF PREVIEW (Time-Machine Protected)[/bold yellow]",
            subtitle=f"[dim]{explanation}[/dim]",
            border_style="yellow",
        )
    )
    approval = console.input("[bold yellow]Apply this patch? \\[y/N]: [/bold yellow]")
    if parse_confirmation(approval) == "no":
        log_audit(cmd_preview, "CONFIRM", "DENIED", None)
        return {"status": "denied", "message": "User rejected the file patch."}

    snap_id = create_file_snapshot(path, "text_patch", explanation, requires_sudo=False)
    try:
        path.write_text(updated, encoding="utf-8")
        log_audit(cmd_preview, "CONFIRM", "EXECUTED", 0)
        return {
            "status": "ok",
            "snapshot_id": snap_id,
            "requires_sudo": False,
            "undo_hint": f"nova undo {snap_id}",
        }
    except PermissionError as exc:
        log_audit(cmd_preview, "CONFIRM", "ERROR", 1)
        return {"status": "error", "message": f"Permission denied writing {path}: {exc}"}


def patch_file_with_sed(file_path: str, sed_expression: str, explanation: str) -> dict:
    """Previews a live unified diff for a sed patch (native sed on Linux, Python fallback on Windows), saves a Time-Machine snapshot, and applies after approval."""
    path = Path(file_path).expanduser().resolve()
    if _is_forbidden_path(path):
        return {"status": "blocked", "message": f"Modifying {path} is blocked by NovaShell security policy."}

    if IS_WINDOWS or not shutil.which("sed"):
        if not path.exists() or not path.is_file():
            return {"status": "error", "message": f"File does not exist: {path}"}
        try:
            original = path.read_text(encoding="utf-8", errors="replace")
        except PermissionError:
            return {"status": "error", "message": f"Permission denied reading {path}. Run as Administrator."}

        updated, err = _apply_python_sed(original, sed_expression)
        if err:
            return {"status": "error", "message": err}
        if updated == original:
            return {"status": "no_change", "message": "Expression matched nothing; file would remain unchanged."}

        diff_text = "\n".join(
            difflib.unified_diff(
                original.splitlines(),
                updated.splitlines(),
                fromfile=f"a/{path.name}",
                tofile=f"b/{path.name}",
                lineterm="",
            )
        )
        cmd_preview = f"python-sed '{sed_expression}' '{path}'"
        console.print(
            Panel(
                Syntax(diff_text, "diff", theme="monokai", word_wrap=True),
                title="[bold yellow]LIVE DIFF PREVIEW (Time-Machine Protected)[/bold yellow]",
                subtitle=f"[dim]{explanation}[/dim]",
                border_style="yellow",
            )
        )
        approval = console.input("[bold yellow]Apply this patch? \\[y/N]: [/bold yellow]")
        if parse_confirmation(approval) == "no":
            log_audit(cmd_preview, "CONFIRM", "DENIED", None)
            return {"status": "denied", "message": "User rejected the patch."}

        snap_id = create_file_snapshot(path, "sed_patch", explanation, requires_sudo=False)
        try:
            path.write_text(updated, encoding="utf-8")
            log_audit(cmd_preview, "CONFIRM", "EXECUTED", 0)
            return {
                "status": "ok",
                "snapshot_id": snap_id,
                "requires_sudo": False,
                "undo_hint": f"nova undo {snap_id}",
                "exit_code": 0,
                "stderr": "",
            }
        except PermissionError as exc:
            log_audit(cmd_preview, "CONFIRM", "ERROR", 1)
            return {"status": "error", "message": f"Permission denied writing {path}: {exc}"}

    # --- Native Linux sed + sudo flow ---
    needs_sudo = not os.access(path, os.W_OK) or not os.access(path.parent, os.W_OK)
    readable = os.access(path, os.R_OK)

    if readable and not path.exists():
        return {"status": "error", "message": f"File does not exist: {path}"}

    if readable:
        original = path.read_text(encoding="utf-8", errors="replace")
        dry = subprocess.run(["sed", sed_expression, str(path)], capture_output=True, text=True)
    else:
        console.print(f"[dim]Inspecting root-owned file {path} via sudo for live diff preview...[/dim]")
        orig_proc = subprocess.run(["sudo", "cat", str(path)], capture_output=True, text=True)
        if orig_proc.returncode != 0:
            return {"status": "error", "message": orig_proc.stderr.strip() or f"Cannot read {path}"}
        original = orig_proc.stdout
        dry = subprocess.run(["sudo", "sed", sed_expression, str(path)], capture_output=True, text=True)

    if dry.returncode != 0:
        return {"status": "error", "message": f"Invalid sed expression: {dry.stderr.strip()}"}

    if dry.stdout == original:
        return {"status": "no_change", "message": "sed expression matched nothing; file would remain unchanged."}

    diff_text = "\n".join(
        difflib.unified_diff(
            original.splitlines(),
            dry.stdout.splitlines(),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
            lineterm="",
        )
    )
    prefix = "sudo " if needs_sudo else ""
    cmd_preview = f"{prefix}sed -i '{sed_expression}' '{path}'"
    scope_label = "ROOT / SUDO · " if needs_sudo else ""

    console.print(
        Panel(
            Syntax(diff_text, "diff", theme="monokai", word_wrap=True),
            title=f"[bold yellow]SED LIVE DIFF PREVIEW ({scope_label}Time-Machine Protected)[/bold yellow]",
            subtitle=f"[dim]{explanation}[/dim]",
            border_style="yellow",
        )
    )
    approval = console.input("[bold yellow]Apply this patch? \\[y/N]: [/bold yellow]")
    if parse_confirmation(approval) == "no":
        log_audit(cmd_preview, "CONFIRM", "DENIED", None)
        return {"status": "denied", "message": "User rejected the sed patch."}

    if needs_sudo:
        _APPROVED_SUDO_READS.add(str(path))

    snap_id = create_file_snapshot(path, "sed_patch", explanation, requires_sudo=needs_sudo)
    sed_cmd = (["sudo", "sed"] if needs_sudo else ["sed"]) + ["-i", sed_expression, str(path)]
    proc = subprocess.run(sed_cmd, capture_output=True, text=True)
    log_audit(cmd_preview, "CONFIRM", "EXECUTED", proc.returncode)
    return {
        "status": "ok" if proc.returncode == 0 else "error",
        "snapshot_id": snap_id,
        "requires_sudo": needs_sudo,
        "undo_hint": f"nova undo {snap_id}",
        "exit_code": proc.returncode,
        "stderr": proc.stderr.strip(),
    }


def _execute_os_command(command: str) -> subprocess.CompletedProcess:
    if IS_WINDOWS:
        ps_bin = shutil.which("pwsh") or shutil.which("powershell")
        if ps_bin:
            wrapped = (
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "$OutputEncoding = [System.Text.Encoding]::UTF8; "
                f"{command}"
            )
            return subprocess.run(
                [ps_bin, "-NoProfile", "-NonInteractive", "-Command", wrapped],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
            )
        return subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )

    return subprocess.run(
        command,
        shell=True,
        executable="/bin/bash",
        capture_output=True,
        text=True,
        timeout=90,
    )


def run_shell_command(command: str, explanation: str) -> dict:
    """Executes a shell command (Bash on Linux, PowerShell on Windows) through the Zero-Trust Security Guard with rollback tracking."""
    global _AUTO_APPROVE_TASK
    risk, reason = classify_command(command)

    if risk == "BLOCKED":
        console.print(
            Panel(
                f"[bold red]{command}[/bold red]\n\n[dim]{reason}[/dim]",
                title="[bold red]BLOCKED BY SECURITY GUARD[/bold red]",
                border_style="red",
            )
        )
        log_audit(command, risk, "BLOCKED", None)
        return {"status": "blocked", "message": f"Command blocked by security policy: {reason}"}

    auto_approved = risk == "CONFIRM" and _AUTO_APPROVE_TASK
    if risk == "SAFE":
        badge, border = "[bold green]SAFE · AUTO-RUN[/bold green]", "green"
    elif auto_approved:
        badge, border = "[bold cyan]CONFIRM · AUTO-APPROVED[/bold cyan]", "cyan"
    else:
        badge, border = "[bold yellow]CONFIRM REQUIRED[/bold yellow]", "yellow"

    syntax_lang = "powershell" if IS_WINDOWS else "bash"
    console.print(
        Panel(
            Syntax(command, syntax_lang, theme="monokai", word_wrap=True),
            title=badge,
            subtitle=f"[dim]{explanation}[/dim]",
            border_style=border,
        )
    )

    if risk == "CONFIRM" and not auto_approved:
        choice = console.input("[bold yellow]Execute this command? \\[y/N/a]: [/bold yellow]")
        decision = parse_confirmation(choice)
        if decision == "all":
            _AUTO_APPROVE_TASK = True
        elif decision == "no":
            log_audit(command, risk, "DENIED", None)
            return {"status": "denied", "message": "User declined execution. Ask for an alternative or stop."}

    try:
        proc = _execute_os_command(command)
        log_audit(command, risk, "EXECUTED", proc.returncode)
        stdout = (proc.stdout or "").strip()[-4000:]
        stderr = (proc.stderr or "").strip()[-2000:]

        if stdout:
            console.print(Panel(stdout, title="[cyan]Output[/cyan]", border_style="cyan"))
        if stderr:
            console.print(Panel(stderr, title="[red]Stderr[/red]", border_style="red"))

        result = {
            "status": "ok" if proc.returncode == 0 else "failed",
            "exit_code": proc.returncode,
            "stdout": redact_secrets(stdout)[0],
            "stderr": redact_secrets(stderr)[0],
        }

        if proc.returncode == 0:
            reverse_cmd = detect_reverse_command(command)
            if reverse_cmd:
                snap_id = create_command_snapshot(command, reverse_cmd, explanation)
                result["snapshot_id"] = snap_id
                result["undo_hint"] = f"nova undo {snap_id}"
                console.print(
                    f"[dim]Time-Machine: saved reverse action as Snapshot #{snap_id} (run `nova undo {snap_id}` to revert)[/dim]"
                )

        return result
    except subprocess.TimeoutExpired:
        log_audit(command, risk, "TIMEOUT", -1)
        return {"status": "timeout", "message": "Command timed out after 90 seconds."}


def run_readonly_inspection(command: str, explanation: str) -> dict:
    """Plan-Mode tool: executes ONLY verified read-only commands and rejects any state-modifying command."""
    risk, reason = classify_command(command)
    if risk != "SAFE":
        return {
            "status": "blocked_in_plan_mode",
            "message": f"Plan Mode is strictly read-only. Do not run mutating commands now ({reason}). Include this command inside your final written plan instead.",
        }
    return run_shell_command(command, explanation)
