"""Live event publishing from the recorder to the fountain.

The recorder sends every event it writes to the database also to a unix
datagram socket, fire and forget: if nobody listens the sendto fails and is
counted, and the recorder is never slowed down or stopped by it.
Datagram payload: msgpack of (table, *row), the row exactly as Writer.put gets it.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from pathlib import Path

import msgpack

log = logging.getLogger(__name__)


def default_live_sock() -> Path:
    env = os.environ.get("MOUSE_LOGGER_LIVE_SOCK")
    if env:
        return Path(env)
    run = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/mouse_logger-{os.getuid()}"
    return Path(run) / "mouse_logger" / "live.sock"


def encode(table: str, row: tuple) -> bytes:
    return msgpack.packb((table, *row), use_bin_type=True)


def decode(data: bytes) -> tuple[str, tuple]:
    table, *row = msgpack.unpackb(data, raw=False)
    return table, tuple(row)


class Publisher:
    """Sends events to the socket path; never raises."""

    def __init__(self, path: Path):
        self.path = str(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sent = 0
        self.dropped = 0
        self._last_log = 0.0

    def put(self, table: str, row: tuple) -> None:
        try:
            self.sock.sendto(encode(table, row), self.path)
            self.sent += 1
        except OSError:
            self.dropped += 1
            now = time.monotonic()
            if now - self._last_log > 600:
                self._last_log = now
                log.debug("live publish: %d sent, %d dropped (no listener?)", self.sent, self.dropped)

    def close(self) -> None:
        self.sock.close()


class Tee:
    """A sink that writes to the database and publishes; the publisher can never hurt the writer."""

    def __init__(self, writer, publisher: Publisher):
        self.writer = writer
        self.publisher = publisher

    def put(self, table: str, row: tuple) -> None:
        self.writer.put(table, row)
        try:
            self.publisher.put(table, row)
        except Exception:  # belt and braces: Publisher.put already swallows OSError
            pass


class Listener:
    """Binds the socket path and reads datagrams; used by the fountain."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        if path.exists():
            path.unlink()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(path))
        self.sock.setblocking(False)
        self.received = 0

    def fileno(self) -> int:
        return self.sock.fileno()

    def drain(self, limit: int = 4096) -> list[tuple[str, tuple]]:
        out = []
        for _ in range(limit):
            try:
                data = self.sock.recv(65536)
            except BlockingIOError:
                break
            except OSError:
                break
            try:
                out.append(decode(data))
                self.received += 1
            except Exception:
                continue
        return out

    def close(self) -> None:
        self.sock.close()
        try:
            self.path.unlink()
        except OSError:
            pass
