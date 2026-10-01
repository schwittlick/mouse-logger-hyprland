"""Minimal Hyprland IPC client (request socket + event socket)."""

from __future__ import annotations

import glob
import json
import logging
import os
import socket
from typing import Iterator

log = logging.getLogger(__name__)


class HyprlandNotRunning(RuntimeError):
    pass


def _runtime_dir() -> str:
    return os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def instance_dir() -> str:
    """Directory holding .socket.sock / .socket2.sock for the running instance.

    Uses HYPRLAND_INSTANCE_SIGNATURE when set, otherwise the most recently
    created instance directory that still has a live request socket.
    """
    base = os.path.join(_runtime_dir(), "hypr")
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if sig:
        d = os.path.join(base, sig)
        if os.path.exists(os.path.join(d, ".socket.sock")):
            return d
        log.warning("HYPRLAND_INSTANCE_SIGNATURE set but socket missing, scanning %s", base)
    candidates = [
        d for d in glob.glob(os.path.join(base, "*"))
        if os.path.exists(os.path.join(d, ".socket.sock"))
    ]
    if not candidates:
        raise HyprlandNotRunning(f"no Hyprland instance found under {base}")
    return max(candidates, key=os.path.getmtime)


class Hypr:
    """Blocking IPC client. One instance per thread is fine; it holds no state."""

    def __init__(self, inst_dir: str | None = None, timeout: float = 2.0):
        self.inst_dir = inst_dir or instance_dir()
        self.request_sock = os.path.join(self.inst_dir, ".socket.sock")
        self.event_sock = os.path.join(self.inst_dir, ".socket2.sock")
        self.timeout = timeout

    def request(self, cmd: str) -> str:
        """Send one hyprctl-style command and return the reply text."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            s.connect(self.request_sock)
            s.sendall(cmd.encode())
            chunks = []
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks).decode(errors="replace")

    def request_json(self, cmd: str):
        return json.loads(self.request("j/" + cmd))

    def cursorpos(self) -> tuple[int, int]:
        """Cursor position in logical (scaled) global coordinates."""
        x, y = self.request("cursorpos").split(",")
        return int(x), int(y)

    def version(self) -> str:
        lines = self.request("version").splitlines()
        return lines[0].strip() if lines else ""

    def monitors(self) -> list:
        return self.request_json("monitors")

    def active_window(self) -> dict | None:
        """The focused window, or None when nothing is focused."""
        data = self.request_json("activewindow")
        if not data or "address" not in data:
            return None
        return data

    def events(self) -> Iterator[tuple[str, str]]:
        """Yield (event_name, payload) from the event socket until it closes."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(self.event_sock)
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode(errors="replace")
                    name, sep, payload = text.partition(">>")
                    if sep:
                        yield name, payload
