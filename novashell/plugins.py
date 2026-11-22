import importlib.util
import inspect
from pathlib import Path
from typing import Callable
from rich.console import Console
from rich.table import Table
from .config import DATA_DIR

console = Console()
PLUGINS_DIR = DATA_DIR / "plugins"
PLUGINS_DIR.mkdir(parents=True, exist_ok=True)


def nova_tool(func: Callable | None = None, *, read_only: bool = False):
    """Decorator to expose a Python function dynamically to NovaShell's Gemini agent."""
    def decorator(fn: Callable) -> Callable:
        fn._is_nova_tool = True
        fn._nova_read_only = read_only
        return fn

    if func is not None:
        return decorator(func)
    return decorator


def load_plugins(read_only_only: bool = False) -> tuple[list[Callable], list[dict]]:
    """Scans ~/.novashell/plugins/*.py and dynamically loads all @nova_tool functions."""
    tools: list[Callable] = []
    registry: list[dict] = []

    for plugin_file in sorted(PLUGINS_DIR.glob("*.py")):
        if plugin_file.name.startswith("_"):
            continue
        mod_name = f"novashell_plugin_{plugin_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, plugin_file)
            if not spec or not spec.loader:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            for name, obj in inspect.getmembers(module, inspect.isfunction):
                if getattr(obj, "_is_nova_tool", False):
                    is_ro = getattr(obj, "_nova_read_only", False)
                    if read_only_only and not is_ro:
                        continue
                    tools.append(obj)
                    doc = (inspect.getdoc(obj) or "No description").splitlines()[0]
                    registry.append({
                        "plugin": plugin_file.name,
                        "tool": name,
                        "mode": "READ-ONLY" if is_ro else "ACTION",
                        "description": doc,
                    })
        except Exception as exc:
            registry.append({
                "plugin": plugin_file.name,
                "tool": "(load error)",
                "mode": "ERROR",
                "description": str(exc),
            })

    return tools, registry


def show_plugins() -> None:
    _, registry = load_plugins(read_only_only=False)
    if not registry:
        console.print(f"[dim]No plugins installed in {PLUGINS_DIR}[/dim]")
        return

    table = Table(title=f"Installed NovaShell Plugins ({PLUGINS_DIR})", border_style="cyan")
    table.add_column("Plugin File", style="cyan")
    table.add_column("Tool Name", style="bold white")
    table.add_column("Mode")
    table.add_column("Description", overflow="fold")

    mode_colors = {"READ-ONLY": "green", "ACTION": "yellow", "ERROR": "red"}
    for item in registry:
        color = mode_colors.get(item["mode"], "white")
        table.add_row(
            item["plugin"],
            item["tool"],
            f"[{color}]{item['mode']}[/{color}]",
            item["description"],
        )

    console.print(table)
