"""Fan the recorder's live events out to WebSocket clients.

The recorder publishes to a unix datagram socket (live.py). The hub reads it
on the server's event loop, remembers the latest cursor position, and sends
every event to each connected WebSocket client. Motion can be thinned per
client with ?hz=N (the newest sample per tick); buttons, scroll and focus are
always forwarded.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import msgpack
import orjson
from fastapi import WebSocket, WebSocketDisconnect

from ..live import Listener
from ..query import NS

EVENTS = ("motion", "button", "scroll", "focus")


class Client:
    def __init__(self, hz: float, events: set[str], fmt: str):
        self.hz, self.events, self.fmt = hz, events, fmt
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.pending_motion: dict | None = None
        self.sent = 0
        self.dropped = 0

    def offer(self, ev: dict) -> None:
        if ev["e"] not in self.events:
            return
        if ev["e"] == "motion" and self.hz > 0:
            self.pending_motion = ev  # the newest sample wins
            return
        try:
            self.queue.put_nowait(ev)
        except asyncio.QueueFull:
            self.dropped += 1

    def encode(self, ev: dict) -> bytes:
        return msgpack.packb(ev, use_bin_type=True) if self.fmt == "msgpack" else orjson.dumps(ev)


class Hub:
    def __init__(self, sock_path: Path, state):
        self.listener = Listener(sock_path)
        self.state = state
        self.clients: set[Client] = set()
        self.last: dict | None = None
        self.app: str | None = None
        self.events = 0
        self.last_event_ns = 0
        self._rect = None
        self._rect_checked = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- event loop side
    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(self.listener.fileno(), self._readable)

    def stop(self) -> None:
        if self._loop is not None:
            try:
                self._loop.remove_reader(self.listener.fileno())
            except Exception:
                pass
        self.listener.close()

    def _readable(self) -> None:
        for table, row in self.listener.drain():
            self._event(table, row)

    def _rect_now(self):
        now = time.monotonic()
        if now - self._rect_checked > 5.0:
            self._rect_checked = now
            tail = self.state.tail
            if tail is not None and tail.rects:
                self._rect = tail.rects[max(tail.rects)]
        return self._rect

    def _event(self, table: str, row: tuple) -> None:
        self.events += 1
        self.last_event_ns = time.time_ns()
        try:
            if table == "motion":
                t_ns, _mono, x, y = row
                ev = {"e": "motion", "t": int(t_ns), "px": int(x), "py": int(y)}
                rect = self._rect_now()
                if rect is not None:
                    rx, ry, rw, rh = rect
                    ev["x"] = (x - rx) / rw
                    ev["y"] = (y - ry) / rh
                self.last = ev
            elif table == "buttons":
                t_ns, _mono, _device, code, name, pressed = row
                ev = {"e": "button", "t": int(t_ns), "code": int(code), "name": name, "pressed": int(pressed)}
            elif table == "scroll":
                t_ns, _mono, _device, axis, value, hires = row
                ev = {"e": "scroll", "t": int(t_ns), "axis": axis, "value": int(value), "hires": int(hires)}
            elif table == "focus":
                t_ns, _mono, _address, app_id, title = row
                ev = {"e": "focus", "t": int(t_ns), "app": app_id, "title": title}
                self.app = app_id
            else:
                return
        except (ValueError, TypeError):
            return
        for c in self.clients:
            c.offer(ev)

    # ---- queries
    def latest(self) -> dict | None:
        if self.last is None:
            return None
        rect = self._rect_now()
        d = dict(self.last)
        d.pop("e", None)
        d["t_ns"] = d.pop("t")
        d["age_ms"] = round((time.time_ns() - d["t_ns"]) / 1e6, 1)
        d["app"] = self.app
        if rect is not None:
            d["screen_w"], d["screen_h"] = int(rect[2]), int(rect[3])
            if "x" not in d:  # the sample arrived before the session's monitors were known
                d["x"], d["y"] = (d["px"] - rect[0]) / rect[2], (d["py"] - rect[1]) / rect[3]
        return d

    def status(self) -> dict:
        return {"connected_clients": len(self.clients), "events": self.events,
                "last_event_age_ms": round((time.time_ns() - self.last_event_ns) / 1e6, 1) if self.last_event_ns else None}

    # ---- websocket
    async def serve(self, ws: WebSocket) -> None:
        qp = ws.query_params
        try:
            hz = float(qp.get("hz", 60))
        except ValueError:
            hz = 60.0
        events = {e for e in (qp.get("events") or ",".join(EVENTS)).split(",") if e in EVENTS} or set(EVENTS)
        fmt = "json" if qp.get("format") == "json" else "msgpack"
        await ws.accept()
        client = Client(hz, events, fmt)
        self.clients.add(client)
        receiver = asyncio.ensure_future(ws.receive())  # notices the client going away
        try:
            while True:
                tick = 1.0 / hz if hz > 0 else 1.0
                getter = asyncio.ensure_future(client.queue.get())
                done, _ = await asyncio.wait({getter, receiver}, timeout=tick, return_when=asyncio.FIRST_COMPLETED)
                if receiver in done:
                    getter.cancel()
                    msg = receiver.result()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    receiver = asyncio.ensure_future(ws.receive())
                if getter in done:
                    ev = getter.result()
                    await self._send(ws, client, ev)
                else:
                    getter.cancel()
                if client.pending_motion is not None:
                    ev, client.pending_motion = client.pending_motion, None
                    await self._send(ws, client, ev)
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass
        finally:
            receiver.cancel()
            self.clients.discard(client)

    async def _send(self, ws: WebSocket, client: Client, ev: dict) -> None:
        if client.fmt == "msgpack":
            await ws.send_bytes(client.encode(ev))
        else:
            await ws.send_text(client.encode(ev).decode())
        client.sent += 1
