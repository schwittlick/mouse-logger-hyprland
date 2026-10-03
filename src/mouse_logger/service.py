"""Install / remove the systemd user unit."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "mouse-logger.service"
EXPORT_SERVICE = "mouse-logger-export.service"
EXPORT_TIMER = "mouse-logger-export.timer"
FOUNTAIN_SERVICE = "mouse-logger-fountain.service"


def unit_dir() -> Path:
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(cfg) / "systemd" / "user"


def unit_path() -> Path:
    return unit_dir() / UNIT_NAME


def executable() -> str:
    exe = Path(sys.argv[0]).resolve()
    if exe.name == "mouse-logger" and exe.exists():
        return str(exe)
    return f"{sys.executable} -m mouse_logger"


def render_unit(exec_path: str, hz: int, db: Path | None, live: bool = True) -> str:
    args = f"run --hz {hz}"
    if db is not None:
        args += f" --db {db}"
    if not live:
        args += " --no-live"
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


def render_export_units(exec_path: str, data_dir: Path | None, db: Path | None) -> tuple[str, str]:
    args = "export"
    if data_dir is not None:
        args += f" --dir {data_dir}"
    if db is not None:
        args += f" --db {db}"
    service = f"""[Unit]
Description=Export completed days of mouse recordings as day files

[Service]
Type=oneshot
ExecStart={exec_path} {args}
Nice=10
"""
    timer = f"""[Unit]
Description=Daily export of mouse recordings

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=10min

[Install]
WantedBy=timers.target
"""
    return service, timer


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)


def render_fountain_unit(exec_path: str, port: int, legacy_dir: Path | None, data_dir: Path | None, db: Path | None) -> str:
    args = f"fountain serve --port {port}"
    if legacy_dir is not None:
        args += f" --legacy-dir {legacy_dir}"
    if data_dir is not None:
        args += f" --data-dir {data_dir}"
    if db is not None:
        args += f" --db {db}"
    return f"""[Unit]
Description=Mouse path data fountain (HTTP on 127.0.0.1:{port})
After=mouse-logger.service

[Service]
Type=simple
ExecStart={exec_path} {args}
Restart=on-failure
RestartSec=5
Nice=10

[Install]
WantedBy=default.target
"""


def install(hz: int, db: Path | None, data_dir: Path | None, live: bool = True, fountain: bool = False,
            port: int = 7777, legacy_dir: Path | None = None) -> None:
    d = unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    exe = executable()
    unit_path().write_text(render_unit(exe, hz, db, live))
    service, timer = render_export_units(exe, data_dir, db)
    (d / EXPORT_SERVICE).write_text(service)
    (d / EXPORT_TIMER).write_text(timer)
    if fountain:
        (d / FOUNTAIN_SERVICE).write_text(render_fountain_unit(exe, port, legacy_dir, data_dir, db))
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", UNIT_NAME)
    _systemctl("enable", "--now", EXPORT_TIMER)
    if fountain:
        _systemctl("enable", "--now", FOUNTAIN_SERVICE)
    print(f"installed {unit_path()} (recorder) and {d / EXPORT_TIMER} (daily export)")
    if fountain:
        print(f"installed {d / FOUNTAIN_SERVICE} (data fountain on http://127.0.0.1:{port})")
    print(f"ExecStart: {exe}")
    print(f"status:  systemctl --user status {UNIT_NAME}")
    print(f"logs:    journalctl --user -u {UNIT_NAME} -f")
    print(f"timer:   systemctl --user list-timers {EXPORT_TIMER}")
    if fountain:
        print(f"fountain: journalctl --user -u {FOUNTAIN_SERVICE} -f   and   curl localhost:{port}/health")


def uninstall() -> None:
    subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME, EXPORT_TIMER], check=False)
    if (unit_dir() / FOUNTAIN_SERVICE).exists():
        subprocess.run(["systemctl", "--user", "disable", "--now", FOUNTAIN_SERVICE], check=False)
    for name in (UNIT_NAME, EXPORT_SERVICE, EXPORT_TIMER, FOUNTAIN_SERVICE):
        path = unit_dir() / name
        if path.exists():
            path.unlink()
            print(f"removed {path}")
    _systemctl("daemon-reload")
