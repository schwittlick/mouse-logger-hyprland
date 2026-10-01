"""Button and scroll capture from /dev/input via evdev.

Only pointer devices are opened (devices reporting relative X/Y motion or a
left mouse button). Only BTN_* mouse button codes and REL_WHEEL* axes are
recorded; keyboard keys are ignored even if a device reports them.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

import evdev
from evdev import ecodes

log = logging.getLogger(__name__)

BTN_MIN, BTN_MAX = ecodes.BTN_MOUSE, ecodes.BTN_TASK  # 0x110 .. 0x117
SCROLL_AXES = {
    ecodes.REL_WHEEL: ("v", 0),
    ecodes.REL_HWHEEL: ("h", 0),
    getattr(ecodes, "REL_WHEEL_HI_RES", 0x0B): ("v", 1),
    getattr(ecodes, "REL_HWHEEL_HI_RES", 0x0C): ("h", 1),
}

PERMISSION_HINT = (
    "no permission to read /dev/input devices; buttons and scroll will not be "
    "recorded. Fix: sudo usermod -aG input %s   (then log out and back in)"
)

# row callbacks: (t_ns, mono_ns, device, code, name, pressed) / (t_ns, mono_ns, device, axis, value, hires)
ButtonCb = Callable[[tuple], None]
ScrollCb = Callable[[tuple], None]


def _is_pointer(dev: evdev.InputDevice) -> bool:
    caps = dev.capabilities()
    rel = caps.get(ecodes.EV_REL, [])
    keys = caps.get(ecodes.EV_KEY, [])
    return ecodes.REL_X in rel or ecodes.BTN_LEFT in keys


def _btn_name(code: int) -> str:
    name = ecodes.BTN.get(code, f"BTN_{code}")
    if isinstance(name, (list, tuple)):
        name = name[0]
    return name


class DeviceReader(threading.Thread):
    def __init__(self, dev: evdev.InputDevice, on_button: ButtonCb, on_scroll: ScrollCb, stop: threading.Event):
        super().__init__(name=f"evdev-{os.path.basename(dev.path)}", daemon=True)
        self.dev = dev
        self.on_button = on_button
        self.on_scroll = on_scroll
        self.stop_event = stop

    def run(self) -> None:
        name = self.dev.name
        log.info("reading %s (%s)", self.dev.path, name)
        try:
            for ev in self.dev.read_loop():
                if self.stop_event.is_set():
                    break
                # Kernel timestamps are CLOCK_REALTIME; derive a monotonic value
                # using the current offset between the two clocks.
                t_ns = ev.sec * 1_000_000_000 + ev.usec * 1_000
                mono_ns = t_ns - (time.time_ns() - time.monotonic_ns())
                if ev.type == ecodes.EV_KEY:
                    if BTN_MIN <= ev.code <= BTN_MAX and ev.value in (0, 1):
                        self.on_button((t_ns, mono_ns, name, ev.code, _btn_name(ev.code), ev.value))
                elif ev.type == ecodes.EV_REL:
                    axis = SCROLL_AXES.get(ev.code)
                    if axis is not None:
                        self.on_scroll((t_ns, mono_ns, name, axis[0], ev.value, axis[1]))
        except OSError as e:
            log.info("%s (%s) gone: %s", self.dev.path, name, e)
        finally:
            try:
                self.dev.close()
            except Exception:
                pass


class InputManager(threading.Thread):
    """Discovers pointer devices, starts a reader per device, handles hotplug by rescanning."""

    def __init__(self, on_button: ButtonCb, on_scroll: ScrollCb, stop: threading.Event, rescan_interval: float = 5.0):
        super().__init__(name="evdev-manager", daemon=True)
        self.on_button = on_button
        self.on_scroll = on_scroll
        self.stop_event = stop
        self.rescan_interval = rescan_interval
        self.readers: dict[str, DeviceReader] = {}
        self._warned_permission = False

    def _scan(self) -> None:
        # drop finished readers (device unplugged)
        for path, r in list(self.readers.items()):
            if not r.is_alive():
                del self.readers[path]
        denied = False
        for path in evdev.list_devices():
            if path in self.readers:
                continue
            try:
                dev = evdev.InputDevice(path)
            except PermissionError:
                denied = True
                continue
            except OSError as e:
                log.debug("skip %s: %s", path, e)
                continue
            if not _is_pointer(dev):
                dev.close()
                continue
            r = DeviceReader(dev, self.on_button, self.on_scroll, self.stop_event)
            r.start()
            self.readers[path] = r
        if denied and not self._warned_permission:
            self._warned_permission = True
            log.warning(PERMISSION_HINT, os.environ.get("USER", "$USER"))
        elif not denied and self._warned_permission:
            self._warned_permission = False
            log.info("/dev/input is readable now")

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._scan()
            except Exception:
                log.exception("device scan failed")
            self.stop_event.wait(self.rescan_interval)
