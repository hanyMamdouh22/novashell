import ctypes
import os
import platform
import shutil
import subprocess


def _is_privileged() -> bool:
    if os.name == "nt":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def collect_system_context() -> dict:
    """Dynamically inspects live Linux or Windows environment so Gemini adapts to this machine."""
    if os.name == "nt":
        os_name = f"Windows {platform.release()} ({platform.version()})"
        tools = [
            "powershell", "pwsh", "cmd", "winget", "choco", "git",
            "docker", "curl", "python", "netstat", "ipconfig", "tasklist", "sc",
        ]
        installed = [t for t in tools if shutil.which(t)]
        shell = (
            "powershell"
            if shutil.which("powershell") or shutil.which("pwsh")
            else os.getenv("COMSPEC", "cmd.exe")
        )
        user = os.getenv("USERNAME", "unknown")
    else:
        os_name = "Linux"
        try:
            out = subprocess.check_output(
                ["lsb_release", "-ds"], text=True, stderr=subprocess.DEVNULL
            )
            os_name = out.strip().strip('"')
        except Exception:
            try:
                os_name = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
            except Exception:
                pass

        tools = [
            "apt", "dnf", "systemctl", "docker", "git", "ufw",
            "firewall-cmd", "ss", "sed", "awk", "curl", "snap",
        ]
        installed = [t for t in tools if shutil.which(t)]
        shell = os.getenv("SHELL", "/bin/bash")
        user = os.getenv("USER", "unknown")

    return {
        "platform": "windows" if os.name == "nt" else "linux",
        "os": os_name,
        "kernel": platform.release(),
        "arch": platform.machine(),
        "user": user,
        "is_root": _is_privileged(),
        "cwd": os.getcwd(),
        "shell": shell,
        "available_tools": installed,
    }
