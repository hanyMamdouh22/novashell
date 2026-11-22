import os
from pathlib import Path

APP_NAME = "NovaShell"
DATA_DIR = Path.home() / ".novashell"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_ENV = DATA_DIR / "config.env"
AUDIT_DB = DATA_DIR / "audit.db"


def load_config_env() -> dict[str, str]:
    """Loads ~/.novashell/config.env into environment without overriding explicit shell exports."""
    loaded: dict[str, str] = {}
    if not CONFIG_ENV.exists():
        return loaded
    for line in CONFIG_ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def save_config_env(values: dict[str, str]) -> Path:
    """Writes configuration atomically with strict 0600 permissions (owner read/write only)."""
    existing = load_config_env()
    existing.update({k: v for k, v in values.items() if v != ""})
    lines = [f'{k}="{v}"' for k, v in sorted(existing.items())]
    CONFIG_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(CONFIG_ENV, 0o600)
    return CONFIG_ENV


load_config_env()

DEFAULT_MODEL = os.getenv("NOVA_MODEL", "gemini-3.5-flash-lite")
API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
STRICT_MODE = os.getenv("NOVA_STRICT_MODE", "false").lower() == "true"
