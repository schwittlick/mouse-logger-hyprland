"""Install / remove the systemd user unit."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "mouse-logger.service"


def unit_path() -> Path:
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(cfg) / "systemd" / "user" / UNIT_NAME


def executable() -> str:
    exe = Path(sys.argv[0]).resolve()
    if exe.name == "mouse-logger" and exe.exists():
        return str(exe)
    return f"{sys.executable} -m mouse_logger"


def render_unit(exec_path: str, hz: int, db: Path | None) -> str:
    args = f"run --hz {hz}"
    if db is not None:
        args += f" --db {db}"
    return f"""[Unit]
Description=Passive mouse movement logger (Hyprland)
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
ExecStart={exec_path} {args}
Restart=always
RestartSec=3
Nice=5

[Install]
WantedBy=graphical-session.target
"""


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)


def install(hz: int, db: Path | None) -> None:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    exe = executable()
    path.write_text(render_unit(exe, hz, db))
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", UNIT_NAME)
    print(f"installed {path}")
    print(f"ExecStart: {exe}")
    print(f"status:  systemctl --user status {UNIT_NAME}")
    print(f"logs:    journalctl --user -u {UNIT_NAME} -f")


def uninstall() -> None:
    path = unit_path()
    subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
    if path.exists():
        path.unlink()
        print(f"removed {path}")
    _systemctl("daemon-reload")
