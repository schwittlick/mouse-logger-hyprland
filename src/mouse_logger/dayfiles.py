"""Move recordings between machines as per-day SQLite files.

export: live db -> <dir>/<machine>/<YYYY-MM-DD>.sqlite for every completed
        local day. Files never change once written, which suits any sync tool.
        Each file also carries the focus state in force when its day began, as
        a focus row stamped start_ns, so it can be read on its own.
        --today additionally writes <day>.partial.sqlite, replaced each run and
        superseded by the complete file once the day is over.
import: every file of every machine under <dir> -> one archive db. Session
        ids are remapped per machine, files already imported are skipped, and
        a partial day is replaced when a newer partial or the complete file
        appears.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import __version__
from .db import SCHEMA

log = logging.getLogger(__name__)
NS = 1_000_000_000

EVENT_COLUMNS = {
    "motion": "t_ns,mono_ns,x,y",
    "buttons": "t_ns,mono_ns,device,code,name,pressed",
    "scroll": "t_ns,mono_ns,device,axis,value,hires",
    "focus": "t_ns,mono_ns,address,app_id,title",
}
SESSION_COLUMNS = "started_ns,ended_ns,hostname,hyprland_version,monitors,poll_hz,logger_version"

# the live schema without its WAL/synchronous pragmas: day files and the archive use a plain journal
PLAIN_SCHEMA = "\n".join(line for line in SCHEMA.splitlines() if not line.startswith("PRAGMA"))

DAYFILE_META = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"

ARCHIVE_EXTRA = """
CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY,
    machine     TEXT NOT NULL,
    day         TEXT NOT NULL,
    file        TEXT NOT NULL,
    complete    INTEGER NOT NULL,
    start_ns    INTEGER NOT NULL,
    end_ns      INTEGER NOT NULL,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    rows        INTEGER NOT NULL,
    imported_ns INTEGER NOT NULL,
    UNIQUE(machine, day)
);
-- which archive session a (machine, original session id) pair became
CREATE TABLE IF NOT EXISTS session_map (
    machine    TEXT NOT NULL,
    source_id  INTEGER NOT NULL,
    session_id INTEGER NOT NULL,
    PRIMARY KEY(machine, source_id)
);
"""


def default_data_dir() -> Path:
    return Path(os.environ.get("MOUSE_LOGGER_DATA") or "~/mouse-data").expanduser()


def default_archive_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(data_home) / "mouse_logger" / "archive.db"


def machine_name() -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", socket.gethostname()) or "unknown"


def day_bounds(d: date) -> tuple[int, int]:
    """[start, end) of a local calendar day in unix ns."""
    start = datetime(d.year, d.month, d.day).astimezone()
    end = (datetime(d.year, d.month, d.day) + timedelta(days=1)).astimezone()
    return int(start.timestamp() * NS), int(end.timestamp() * NS)


def _connect(path: Path) -> sqlite3.Connection:
    # uri=True so that ATTACH accepts file: URIs with ?mode=ro
    return sqlite3.connect(f"file:{path}", uri=True)


# ------------------------------------------------------------------- export
def _recorded_span(db: Path) -> tuple[int, int] | None:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        lo, hi = conn.execute(
            "SELECT MIN(t), MAX(t) FROM ("
            "  SELECT MIN(t_ns) t FROM motion UNION ALL SELECT MAX(t_ns) FROM motion"
            "  UNION ALL SELECT MIN(t_ns) FROM buttons UNION ALL SELECT MAX(t_ns) FROM buttons"
            "  UNION ALL SELECT MIN(t_ns) FROM focus UNION ALL SELECT MAX(t_ns) FROM focus)"
        ).fetchone()
    finally:
        conn.close()
    return None if lo is None else (int(lo), int(hi))


def export_day(db: Path, path: Path, machine: str, d: date, complete: bool) -> int:
    """Write one day file. Returns the number of event rows, 0 if nothing to write."""
    start, end = day_bounds(d)
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    conn = _connect(tmp)
    try:
        conn.executescript(PLAIN_SCHEMA + DAYFILE_META)
        conn.execute("ATTACH DATABASE ? AS src", (f"file:{db}?mode=ro",))
        n = 0
        with conn:
            # the focus state in force when the day began, stamped start_ns so it lies inside the
            # day's range: query.load_focus() then labels the first strokes of the day without the
            # previous file, and a re-import still replaces it with the day. Inserted first so a
            # real focus change at exactly start_ns sorts after it.
            conn.execute(
                "INSERT INTO focus(session_id,t_ns,mono_ns,address,app_id,title) "
                "SELECT session_id,?,mono_ns,address,app_id,title FROM src.focus "
                "WHERE t_ns < ? ORDER BY t_ns DESC LIMIT 1", (start, start))
            for table, cols in EVENT_COLUMNS.items():
                cur = conn.execute(
                    f"INSERT INTO {table}(session_id,{cols}) SELECT session_id,{cols} FROM src.{table} "
                    "WHERE t_ns >= ? AND t_ns < ? ORDER BY t_ns", (start, end))
                n += cur.rowcount
            conn.execute(
                f"INSERT INTO sessions(id,{SESSION_COLUMNS}) SELECT id,{SESSION_COLUMNS} FROM src.sessions "
                "WHERE id IN (SELECT session_id FROM motion UNION SELECT session_id FROM buttons "
                "UNION SELECT session_id FROM scroll UNION SELECT session_id FROM focus)")
            conn.executemany("INSERT INTO meta VALUES(?,?)", [
                ("machine", machine), ("day", d.isoformat()), ("start_ns", str(start)), ("end_ns", str(end)),
                ("complete", "1" if complete else "0"), ("rows", str(n)),
                ("exported_ns", str(time.time_ns())), ("logger_version", __version__),
            ])
        conn.execute("DETACH DATABASE src")
    finally:
        conn.close()
    if n == 0:
        tmp.unlink()
        return 0
    os.replace(tmp, path)
    return n


def export_days(db: Path, out_dir: Path, include_today: bool = False, force: bool = False) -> list[tuple[Path, int]]:
    """Export every completed day that has no file yet. Returns [(path, rows)] written."""
    if not db.exists():
        raise FileNotFoundError(f"no database at {db}")
    span = _recorded_span(db)
    if span is None:
        return []
    machine = machine_name()
    dest = out_dir / machine
    dest.mkdir(parents=True, exist_ok=True)
    first = datetime.fromtimestamp(span[0] / NS).date()
    last = datetime.fromtimestamp(span[1] / NS).date()
    today = date.today()
    written = []
    d = first
    while d <= last:
        complete = d < today
        full, partial = dest / f"{d}.sqlite", dest / f"{d}.partial.sqlite"
        if complete:
            if force or not full.exists():
                n = export_day(db, full, machine, d, True)
                if n:
                    written.append((full, n))
            if full.exists() and partial.exists():
                partial.unlink()  # superseded
        elif include_today:
            n = export_day(db, partial, machine, d, False)
            if n:
                written.append((partial, n))
        d += timedelta(days=1)
    return written


# ------------------------------------------------------------------- import
def _read_meta(path: Path) -> dict | None:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return dict(conn.execute("SELECT key, value FROM meta").fetchall())
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.warning("skipping %s: %s", path, e)
        return None


def _delete_import(conn: sqlite3.Connection, machine: str, start: int, end: int) -> None:
    for table in EVENT_COLUMNS:
        conn.execute(
            f"DELETE FROM {table} WHERE t_ns >= ? AND t_ns < ? AND session_id IN "
            "(SELECT session_id FROM session_map WHERE machine = ?)", (start, end, machine))


def _import_file(conn: sqlite3.Connection, machine: str) -> int:
    """Copy the attached day file 'd' into the archive. Runs inside the caller's transaction."""
    conn.execute("CREATE TEMP TABLE map (src INTEGER PRIMARY KEY, dst INTEGER NOT NULL)")
    try:
        for row in conn.execute(f"SELECT id,{SESSION_COLUMNS} FROM d.sessions").fetchall():
            src_id, fields = row[0], row[1:]
            hit = conn.execute("SELECT session_id FROM session_map WHERE machine=? AND source_id=?",
                               (machine, src_id)).fetchone()
            if hit:
                dst = hit[0]
                if fields[1] is not None:  # ended_ns became known after an earlier export
                    conn.execute("UPDATE sessions SET ended_ns = COALESCE(ended_ns, ?) WHERE id = ?", (fields[1], dst))
            else:
                marks = ",".join("?" for _ in fields)
                dst = conn.execute(f"INSERT INTO sessions({SESSION_COLUMNS}) VALUES({marks})", fields).lastrowid
                conn.execute("INSERT INTO session_map VALUES(?,?,?)", (machine, src_id, dst))
            conn.execute("INSERT INTO map VALUES(?,?)", (src_id, dst))
        n = 0
        for table, cols in EVENT_COLUMNS.items():
            cur = conn.execute(
                f"INSERT INTO {table}(session_id,{cols}) SELECT map.dst,{cols} FROM d.{table} e "
                "JOIN map ON map.src = e.session_id ORDER BY e.t_ns")
            n += cur.rowcount
    finally:
        conn.execute("DROP TABLE map")
    return n


