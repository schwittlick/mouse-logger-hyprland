"""The viz loaders: time range plus the focused-app filter."""

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from mouse_logger import query
from mouse_logger.query import Range

NS = 1_000_000_000
T0 = 1_767_225_600 * NS  # as in conftest
MS = 1_000_000

# The fixture: focus is kitty from T0 and firefox from T0 + 30 ms. Stroke A has samples at
# T0 + 0, 4, ..., 36 ms, so its first 8 belong to kitty; everything later (27 samples, both
# clicks) belongs to firefox.


def test_app_filter_keeps_only_focused_samples(day_db: Path):
    with sqlite3.connect(day_db) as conn:
        assert len(query.load_motion(conn, Range())) == 35
        kitty = query.load_motion(conn, Range(app=("kitty",)))
        assert len(kitty) == 8 and len(kitty.cut) == 0
        assert len(query.load_presses(conn, Range(app=("kitty",)))[0]) == 0
        fox = query.load_motion(conn, Range(app=("fire*",)))
        assert len(fox) == 27 and len(fox.cut) == 0
        assert len(query.load_presses(conn, Range(app=("fire*",)))[0]) == 2
        assert len(query.load_motion(conn, Range(app=("kitty", "firefox")))) == 35
        assert len(query.load_motion(conn, Range(app=("nosuchapp",)))) == 0


def test_app_filter_cuts_where_focus_left(day_db: Path):
    # Focus goes back to kitty at 600 ms (inside stroke B) and to firefox at 1100 ms (before C).
    with sqlite3.connect(day_db) as conn:
        conn.executemany("INSERT INTO focus(session_id, t_ns, mono_ns, address, app_id, title) VALUES (?,?,?,?,?,?)",
                         [(1, T0 + 600 * MS, T0 + 600 * MS, "0x1", "kitty", "shell"),
                          (1, T0 + 1100 * MS, T0 + 1100 * MS, "0x2", "firefox", "web")])
        conn.commit()
    with sqlite3.connect(day_db) as conn:
        m = query.load_motion(conn, Range(app=("kitty",)))
        # 8 samples of A, then the 5 samples of B at 600..616 ms
        assert len(m) == 13
        assert m.cut.tolist() == [8]
        assert m.hard_breaks().tolist() == [8]
        # no distance is counted across the cut, and the strokes do not share a point there
        assert m.step_lengths(gap_ns=10 * NS)[7] == 0.0
        xs, _ = m.stroke_arrays(m.cut, np.zeros(1, dtype=bool))
        assert np.isnan(xs[8]) and len(xs) == 14
        t, code = query.load_presses(conn, Range(app=("kitty",)))
        assert t.tolist() == [T0 + 620 * MS] and code.tolist() == [273]
        t, _ = query.load_presses(conn, Range(app=("firefox",)))
        assert t.tolist() == [T0 + 40 * MS]


def test_app_mask_handles_unknown_focus():
    t = np.array([5, 15, 25], dtype=np.int64)
    assert query.app_mask(np.zeros(0, dtype=np.int64), [], t, ("kitty",)).tolist() == [False] * 3
    assert query.app_mask(np.zeros(0, dtype=np.int64), [], t, ("(none)",)).tolist() == [True] * 3
    ft, fa = np.array([10, 20], dtype=np.int64), [None, "kitty"]
    assert query.app_mask(ft, fa, t, ("kitty",)).tolist() == [False, False, True]
    assert query.app_mask(ft, fa, t, ("(none)",)).tolist() == [True, True, False]


@pytest.mark.parametrize("view", ["path", "heatmap", "activity"])
def test_viz_app_filter_renders(day_db: Path, tmp_path: Path, view: str):
    pytest.importorskip("matplotlib")
    from mouse_logger.cli import main

    out = tmp_path / f"{view}.png"
    args = ["viz", view, "--db", str(day_db), "--since", "all", "--out", str(out)]
    assert main(args + ["--app", "kitty"]) == 0
    assert out.stat().st_size > 0
    assert main(args + ["--app", "nosuchapp"]) == 1
