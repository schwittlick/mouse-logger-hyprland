import sqlite3
from datetime import date
from pathlib import Path

from mouse_logger import query
from mouse_logger.dayfiles import NS, day_bounds, export_day
from mouse_logger.db import SCHEMA

MONITORS = '[{"x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.0, "transform": 0}]'


def make_live_db(path: Path) -> tuple[int, int]:
    """Two days of one session: the only focus change (cs2) happens on day 1, the motion on day 2."""
    start1, _ = day_bounds(date(2026, 1, 1))
    start2, _ = day_bounds(date(2026, 1, 2))
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO sessions(id, started_ns, hostname, monitors, poll_hz) VALUES (1, ?, 'testbox', ?, 250)",
                 (start1, MONITORS))
    conn.execute("INSERT INTO focus(session_id, t_ns, mono_ns, address, app_id, title) VALUES (1, ?, ?, '0x1', 'cs2', 'game')",
                 (start1 + 3600 * NS, 7))
    ms = 1_000_000
    conn.executemany("INSERT INTO motion(session_id, t_ns, mono_ns, x, y) VALUES (1, ?, 0, ?, ?)",
                     [(start2 + i * 4 * ms, 10 * i, 20 * i) for i in range(20)])
    conn.commit()
    conn.close()
    return start1, start2


def test_export_day_carries_focus_state_at_day_start(tmp_path):
    db = tmp_path / "mouse.db"
    start1, start2 = make_live_db(db)
    out = tmp_path / "2026-01-02.sqlite"
    assert export_day(db, out, "testbox", date(2026, 1, 2), True) == 20  # the synthetic row is not an event

    conn = sqlite3.connect(out)
    assert conn.execute("SELECT t_ns, app_id, title FROM focus").fetchall() == [(start2, "cs2", "game")]
    assert conn.execute("SELECT id FROM sessions").fetchall() == [(1,)]
    ft, fa = query.load_focus(conn, query.Range())
    m = query.load_motion(conn, query.Range())
    assert query.app_at(ft, fa, m.t[:1]) == ["cs2"]
    conn.close()

    # day 1 has nothing before its start, so it gets only its own focus row
    out1 = tmp_path / "2026-01-01.sqlite"
    assert export_day(db, out1, "testbox", date(2026, 1, 1), True) == 1
    conn = sqlite3.connect(out1)
    assert conn.execute("SELECT t_ns, app_id FROM focus").fetchall() == [(start1 + 3600 * NS, "cs2")]
    conn.close()
