"""Run the fountain: build what is stale, index everything, tail the live db, serve HTTP."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import dayfiles
from . import cache
from .ingest_days import LiveTail
from .store import Index

log = logging.getLogger("fountain")


@dataclass
class Options:
    host: str = "127.0.0.1"
    port: int = 7777
    legacy_dir: Path | None = None
    data_dir: Path | None = None
    db: Path | None = None
    cache_dir: Path = field(default_factory=cache.default_cache_dir)
    live: bool = True
    live_sock: Path | None = None
    jobs: int | None = None
    rescan_s: float = 60.0
    tail_s: float = 1.0


class State:
    """What the endpoints read. index is swapped atomically by the builder thread."""

    def __init__(self, opts: Options):
        self.opts = opts
        self.index: Index | None = None
        self.base: Index | None = None
        self.tail: LiveTail | None = None
        self.hub = None
        self.progress: dict = {"phase": "starting"}
        self.started = time.time()
        self.startup_ms: float | None = None
        self._rescan = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def request_rescan(self) -> None:
        self._rescan.set()

    def live_status(self) -> dict:
        d = {"socket": str(self.opts.live_sock) if self.opts.live else None}
        if self.tail is not None:
            d["tail"] = self.tail.status()
        if self.hub is not None:
            d.update(self.hub.status())
        return d

    # ---- building
    def inputs(self) -> list[cache.Input]:
        return cache.find_inputs(self.opts.legacy_dir, self.opts.data_dir, self.opts.cache_dir)

    def build_and_index(self) -> None:
        """Build stale chunks, load every chunk, build the base Index, start the live tail."""
        t = time.perf_counter()
        inputs = self.inputs()
        stale = [i for i in inputs if not cache.is_current(i)]
        self.progress = {"phase": "building", "stale": len(stale), "inputs": len(inputs)}
        if stale:
            log.info("building %d of %d chunks", len(stale), len(inputs))
            _built, failed, _ = cache.build(inputs, self.opts.cache_dir, self.opts.jobs, log=log.info)
            if failed:
                log.warning("%d inputs could not be built: %s", len(failed), ", ".join(i.key for i in failed[:5]))
        self.progress = {"phase": "indexing", "inputs": len(inputs)}
        chunks = []
        for i in inputs:
            if i.out.exists():
                try:
                    chunks.append(cache.read_chunk(i.out))
                except Exception as e:
                    log.warning("cannot read %s: %s", i.out, e)
        base = Index.build(chunks)
        tail = None
        if self.opts.db is not None:
            tail = LiveTail(self.opts.db, self.covered_until(chunks), dayfiles.machine_name())
            try:
                tail.refresh()
            except Exception as e:
                log.warning("live tail: %s", e)
        with self._lock:
            self.base, self.tail = base, tail
            self.index = Index.build(tail.chunks(), base=base) if tail else base
        self.progress = {"phase": "ready"}
        if self.startup_ms is None:
            self.startup_ms = round((time.perf_counter() - t) * 1000, 1)
        log.info("ready: %s paths in %d chunks, %.1f s", f"{len(self.index):,}", len(chunks), time.perf_counter() - t)

    @staticmethod
    def covered_until(chunks) -> int:
        me = dayfiles.machine_name()
        ends = [int(c.meta.get("end_ns") or 0) for c in chunks
                if c.meta.get("kind") == "day" and c.meta.get("complete") and c.meta.get("machine") == me]
        return max(ends, default=0)

    def tail_once(self) -> None:
        tail = self.tail
        if tail is None:
            return
        try:
            if tail.refresh():
                with self._lock:
                    self.index = Index.build(tail.chunks(), base=self.base)
        except Exception as e:
            log.warning("live tail: %s", e)

    def loop(self) -> None:
        """Builder thread: initial build, then tail every second and rescan every minute."""
        try:
            self.build_and_index()
        except Exception:
            log.exception("initial build failed")
            self.progress = {"phase": "failed"}
        last_scan = time.monotonic()
        while not self._stop.is_set():
            self._rescan.wait(self.opts.tail_s)
            now = time.monotonic()
            if self._rescan.is_set() or now - last_scan >= self.opts.rescan_s:
                self._rescan.clear()
                last_scan = now
                try:
                    if any(not cache.is_current(i) for i in self.inputs()):
                        log.info("inputs changed, rebuilding")
                        self.build_and_index()
                except Exception:
                    log.exception("rescan failed")
            self.tail_once()

    def stop(self) -> None:
        self._stop.set()


def serve(opts: Options) -> int:
    import uvicorn

    from .api import create_app

    state = State(opts)
    if opts.live:
        try:
            from .livehub import Hub

            state.hub = Hub(opts.live_sock, state)
        except Exception as e:
            log.warning("live stream disabled: %s", e)
            state.hub = None
    app = create_app(state)
    worker = threading.Thread(target=state.loop, name="fountain-builder", daemon=True)
    worker.start()
    log.info("fountain listening on http://%s:%d (docs at /docs)", opts.host, opts.port)
    try:
        uvicorn.run(app, host=opts.host, port=opts.port, workers=1, log_level="warning", ws="auto")
    finally:
        state.stop()
    return 0
