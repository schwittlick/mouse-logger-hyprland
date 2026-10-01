"""Wires the sources (Hyprland poller, Hyprland events, evdev) to the DB writer."""

from __future__ import annotations

import json
import logging
import signal
import socket
import threading
import time
from pathlib import Path

from . import __version__
from .db import Writer
from .hypr import Hypr, HyprlandNotRunning

log = logging.getLogger(__name__)


def _now() -> tuple[int, int]:
    return time.time_ns(), time.monotonic_ns()


def wait_for_hyprland(stop: threading.Event) -> Hypr | None:
    """Block until the Hyprland IPC socket answers, or stop is set."""
    last_log = 0.0
    while not stop.is_set():
        try:
            h = Hypr()
            h.cursorpos()
            return h
        except (HyprlandNotRunning, OSError, ValueError) as e:
            if time.monotonic() - last_log > 30:
                log.warning("waiting for Hyprland IPC: %s", e)
                last_log = time.monotonic()
            stop.wait(1.0)
    return None


class Poller(threading.Thread):
    """Samples the cursor position at a fixed rate, emits a row on change."""

    def __init__(self, hypr: Hypr, hz: int, writer: Writer, stop: threading.Event):
        super().__init__(name="poller", daemon=True)
        self.hypr = hypr
        self.period = 1.0 / hz
        self.writer = writer
        self.stop_event = stop
        self.samples = 0
        self.changes = 0
        self.errors = 0

    def run(self) -> None:
        next_t = time.monotonic()
        last = None
        while not self.stop_event.is_set():
            try:
                x, y = self.hypr.cursorpos()
            except (OSError, ValueError) as e:
                self.errors += 1
                if self.errors in (1, 10, 100) or self.errors % 1000 == 0:
                    log.warning("cursorpos failed (%d): %s", self.errors, e)
                self.stop_event.wait(0.5)
                next_t = time.monotonic()
                continue
            self.samples += 1
            if (x, y) != last:
                last = (x, y)
                self.changes += 1
                t_ns, mono_ns = _now()
                self.writer.put("motion", (t_ns, mono_ns, x, y))
            next_t += self.period
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:  # fell behind (e.g. Hyprland stalled); resync instead of bursting
                next_t = time.monotonic()


class FocusTracker(threading.Thread):
    """Listens to Hyprland's event socket and logs focused-window changes."""

    RELEVANT = {"activewindow", "activewindowv2", "windowtitle", "windowtitlev2", "closewindow"}

    def __init__(self, hypr: Hypr, writer: Writer, stop: threading.Event):
        super().__init__(name="focus", daemon=True)
        self.hypr = hypr
        self.writer = writer
        self.stop_event = stop
        self.last: tuple | None = None

    def _refresh(self) -> None:
        try:
            win = self.hypr.active_window()
        except (OSError, ValueError) as e:
            log.warning("activewindow failed: %s", e)
            return
        if win is None:
            state = (None, None, None)
        else:
            addr = str(win.get("address", "")).removeprefix("0x") or None
            state = (addr, win.get("class") or None, win.get("title") or None)
        if state != self.last:
            self.last = state
            t_ns, mono_ns = _now()
            self.writer.put("focus", (t_ns, mono_ns, *state))

    def run(self) -> None:
        self._refresh()
        while not self.stop_event.is_set():
            try:
                for name, _payload in self.hypr.events():
                    if self.stop_event.is_set():
                        return
                    if name in self.RELEVANT:
                        self._refresh()
                log.warning("event socket closed")
            except OSError as e:
                log.warning("event socket error: %s", e)
            self.stop_event.wait(2.0)
            self._refresh()


def run(db_path: Path, hz: int, use_evdev: bool = True) -> int:
    stop = threading.Event()

    def on_signal(signum, _frame):
        log.info("received %s, stopping", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    hypr = wait_for_hyprland(stop)
    if hypr is None:
        return 0

    writer = Writer(db_path)
    try:
        monitors = json.dumps(hypr.monitors(), separators=(",", ":"))
        hv = hypr.version()
    except (OSError, ValueError) as e:
        log.warning("could not read monitors/version: %s", e)
        monitors, hv = None, None
    sid = writer.open_session(
        started_ns=time.time_ns(),
        hostname=socket.gethostname(),
        hyprland_version=hv,
        monitors=monitors,
        poll_hz=hz,
        logger_version=__version__,
    )
    log.info("session %d started, db=%s, %d Hz, evdev=%s", sid, db_path, hz, use_evdev)
    writer.start()

    threads = [Poller(hypr, hz, writer, stop), FocusTracker(hypr, writer, stop)]
    if use_evdev:
        from .inputdev import InputManager

        threads.append(InputManager(
            on_button=lambda row: writer.put("buttons", row),
            on_scroll=lambda row: writer.put("scroll", row),
            stop=stop,
        ))
    for t in threads:
        t.start()

    # Idle here until a signal arrives. signal.pause would also do, but wait()
    # keeps behaviour identical when the stop is set from another thread.
    while not stop.is_set():
        stop.wait(60)
        if not stop.is_set() and not threads[0].is_alive():
            log.error("poller thread died, exiting so systemd restarts us")
            stop.set()
            writer.stop(time.time_ns())
            return 1

    writer.stop(time.time_ns())
    p: Poller = threads[0]
    log.info("stopped: %d samples, %d position changes, %d errors", p.samples, p.changes, p.errors)
    return 0
