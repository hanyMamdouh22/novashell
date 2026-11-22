import os
import re
import shlex
import sqlite3
from datetime import datetime, timezone
from typing import Tuple
from .config import AUDIT_DB, STRICT_MODE

BLOCKED_PATTERNS = [
    # --- Linux catastrophic / RCE rules ---
    r"\brm\s+[^\n]*(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|--no-preserve-root)[^\n]*\s+(/\*?|/(etc|boot|usr|var|lib|bin|sbin|root)\b|~|\$HOME)(\s|$)",
    r"\brm\s+[^\n]*\s+/\s*$",
    r"\b(mkfs(\.[a-z0-9]+)?|wipefs|fdisk|parted|gdisk)\b",
    r"\bdd\s+[^\n]*(of=/dev/(sd|nvme|vd|hd|loop)|if=/dev/(zero|urandom))",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"\b(chmod|chown)\s+[^\n]*-R[^\n]*\s+(/\*?|/etc)\s*$",
    r"\b(shutdown|reboot|poweroff|halt|init\s+[06]|systemctl\s+(poweroff|reboot|halt))\b",
    r"\b(curl|wget)\b[^\n]*\|\s*(sudo\s+)?(ba|da|z|k)?sh\b",
    r"\b(curl|wget)\b[^\n]*\|\s*(sudo\s+)?python3?\b",
    r">{1,2}\s*/etc/(passwd|shadow|gshadow|sudoers|fstab|group)\b",
    r">{1,2}\s*/dev/(sd|nvme|vd|hd)[a-z0-9]*",
    # --- Windows / PowerShell catastrophic & RCE rules ---
    r"\b(remove-item|ri|rd|rmdir|del|erase)\b[^\n]*(-recurse|-r|/s)[^\n]*([a-z]:\\($|\s|windows\b|program\s+files|system32|users\s*$)|\$env:systemroot)",
    r"\b(format-volume|clear-disk|initialize-disk|diskpart|cipher\s+/w)\b",
    r"\bformat\s+[a-z]:",
    r"\breg\s+delete\s+[^\n]*(hklm|hkey_local_machine)\b",
    r"\bremove-item\s+[^\n]*hklm:",
    r"\bbcdedit\s+/delete\b",
    r"\b(stop-computer|restart-computer)\b",
    r"\b(irm|invoke-restmethod|iwr|invoke-webrequest)\b[^\n]*\|\s*(iex|invoke-expression)\b",
    r"\b(iex|invoke-expression)\b[^\n]*\([^\n]*(irm|invoke-restmethod|iwr|invoke-webrequest|downloadstring)",
]

SAFE_SINGLE_CMDS = {
    # Linux read-only commands
    "ls", "pwd", "whoami", "id", "uname", "df", "du", "free", "uptime",
    "ps", "ss", "cat", "head", "tail", "wc", "grep", "which",
    "lsb_release", "lsblk", "hostnamectl", "journalctl", "date", "timedatectl",
    # Windows & PowerShell read-only commands (matched case-insensitively)
    "dir", "hostname", "ver", "systeminfo", "ipconfig", "netstat",
    "tasklist", "where", "getmac", "driverquery",
    "get-service", "get-process", "get-childitem", "gci", "get-location", "gl",
    "get-date", "get-volume", "get-disk", "get-partition", "get-psdrive",
    "get-netipaddress", "get-netipconfiguration", "get-nettcpconnection",
    "get-netudpendpoint", "get-netfirewallprofile", "get-netfirewallrule",
    "get-computerinfo", "get-ciminstance", "get-eventlog", "get-winevent",
    "get-content", "gc", "get-hotfix", "test-connection", "test-netconnection",
    "resolve-dnsname",
}

SAFE_SUBCMDS = {
    "systemctl": {"status", "--failed", "list-units", "list-unit-files", "is-active", "is-enabled", "show"},
    "docker": {"ps", "images", "info", "version", "stats", "logs", "inspect"},
    "git": {"status", "log", "diff", "branch", "remote"},
    "ip": {"a", "addr", "address", "r", "route", "link", "-br"},
    "top": {"-bn1"},
    "dnf": {"list", "info", "search", "repolist", "check-update", "history"},
    "rpm": {"-qa", "-qi", "-ql", "-qf"},
    "firewall-cmd": {"--state", "--list-all", "--list-ports", "--list-services", "--get-active-zones", "--get-default-zone"},
    "sc": {"query", "queryex", "qc"},
    "sc.exe": {"query", "queryex", "qc"},
    "winget": {"list", "search", "show"},
}

