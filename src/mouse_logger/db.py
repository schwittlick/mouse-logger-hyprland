"""SQLite storage with a background writer thread that batches inserts."""

from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS sessions (
    id               INTEGER PRIMARY KEY,
    started_ns       INTEGER NOT NULL,   -- unix wall clock, nanoseconds
    ended_ns         INTEGER,            -- NULL if the logger did not exit cleanly
    hostname         TEXT,
    hyprland_version TEXT,
    monitors         TEXT,               -- JSON from `hyprctl -j monitors` at start
    poll_hz          INTEGER,
    logger_version   TEXT
);

-- Cursor position samples. A row is written only when the position changed
-- since the previous sample. x/y are Hyprland logical (scaled) global coords.
CREATE TABLE IF NOT EXISTS motion (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    t_ns       INTEGER NOT NULL,   -- unix wall clock, nanoseconds
    mono_ns    INTEGER NOT NULL,   -- CLOCK_MONOTONIC, nanoseconds (for replay timing)
    x          INTEGER NOT NULL,
    y          INTEGER NOT NULL
);

-- Mouse button press/release from evdev. Keyboard keys are never recorded.
CREATE TABLE IF NOT EXISTS buttons (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    t_ns       INTEGER NOT NULL,
    mono_ns    INTEGER NOT NULL,
    device     TEXT,               -- evdev device name
    code       INTEGER NOT NULL,   -- linux input code, e.g. 272 = BTN_LEFT
    name       TEXT,               -- BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, ...
    pressed    INTEGER NOT NULL    -- 1 press, 0 release
);

-- Scroll wheel events from evdev. axis 'v' or 'h'. For hires=0 value is in
-- notches (+1 = up/left), for hires=1 value is in 1/120 notch units.
CREATE TABLE IF NOT EXISTS scroll (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    t_ns       INTEGER NOT NULL,
    mono_ns    INTEGER NOT NULL,
    device     TEXT,
    axis       TEXT NOT NULL,
    value      INTEGER NOT NULL,
    hires      INTEGER NOT NULL
);

-- Focus change log: from t_ns onward, the focused window was (app_id, title).
-- A row with NULL address means nothing was focused (empty workspace).
CREATE TABLE IF NOT EXISTS focus (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    t_ns       INTEGER NOT NULL,
    mono_ns    INTEGER NOT NULL,
    address    TEXT,
    app_id     TEXT,
    title      TEXT
);

CREATE INDEX IF NOT EXISTS motion_t  ON motion(t_ns);
CREATE INDEX IF NOT EXISTS buttons_t ON buttons(t_ns);
CREATE INDEX IF NOT EXISTS scroll_t  ON scroll(t_ns);
CREATE INDEX IF NOT EXISTS focus_t   ON focus(t_ns);
"""

INSERTS = {
    "motion": "INSERT INTO motion(session_id,t_ns,mono_ns,x,y) VALUES(?,?,?,?,?)",
    "buttons": "INSERT INTO buttons(session_id,t_ns,mono_ns,device,code,name,pressed) VALUES(?,?,?,?,?,?,?)",
    "scroll": "INSERT INTO scroll(session_id,t_ns,mono_ns,device,axis,value,hires) VALUES(?,?,?,?,?,?,?)",
    "focus": "INSERT INTO focus(session_id,t_ns,mono_ns,address,app_id,title) VALUES(?,?,?,?,?,?)",
}


def default_db_path() -> Path:
    env = os.environ.get("MOUSE_LOGGER_DB")
    if env:
        return Path(env).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(data_home) / "mouse_logger" / "mouse.db"


class Writer(threading.Thread):
    """Owns the sqlite connection. Other threads call .put(table, row)."""

    def __init__(self, path: Path, flush_interval: float = 1.0, batch_size: int = 1000):
        super().__init__(name="db-writer", daemon=True)
        self.path = path
        self.flush_interval = flush_interval
        self.batch_size = batch_size
        self.q: queue.Queue[tuple[str, tuple] | None] = queue.Queue()
        self._stopping = threading.Event()
        self.session_id: int | None = None
        self.rows_written = 0

        path.parent.mkdir(parents=True, exist_ok=True)
        # The connection is created here (main thread) only for setup; the
        # writer thread re-opens its own connection in run().
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA)

    # -- called from the main thread before start() ---------------------------
    def open_session(self, **fields) -> int:
        cols = ",".join(fields)
        marks = ",".join("?" for _ in fields)
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(f"INSERT INTO sessions({cols}) VALUES({marks})", tuple(fields.values()))
            self.session_id = cur.lastrowid
        return self.session_id

    # -- called from any thread ------------------------------------------------
    def put(self, table: str, row: tuple) -> None:
        self.q.put((table, row))

    def stop(self, ended_ns: int) -> None:
        self._stopping.set()
        self.q.put(None)
        self.join(timeout=10)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE sessions SET ended_ns=? WHERE id=?", (ended_ns, self.session_id))

    # -- thread body -----------------------------------------------------------
    def run(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA synchronous = NORMAL")
        pending: dict[str, list[tuple]] = {t: [] for t in INSERTS}
        n_pending = 0
        last_flush = time.monotonic()

        def flush():
            nonlocal n_pending, last_flush
            if n_pending:
                with conn:
                    for table, rows in pending.items():
                        if rows:
                            conn.executemany(INSERTS[table], rows)
                            self.rows_written += len(rows)
                            rows.clear()
                n_pending = 0
            last_flush = time.monotonic()

        while True:
            timeout = max(0.0, self.flush_interval - (time.monotonic() - last_flush))
            try:
                item = self.q.get(timeout=timeout)
            except queue.Empty:
                flush()
                continue
            if item is None:
                flush()
                break
            table, row = item
            pending[table].append((self.session_id, *row))
            n_pending += 1
            if n_pending >= self.batch_size or time.monotonic() - last_flush >= self.flush_interval:
                flush()
        conn.close()
        log.info("writer stopped, %d rows written this session", self.rows_written)