def import_days(data_dir: Path, archive: Path) -> tuple[list[tuple[Path, int]], int]:
    """Merge all day files under data_dir into archive. Returns ([(path, rows)] imported, files skipped)."""
    if not data_dir.is_dir():
        raise FileNotFoundError(f"no data directory at {data_dir}")
    archive.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(archive)
    conn.executescript(PLAIN_SCHEMA + ARCHIVE_EXTRA)

    # one candidate per (machine, day); a complete file beats a partial one
    candidates: dict[tuple[str, str], tuple[Path, dict]] = {}
    for path in sorted(data_dir.glob("*/*.sqlite")):
        meta = _read_meta(path)
        if not meta or "machine" not in meta or "day" not in meta:
            continue
        key = (meta["machine"], meta["day"])
        if key not in candidates or meta["complete"] == "1":
            candidates[key] = (path, meta)

    imported, skipped = [], 0
    try:
        for (machine, day), (path, meta) in sorted(candidates.items()):
            complete = meta["complete"] == "1"
            st = path.stat()
            prev = conn.execute("SELECT complete, size, mtime_ns FROM imports WHERE machine=? AND day=?",
                                (machine, day)).fetchone()
            if prev and (prev[0] == 1 or (not complete and (prev[1], prev[2]) == (st.st_size, st.st_mtime_ns))):
                skipped += 1
                continue
            start, end = int(meta["start_ns"]), int(meta["end_ns"])
            # ATTACH/DETACH must happen outside a transaction; everything in between is atomic
            conn.execute("ATTACH DATABASE ? AS d", (f"file:{path}?mode=ro",))
            try:
                with conn:
                    if prev:
                        _delete_import(conn, machine, start, end)
                        conn.execute("DELETE FROM imports WHERE machine=? AND day=?", (machine, day))
                    n = _import_file(conn, machine)
                    conn.execute(
                        "INSERT INTO imports(machine,day,file,complete,start_ns,end_ns,size,mtime_ns,rows,imported_ns) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (machine, day, str(path), int(complete), start, end, st.st_size, st.st_mtime_ns, n,
                         time.time_ns()))
            finally:
                conn.execute("DETACH DATABASE d")
            imported.append((path, n))
    finally:
        conn.close()
    return imported, skipped
