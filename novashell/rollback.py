import difflib
import json
import os
import shlex
import shutil
import sqlite3
import stat
import subprocess

def _run_reverse_shell(cmd: str) -> subprocess.CompletedProcess:
    if os.name == "nt":
        ps_bin = shutil.which("pwsh") or shutil.which("powershell")
        if ps_bin:
            return subprocess.run(
                [ps_bin, "-NoProfile", "-NonInteractive", "-Command", cmd],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return subprocess.run(cmd, shell=True, executable="/bin/bash", capture_output=True, text=True)
from datetime import datetime, timezone
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from .config import AUDIT_DB, DATA_DIR
from .guard import init_db, log_audit, parse_confirmation

console = Console()
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def init_snapshots_db() -> None:
    init_db()
    with sqlite3.connect(AUDIT_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                action_type TEXT NOT NULL,
                target TEXT NOT NULL,
                backup_path TEXT NOT NULL,
                description TEXT NOT NULL,
                restored INTEGER DEFAULT 0,
                requires_sudo INTEGER DEFAULT 0,
                reverse_cmd TEXT DEFAULT '',
                file_meta TEXT DEFAULT '{}'
            )
            """
        )
        # Safe online migration for existing databases
        cols = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)").fetchall()}
        if "requires_sudo" not in cols:
            conn.execute("ALTER TABLE snapshots ADD COLUMN requires_sudo INTEGER DEFAULT 0")
        if "reverse_cmd" not in cols:
            conn.execute("ALTER TABLE snapshots ADD COLUMN reverse_cmd TEXT DEFAULT ''")
        if "file_meta" not in cols:
            conn.execute("ALTER TABLE snapshots ADD COLUMN file_meta TEXT DEFAULT '{}'")


def _capture_file_meta(file_path: Path, requires_sudo: bool) -> dict:
    try:
        st = os.stat(file_path)
        return {"mode": oct(stat.S_IMODE(st.st_mode))[2:], "uid": st.st_uid, "gid": st.st_gid}
    except PermissionError:
        if requires_sudo:
            proc = subprocess.run(
                ["sudo", "stat", "-c", "%a %u %g", str(file_path)],
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                mode, uid, gid = proc.stdout.strip().split()
                return {"mode": mode, "uid": int(uid), "gid": int(gid)}
    return {}


def create_file_snapshot(
    file_path: Path, action_type: str, description: str, requires_sudo: bool = False
) -> int:
    init_snapshots_db()
    ts = datetime.now(timezone.utc)
    stamp = ts.strftime("%Y%m%d_%H%M%S_%f")
    backup_file = SNAPSHOTS_DIR / f"{stamp}_{file_path.name}.snap"
    meta = _capture_file_meta(file_path, requires_sudo)

    if os.access(file_path, os.R_OK):
        shutil.copy2(file_path, backup_file)
    else:
        proc = subprocess.run(["sudo", "cat", str(file_path)], capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to snapshot root-owned file: {proc.stderr.decode(errors='replace')}")
        backup_file.write_bytes(proc.stdout)
    if os.name != 'nt':
        os.chmod(backup_file, 0o600)

    with sqlite3.connect(AUDIT_DB) as conn:
        cur = conn.execute(
            """
            INSERT INTO snapshots (timestamp, action_type, target, backup_path, description, requires_sudo, file_meta)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts.isoformat(),
                action_type,
                str(file_path),
                str(backup_file),
                description,
                1 if requires_sudo else 0,
                json.dumps(meta),
            ),
        )
        return cur.lastrowid