SENSITIVE_PATH_HINTS = (
    "/etc/shadow", "/etc/gshadow", "/etc/sudoers",
    ".ssh/id_", ".aws/credentials", "config.env", ".env",
    "system32/config", "ntds.dit", "sam",
)

HARD_METACHARS = ("|", ";", "`", "$(", ">", "<", "\n", "&")

YES_TOKENS = {
    "y", "yes", "ok", "okay", "sure", "go", "run",
    "نعم", "ايوه", "أيوة", "ايوا", "اه", "آه",
    "تمام", "ماشي", "كمل", "موافق", "نفذ", "نفّذ", "طبق", "طبّق", "اوك", "اوكي",
}

ALL_TOKENS = {"a", "all", "الكل", "كله", "نفذ الكل"}


def init_db() -> None:
    with sqlite3.connect(AUDIT_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                command TEXT NOT NULL,
                risk TEXT NOT NULL,
                decision TEXT NOT NULL,
                exit_code INTEGER
            )
            """
        )


def parse_confirmation(raw: str) -> str:
    """Normalizes English/Arabic confirmations into 'yes', 'all', or 'no'."""
    token = raw.strip().lower()
    if token in ALL_TOKENS:
        return "all"
    if token in YES_TOKENS:
        return "yes"
    return "no"


def _touches_sensitive_path(tokens: list[str]) -> bool:
    for tok in tokens[1:]:
        normalized = tok.lower().replace("\\", "/")
        if any(hint in normalized for hint in SENSITIVE_PATH_HINTS):
            return True
    return False


def _is_safe_segment(segment: str) -> bool:
    if not segment or any(op in segment for op in HARD_METACHARS):
        return False
    try:
        tokens = shlex.split(segment, posix=(os.name != "nt"))
    except ValueError:
        return False
    if not tokens:
        return False

    cmd = tokens[0].lower()
    if cmd in {"sudo", "runas", "su"}:
        return False
    if _touches_sensitive_path(tokens):
        return False

    if cmd in SAFE_SINGLE_CMDS:
        return True
    if cmd in SAFE_SUBCMDS and len(tokens) >= 2:
        sub = tokens[1].lower()
        if sub in SAFE_SUBCMDS[cmd]:
            return True
    return False


def classify_command(command: str) -> Tuple[str, str]:
    cmd = command.strip()
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, cmd, flags=re.IGNORECASE):
            return "BLOCKED", "Matched catastrophic, RCE, or protected-system security rule."

    if not STRICT_MODE:
        segments = [seg.strip() for seg in cmd.split("&&")]
        if segments and all(_is_safe_segment(seg) for seg in segments):
            return "SAFE", "Verified read-only command chain."

    return "CONFIRM", "Command modifies state, uses sudo/elevation, or accesses sensitive paths."


def log_audit(command: str, risk: str, decision: str, exit_code: int | None = None) -> None:
    init_db()
    with sqlite3.connect(AUDIT_DB) as conn:
        conn.execute(
            "INSERT INTO audit_log (timestamp, command, risk, decision, exit_code) VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), command, risk, decision, exit_code),
        )


SECRET_REDACTION_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9\-._~+/]+=*"), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:API_KEY|SECRET|PASSWORD|PASSWD|TOKEN|PRIVATE_KEY)[A-Z0-9_]*\s*[:=]\s*)([^\s'\"`]+|\"[^\"]*\"|'[^']*')"), r"\1[REDACTED]"),
    (re.compile(r"\b(?:AIza[0-9A-Za-z\-_]{35}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{36}|AKIA[0-9A-Z]{16})\b"), "[REDACTED_KEY]"),
]


def redact_secrets(text: str) -> tuple[str, int]:
    """Redacts credentials, tokens, and private keys before any output is sent to the LLM."""
    if not text:
        return text, 0
    redacted = text
    total_hits = 0
    for pattern, replacement in SECRET_REDACTION_PATTERNS:
        redacted, count = pattern.subn(replacement, redacted)
        total_hits += count
    return redacted, total_hits
