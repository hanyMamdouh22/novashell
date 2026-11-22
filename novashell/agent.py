import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Callable
from google import genai
from google.genai import types
from google.genai.errors import APIError
from rich.console import Console
from .config import API_KEY, APP_NAME, AUDIT_DB
from .context import collect_system_context
from .plugins import load_plugins
from .tools import (
    get_system_snapshot,
    patch_file_text,
    patch_file_with_sed,
    read_file_content,
    run_readonly_inspection,
    run_shell_command,
)

console = Console()

EXCLUDED_KEYWORDS = (
    "embedding", "imagen", "veo", "tts", "audio", "live",
    "image", "robotics", "computer-use", "deep-research", "aqa",
    "transcribe", "nano-banana", "omni",
)

EXPERIMENTAL_CODENAMES = ("argon", "carbon", "hydrogen", "neon")
MAX_REACT_STEPS = 12
MEMORY_WINDOW = 16


def init_memory_db() -> None:
    with sqlite3.connect(AUDIT_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL
            )
            """
        )


def load_session_history(limit: int = MEMORY_WINDOW) -> list[types.Content]:
    init_memory_db()
    with sqlite3.connect(AUDIT_DB) as conn:
        rows = conn.execute(
            "SELECT role, text FROM chat_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    rows.reverse()
    history = [
        types.Content(role=role, parts=[types.Part.from_text(text=text)])
        for role, text in rows
    ]
    if history and history[0].role == "model":
        history = history[1:]
    return history


def save_memory_turn(role: str, text: str) -> None:
    if not text.strip():
        return
    init_memory_db()
    with sqlite3.connect(AUDIT_DB) as conn:
        conn.execute(
            "INSERT INTO chat_history (timestamp, role, text) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), role, text.strip()[:4000]),
        )


def clear_session_history() -> int:
    init_memory_db()
    with sqlite3.connect(AUDIT_DB) as conn:
        cur = conn.execute("DELETE FROM chat_history")
        return cur.rowcount


def _model_rank(name: str) -> tuple:
    is_codename = 1 if any(c in name for c in EXPERIMENTAL_CODENAMES) else 0
    ver_match = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    version = float(ver_match.group(1)) if ver_match else 0.0

    if "customtools" in name:
        tier = 4
    elif "flash-lite" in name:
        tier = 3
    elif "flash" in name:
        tier = 2
    elif "pro" in name:
        tier = 1
    else:
        tier = 0

    is_stable = 0 if any(tag in name for tag in ("exp", "preview", "thinking")) else 1
    return (1 - is_codename, tier, is_stable, version, name)


def discover_available_models(client: genai.Client) -> list[str]:
    pinned = os.getenv("NOVA_MODEL", "").strip()
    found = []
    try:
        for m in client.models.list():
            raw = (m.name or "").replace("models/", "")
            if not raw.startswith("gemini-"):
                continue
            if any(bad in raw for bad in EXCLUDED_KEYWORDS):
                continue
            actions = getattr(m, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            found.append(raw)
    except Exception:
        pass

    ordered = sorted(set(found), key=_model_rank, reverse=True)
    if pinned:
        ordered = [pinned] + [m for m in ordered if m != pinned]
    return ordered or ["gemini-3.5-flash-lite", "gemini-3.1-pro-preview-customtools", "gemini-3.8-flash"]


def build_system_instruction(plan_mode: bool = False) -> str:
    ctx_dict = collect_system_context()
    is_win = ctx_dict.get("platform") == "windows"
    ctx = json.dumps(ctx_dict, indent=2)
    os_role = "Windows (PowerShell)" if is_win else "Linux (Ubuntu / RHEL)"
    platform_rule = (
        "3. You are running on Windows: use native PowerShell commands (never Linux commands like ls, grep, sudo, or systemctl). For file edits, prefer `patch_file_text` so a live diff preview and Time-Machine snapshot (`nova undo`) are created automatically."
        if is_win
        else "3. For surgical edits to existing config or text files, prefer `patch_file_with_sed` or `patch_file_text` so a live diff preview and a Time-Machine snapshot (`nova undo`) are created automatically."
    )
    base = f"""You are {APP_NAME}, an elite autonomous {os_role} Systems Engineer AI Agent.

Live System Context:
{ctx}

Core Operating Rules:
1. Understand user intent naturally (Arabic, Egyptian Arabic, or English) and reply in the same language.
2. NEVER guess system state — inspect first using available read-only tools or plugins.
{platform_rule}
4. Self-Healing: If a command returns a non-zero exit code, analyze `stderr`, diagnose the root cause, and propose/execute the fix automatically.
5. Keep commands clean, idempotent, and minimal. Explain clearly what changed and verify the final state.
6. Respect user refusals immediately — never retry a denied command without modifying the approach.
"""
    if plan_mode:
        base += """
STRICT PLAN MODE (READ-ONLY ARCHITECTURE):
- You are currently in PLAN MODE. Do NOT modify files, install packages, or alter services.
- Inspect the live system using read-only tools first, then output a clear, numbered Execution Plan containing:
  1. Target goal & current state summary.
  2. Numbered steps with exact commands/patches and Risk Level (LOW / MEDIUM / HIGH).
  3. Rollback & verification strategy for each risky step.
