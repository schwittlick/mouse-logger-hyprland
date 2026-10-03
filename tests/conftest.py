import json
import sqlite3
from pathlib import Path

import pytest

from mouse_logger.dayfiles import DAYFILE_META, PLAIN_SCHEMA

NS = 1_000_000_000
T0 = 1_767_225_600 * NS  # 2026-01-01 00:00 UTC
MONITORS = json.dumps([{"x": 0, "y": 0, "width": 3840, "height": 2160, "scale": 1.5, "transform": 0}])


def make_day_db(path: Path, machine: str = "testbox", day: str = "2026-01-01", complete: bool = True) -> Path:
    """A day file like dayfiles.export_day writes: one session, three click-to-click strokes, a focus change."""
    conn = sqlite3.connect(path)
    conn.executescript(PLAIN_SCHEMA + DAYFILE_META)
    conn.execute("INSERT INTO sessions(id, started_ns, hostname, monitors, poll_hz) VALUES (1, ?, ?, ?, 250)",
                 (T0, machine, MONITORS))
    motion, buttons = [], []
    ms = 1_000_000
    # stroke A: a short diagonal, 10 samples at 4 ms; click; stroke B: a wiggle, 20 samples; click; stroke C: 5 samples.
    # Clicks are more than 0.3 s apart so they are not merged as a double click. After a click the next
    # stroke starts at the sample the click happened on, so B and C each carry one extra point.
    for i in range(10):
        motion.append((1, T0 + i * 4 * ms, 0, 100 + 50 * i, 100 + 50 * i))
    buttons.append((1, T0 + 40 * ms, 0, "mouse", 272, "BTN_LEFT", 1))
    for i in range(20):
        motion.append((1, T0 + (540 + i * 4) * ms, 0, 1000 + (i % 4) * 50, 1000 + (i // 4) * 50))
    buttons.append((1, T0 + 620 * ms, 0, "mouse", 273, "BTN_RIGHT", 1))
    for i in range(5):
        motion.append((1, T0 + (1120 + i * 4) * ms, 0, 2000 - 10 * i, 300))
    conn.executemany("INSERT INTO motion(session_id, t_ns, mono_ns, x, y) VALUES (?,?,?,?,?)", motion)
    conn.executemany("INSERT INTO buttons(session_id, t_ns, mono_ns, device, code, name, pressed) VALUES (?,?,?,?,?,?,?)",
                     buttons)
    conn.executemany("INSERT INTO focus(session_id, t_ns, mono_ns, address, app_id, title) VALUES (?,?,?,?,?,?)",
                     [(1, T0, T0, "0x1", "kitty", "shell"), (1, T0 + 30_000_000, T0 + 30_000_000, "0x2", "firefox", "web")])
    conn.executemany("INSERT INTO meta VALUES(?,?)", [
        ("machine", machine), ("day", day), ("start_ns", str(T0)), ("end_ns", str(T0 + 86400 * NS)),
        ("complete", "1" if complete else "0"), ("rows", str(len(motion) + len(buttons) + 2)),
    ])
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def day_db(tmp_path: Path) -> Path:
    return make_day_db(tmp_path / "2026-01-01.sqlite")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A data dir with one machine and one complete day."""
    d = tmp_path / "mouse-data" / "testbox"
    d.mkdir(parents=True)
    make_day_db(d / "2026-01-01.sqlite")
    return tmp_path / "mouse-data"