def _reverse_single_segment(segment: str) -> str | None:
    if any(op in segment for op in ("|", ";", "`", "$(", ">", "<")):
        return None
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return None
    if not tokens:
        return None

    prefix = "sudo " if tokens[0] == "sudo" else ""
    body = tokens[1:] if tokens[0] == "sudo" else tokens
    if len(body) < 2:
        return None

    prog = body[0]

    # apt / apt-get install -> apt-get remove
    if prog in ("apt", "apt-get") and "install" in body[1:]:
        idx = body.index("install")
        pkgs = [t for t in body[idx + 1 :] if not t.startswith("-")]
        if pkgs:
            return f"{prefix}apt-get remove -y " + " ".join(shlex.quote(p) for p in pkgs)

    # systemctl start/stop/enable/disable
    if prog == "systemctl":
        flip = {"start": "stop", "stop": "start", "enable": "disable", "disable": "enable"}
        args = [t for t in body[1:] if not t.startswith("-")]
        if len(args) >= 2 and args[0] in flip:
            units = " ".join(shlex.quote(u) for u in args[1:])
            return f"{prefix}systemctl {flip[args[0]]} {units}"

    # ufw enable/disable/allow/deny
    if prog == "ufw":
        args = [t for t in body[1:] if t != "--force"]
        if not args:
            return None
        if args[0] == "enable":
            return f"{prefix}ufw disable"
        if args[0] == "disable":
            return f"{prefix}ufw --force enable"
        if args[0] in ("allow", "deny") and len(args) >= 2:
            rule = " ".join(shlex.quote(r) for r in args[1:])
            return f"{prefix}ufw delete {args[0]} {rule}"

    # dnf / yum install -> dnf remove
    if prog in ("dnf", "yum") and "install" in body[1:]:
        idx = body.index("install")
        pkgs = [t for t in body[idx + 1 :] if not t.startswith("-")]
        if pkgs:
            return f"{prefix}{prog} remove -y " + " ".join(shlex.quote(p) for p in pkgs)

    # firewall-cmd --add-port / --remove-port / --add-service / --remove-service
    if prog == "firewall-cmd":
        perm = "--permanent " if "--permanent" in body[1:] else ""
        zone_args = [t for t in body[1:] if t.startswith("--zone=")]
        zone = f"{shlex.quote(zone_args[0])} " if zone_args else ""
        flip_flags = (
            ("--add-port=", "--remove-port="),
            ("--remove-port=", "--add-port="),
            ("--add-service=", "--remove-service="),
            ("--remove-service=", "--add-service="),
        )
        for token in body[1:]:
            for src_flag, dst_flag in flip_flags:
                if token.startswith(src_flag):
                    val = token[len(src_flag):]
                    cmd = f"{prefix}firewall-cmd {perm}{zone}{dst_flag}{shlex.quote(val)}"
                    if perm:
                        cmd += f" && {prefix}firewall-cmd --reload"
                    return cmd

    return None


def detect_reverse_command(command: str) -> str | None:
    """Detects a clean rollback command for reversible apt, systemctl, and ufw operations."""
    segments = [s.strip() for s in command.split("&&") if s.strip()]
    reverses: list[str] = []
    for seg in segments:
        rev = _reverse_single_segment(seg)
        if rev:
            reverses.append(rev)
    if not reverses:
        return None
    reverses.reverse()
    return " && ".join(reverses)