"""
    return base


def _extract_visible_text(response) -> str:
    if response.candidates and response.candidates[0].content:
        chunks = [
            p.text
            for p in (response.candidates[0].content.parts or [])
            if getattr(p, "text", None) and not getattr(p, "thought", False)
        ]
        if chunks:
            return "\n".join(chunks).strip()
    return (response.text or "").strip()


def _flatten_history_for_failover(history: list[types.Content]) -> list[types.Content]:
    clean: list[types.Content] = []
    for content in history:
        lines: list[str] = []
        for part in content.parts or []:
            if getattr(part, "thought", False):
                continue
            if getattr(part, "text", None):
                lines.append(part.text)
            elif getattr(part, "function_call", None):
                fc = part.function_call
                lines.append(f"[Already called tool `{fc.name}` with args: {json.dumps(dict(fc.args or {}), ensure_ascii=False)}]")
            elif getattr(part, "function_response", None):
                fr = part.function_response
                lines.append(
                    f"[Tool `{fr.name}` ALREADY EXECUTED — do not repeat it unless needed. Result: {json.dumps(fr.response, ensure_ascii=False)}]"
                )
        if lines:
            role = "model" if content.role == "model" else "user"
            merged = "\n".join(lines)
            if clean and clean[-1].role == role:
                clean[-1] = types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=f"{clean[-1].parts[0].text}\n{merged}")],
                )
            else:
                clean.append(types.Content(role=role, parts=[types.Part.from_text(text=merged)]))
    return clean


class NovaAgent:
    def __init__(self, model: str | None = None, plan_mode: bool = False, persist_memory: bool = True):
        if not API_KEY:
            raise RuntimeError(
                "GEMINI_API_KEY not found. Run `nova setup` to configure your API key."
            )
        self.plan_mode = plan_mode
        self.persist_memory = persist_memory and not plan_mode
        self.client = genai.Client(
            api_key=API_KEY,
            http_options=types.HttpOptions(api_version="v1beta"),
        )
        self.models = discover_available_models(self.client)
        if model:
            self.models = [model] + [m for m in self.models if m != model]
        self.model_idx = 0
        self.model = self.models[0]
        self.plugin_tools, self.plugin_registry = load_plugins(read_only_only=plan_mode)
        self.tools = self._active_tools()
        self.tool_map: dict[str, Callable] = {fn.__name__: fn for fn in self.tools}
        self.system_instruction = build_system_instruction(plan_mode=plan_mode)
        self.history: list[types.Content] = load_session_history() if self.persist_memory else []

    def _active_tools(self) -> list[Callable]:
        if self.plan_mode:
            core = [get_system_snapshot, read_file_content, run_readonly_inspection]
        else:
            core = [get_system_snapshot, read_file_content, patch_file_text, patch_file_with_sed, run_shell_command]
        return core + self.plugin_tools

    def _request_config(self) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=self.system_instruction,
            tools=self.tools,
            temperature=0.2,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def _call_model_resilient(self):
        last_error = None
        backoff_schedule = (2.0, 4.0)

        while self.model_idx < len(self.models):
            for attempt in range(len(backoff_schedule) + 1):
                try:
                    return self.client.models.generate_content(
                        model=self.model,
                        contents=self.history,
                        config=self._request_config(),
                    )
                except APIError as exc:
                    last_error = exc
                    if exc.code in (500, 503, 504) and attempt < len(backoff_schedule):
                        wait_s = backoff_schedule[attempt]
                        console.print(
                            f"[dim]HTTP {exc.code} (transient) on {self.model} — retrying in {wait_s:.0f}s ({attempt + 1}/{len(backoff_schedule)})...[/dim]"
                        )
                        time.sleep(wait_s)
                        continue

                    if exc.code in (400, 404, 429, 500, 503, 504) and self.model_idx + 1 < len(self.models):
                        old_model = self.model
                        self.model_idx += 1
                        self.model = self.models[self.model_idx]
                        self.history = _flatten_history_for_failover(self.history)
                        console.print(
                            f"[bold yellow]HTTP {exc.code} on {old_model} — failing over to {self.model} (state preserved)...[/bold yellow]"
                        )
                        break
                    raise
        raise last_error

    def ask(self, prompt: str) -> str:
        if len(self.history) > 24:
            trimmed = _flatten_history_for_failover(self.history[-24:])
            self.history = trimmed[1:] if trimmed and trimmed[0].role == "model" else trimmed

        self.history.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))
        if self.persist_memory:
            save_memory_turn("user", prompt)

        for _ in range(MAX_REACT_STEPS):
            response = self._call_model_resilient()
            if response.candidates and response.candidates[0].content:
                self.history.append(response.candidates[0].content)

            function_calls = response.function_calls or []
            if not function_calls:
                reply = _extract_visible_text(response)
                if self.persist_memory and reply:
                    save_memory_turn("model", reply)
                return reply

            response_parts: list[types.Part] = []
            for fc in function_calls:
                fn = self.tool_map.get(fc.name)
                if not fn:
                    result = {"status": "error", "message": f"Unknown tool: {fc.name}"}
                else:
                    try:
                        args = dict(fc.args) if fc.args else {}
                        raw_res = fn(**args)
                        result = raw_res if isinstance(raw_res, dict) else {"result": raw_res}
                    except Exception as exc:
                        result = {"status": "error", "message": str(exc)}
                response_parts.append(
                    types.Part.from_function_response(name=fc.name, response=result)
                )

            self.history.append(types.Content(role="user", parts=response_parts))

        return "Reached maximum tool-execution steps for this turn. Run `nova audit` to review executed commands."