def create_command_snapshot(original_cmd: str, reverse_cmd: str, description: str) -> int:
    init_snapshots_db()
    ts = datetime.now(timezone.utc)
    requires_sudo = 1 if "sudo " in reverse_cmd else 0
    with sqlite3.connect(AUDIT_DB) as conn:
        cur = conn.execute(
            """
            INSERT INTO snapshots (timestamp, action_type, target, backup_path, description, requires_sudo, reverse_cmd)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ts.isoformat(), "command_undo", original_cmd, "", description, requires_sudo, reverse_cmd),
        )
        return cur.lastrowid


def show_audit_log(limit: int = 20) -> None:
    init_db()
    with sqlite3.connect(AUDIT_DB) as conn:
        rows = conn.execute(
            "SELECT id, timestamp, risk, decision, exit_code, command FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    if not rows:
        console.print("[dim]No audit records yet.[/dim]")
        return

    table = Table(title=f"NovaShell Audit Log (Last {len(rows)})", border_style="cyan")
    table.add_column("ID", style="dim", justify="right")
    table.add_column("UTC Time", style="dim")
    table.add_column("Risk")
    table.add_column("Decision")
    table.add_column("Exit", justify="center")
    table.add_column("Command", overflow="fold")

    risk_colors = {"SAFE": "green", "CONFIRM": "yellow", "BLOCKED": "red"}
    for rid, ts, risk, decision, code, cmd in rows:
        color = risk_colors.get(risk, "white")
        exit_str = "-" if code is None else ("[green]0[/green]" if code == 0 else f"[red]{code}[/red]")
        table.add_row(str(rid), ts[:19].replace("T", " "), f"[{color}]{risk}[/{color}]", decision, exit_str, cmd)

    console.print(table)


def show_snapshots(limit: int = 15) -> None:
    init_snapshots_db()
    with sqlite3.connect(AUDIT_DB) as conn:
        rows = conn.execute(
            """
            SELECT id, timestamp, action_type, target, description, restored, requires_sudo
            FROM snapshots ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    if not rows:
        console.print("[dim]No Time-Machine snapshots saved yet.[/dim]")
        return

    table = Table(title="Time-Machine Snapshots", border_style="magenta")
    table.add_column("ID", style="bold cyan", justify="right")
    table.add_column("UTC Time", style="dim")
    table.add_column("Type")
    table.add_column("Target", overflow="fold")
    table.add_column("Scope")
    table.add_column("Status")
    table.add_column("Description", overflow="fold")

    for sid, ts, action, target, desc, restored, req_sudo in rows:
        scope = "[yellow]ROOT[/yellow]" if req_sudo else "[dim]USER[/dim]"
        status = "[dim]RESTORED[/dim]" if restored else "[green]ACTIVE[/green]"
        table.add_row(str(sid), ts[:19].replace("T", " "), action, target, scope, status, desc)

    console.print(table)


def _read_current_lines(target: Path, requires_sudo: bool) -> list[str]:
    if not target.exists():
        return []
    try:
        return target.read_text(encoding="utf-8", errors="replace").splitlines()
    except PermissionError:
        if requires_sudo:
            proc = subprocess.run(["sudo", "cat", str(target)], capture_output=True, text=True)
            if proc.returncode == 0:
                return proc.stdout.splitlines()
        return []


def undo_snapshot(snapshot_id: int | None = None) -> None:
    init_snapshots_db()
    with sqlite3.connect(AUDIT_DB) as conn:
        if snapshot_id is not None:
            row = conn.execute(
                """
                SELECT id, action_type, target, backup_path, description, restored, requires_sudo, reverse_cmd, file_meta
                FROM snapshots WHERE id = ?
                """,
                (snapshot_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id, action_type, target, backup_path, description, restored, requires_sudo, reverse_cmd, file_meta
                FROM snapshots WHERE restored = 0 ORDER BY id DESC LIMIT 1
                """
            ).fetchone()

    if not row:
        console.print("[yellow]No eligible snapshot found to undo.[/yellow]")
        return

    sid, action_type, target_str, backup_str, desc, _, req_sudo, reverse_cmd, meta_json = row

    # Case 1: Command-level rollback (apt / systemctl / ufw)
    if action_type == "command_undo" and reverse_cmd:
        console.print(
            Panel(
                Syntax(reverse_cmd, "bash", theme="monokai", word_wrap=True),
                title=f"[bold magenta]TIME-MACHINE COMMAND ROLLBACK · Snapshot #{sid}[/bold magenta]",
                subtitle=f"[dim]Reversing: {target_str}[/dim]",
                border_style="magenta",
            )
        )
        confirm = console.input("[bold magenta]Execute this rollback command? \\[y/N]: [/bold magenta]")
        if parse_confirmation(confirm) == "no":
            console.print("[dim]Rollback cancelled.[/dim]")
            return

        proc = _run_reverse_shell(reverse_cmd)
        if proc.stdout.strip():
            console.print(Panel(proc.stdout.strip()[-3000:], title="[cyan]Output[/cyan]", border_style="cyan"))
        if proc.stderr.strip():
            console.print(Panel(proc.stderr.strip()[-1500:], title="[red]Stderr[/red]", border_style="red"))

        if proc.returncode == 0:
            with sqlite3.connect(AUDIT_DB) as conn:
                conn.execute("UPDATE snapshots SET restored = 1 WHERE id = ?", (sid,))
            log_audit(f"nova undo {sid}: {reverse_cmd}", "CONFIRM", "RESTORED", 0)
            console.print(f"[bold green]Rollback #{sid} completed successfully.[/bold green]")
        else:
            log_audit(f"nova undo {sid}: {reverse_cmd}", "CONFIRM", "FAILED", proc.returncode)
            console.print(f"[bold red]Rollback command failed with exit code {proc.returncode}.[/bold red]")
        return

    # Case 2: File-level snapshot rollback (user or root-owned)
    target = Path(target_str)
    backup = Path(backup_str)
    if not backup.exists():
        console.print(f"[bold red]Snapshot file missing:[/bold red] {backup}")
        return

    current_lines = _read_current_lines(target, bool(req_sudo))
    backup_lines = backup.read_text(encoding="utf-8", errors="replace").splitlines()
    diff_text = "\n".join(
        difflib.unified_diff(
            current_lines,
            backup_lines,
            fromfile=f"current: {target.name}",
            tofile=f"snapshot #{sid}",
            lineterm="",
        )
    )

    scope_tag = " · ROOT/SUDO" if req_sudo else ""
    console.print(
        Panel(
            Syntax(diff_text or "(File content is identical to snapshot)", "diff", theme="monokai", word_wrap=True),
            title=f"[bold magenta]TIME-MACHINE ROLLBACK · Snapshot #{sid}{scope_tag}[/bold magenta]",
            subtitle=f"[dim]{target} — {desc}[/dim]",
            border_style="magenta",
        )
    )

    confirm = console.input("[bold magenta]Restore this snapshot? \\[y/N]: [/bold magenta]")
    if parse_confirmation(confirm) == "no":
        console.print("[dim]Rollback cancelled.[/dim]")
        return

    needs_sudo = bool(req_sudo) or not os.access(target.parent, os.W_OK) or (
        target.exists() and not os.access(target, os.W_OK)
    )

    if needs_sudo and os.name != 'nt':
        proc = subprocess.run(["sudo", "cp", str(backup), str(target)], capture_output=True, text=True)
        if proc.returncode != 0:
            console.print(f"[bold red]Sudo restore failed:[/bold red] {proc.stderr.strip()}")
            return
        meta = json.loads(meta_json or "{}")
        if "uid" in meta and "gid" in meta:
            subprocess.run(["sudo", "chown", f"{meta['uid']}:{meta['gid']}", str(target)], capture_output=True)
        if "mode" in meta:
            subprocess.run(["sudo", "chmod", str(meta["mode"]), str(target)], capture_output=True)
    else:
        shutil.copy2(backup, target)

    with sqlite3.connect(AUDIT_DB) as conn:
        conn.execute("UPDATE snapshots SET restored = 1 WHERE id = ?", (sid,))
    log_audit(f"nova undo {sid} ({target})", "CONFIRM", "RESTORED", 0)
    console.print(f"[bold green]Restored[/bold green] [cyan]{target}[/cyan] to snapshot #{sid}.")


def export_audit_log(fmt: str = "csv", output_path: str | None = None) -> None:
    """Exports the full SQLite audit trail to CSV or JSON for compliance & management reporting."""
    import csv

    init_db()
    fmt = fmt.lower().strip()
    if fmt not in ("csv", "json"):
        console.print("[bold red]Unsupported export format.[/bold red] Use [cyan]csv[/cyan] or [cyan]json[/cyan].")
        return

    with sqlite3.connect(AUDIT_DB) as conn:
        rows = conn.execute(
            "SELECT id, timestamp, risk, decision, exit_code, command FROM audit_log ORDER BY id DESC"
        ).fetchall()

    if not rows:
        console.print("[dim]No audit records to export yet.[/dim]")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = Path(output_path).expanduser().resolve() if output_path else Path.cwd() / f"novashell_audit_{stamp}.{fmt}"
    headers = ["id", "timestamp_utc", "risk", "decision", "exit_code", "command"]

    if fmt == "csv":
        with dest.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(headers)
            writer.writerows(rows)
    else:
        records = [dict(zip(headers, row)) for row in rows]
        dest.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")

    console.print(
        Panel(
            f"[bold green]Audit report exported successfully![/bold green]\n"
            f"• Format: [cyan]{fmt.upper()}[/cyan]\n"
            f"• Records: [cyan]{len(rows)}[/cyan]\n"
            f"• File: [cyan]{dest}[/cyan]",
            title="[bold cyan]NovaShell Compliance Export[/bold cyan]",
            border_style="green",
        )
    )
