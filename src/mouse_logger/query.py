"""Read recorded data into numpy arrays. Shared by the viz commands."""

from __future__ import annotations

import itertools
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

NS = 1_000_000_000
BUTTON_NAMES = {272: "left", 273: "right", 274: "middle"}


class NoData(RuntimeError):
    pass


def _local_midnight(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_when(text: str | None, *, end: bool = False) -> int | None:
    """Turn a user-supplied time into unix nanoseconds (local time zone).

    Accepts: all (no bound), now, today, yesterday, a relative age like 30m / 2h / 7d / 1w
    (meaning "that long ago"), or an ISO date/datetime such as 2026-09-30 or
    2026-09-30 14:00. A date-only value used as an end bound means the end of
    that day.
    """
    if text is None:
        return None
    t = text.strip().lower()
    now = datetime.now().astimezone()
    if t == "all":
        return None
    if t == "now":
        return int(now.timestamp() * NS)
    if t in ("today", "yesterday"):
        day = _local_midnight(now) - (timedelta(days=1) if t == "yesterday" else timedelta())
        if end:
            day += timedelta(days=1)
        return int(day.timestamp() * NS)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhdw])", t)
    if m:
        unit = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return int((now - timedelta(seconds=float(m.group(1)) * unit)).timestamp() * NS)
    try:
        dt = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"cannot parse time {text!r} (try today, 2h, 7d, or 2026-09-30 14:00)") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    if end and len(t) == 10:
        dt += timedelta(days=1)
    return int(dt.timestamp() * NS)


def fmt_time(t_ns: int, with_date: bool = True) -> str:
    dt = datetime.fromtimestamp(t_ns / NS).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M")


@dataclass(frozen=True)
class Range:
    since_ns: int | None = None
    until_ns: int | None = None
    session: int | None = None
    machine: str | None = None  # sessions.hostname, useful on a merged archive

    @classmethod
    def from_args(cls, since: str | None, until: str | None, session: int | None,
                  machine: str | None = None) -> "Range":
        return cls(parse_when(since), parse_when(until, end=True), session, machine)

    def sql(self) -> tuple[str, list]:
        conds, args = [], []
        if self.since_ns is not None:
            conds.append("t_ns >= ?")
            args.append(self.since_ns)
        if self.until_ns is not None:
            conds.append("t_ns < ?")
            args.append(self.until_ns)
        if self.session is not None:
            conds.append("session_id = ?")
            args.append(self.session)
        if self.machine is not None:
            conds.append("session_id IN (SELECT id FROM sessions WHERE hostname = ?)")
            args.append(self.machine)
        return (" WHERE " + " AND ".join(conds)) if conds else "", args


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise NoData(f"no database at {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _int_matrix(cur: sqlite3.Cursor, ncols: int) -> np.ndarray:
    flat = np.fromiter(itertools.chain.from_iterable(cur), dtype=np.int64)
    return flat.reshape(-1, ncols)


@dataclass
class Motion:
    t: np.ndarray    # int64 unix ns, sorted
    x: np.ndarray    # float64 logical px
    y: np.ndarray
    sid: np.ndarray  # int64 session id

    def __len__(self) -> int:
        return len(self.t)

    # Stroke boundaries are sample indices i meaning "a new stroke starts at i".

    def session_breaks(self) -> np.ndarray:
        """The logger restarted before sample i: nothing is known about motion in between."""
        if len(self.t) < 2:
            return np.array([], dtype=np.int64)
        return np.flatnonzero(np.diff(self.sid) != 0) + 1

    def rest_breaks(self, gap_ns: int) -> np.ndarray:
        """The cursor rested longer than gap_ns before sample i."""
        if len(self.t) < 2:
            return np.array([], dtype=np.int64)
        return np.flatnonzero(np.diff(self.t) > gap_ns) + 1

    def click_breaks(self, click_t: np.ndarray) -> np.ndarray:
        """Sample i is the first one after a click."""
        idx = np.searchsorted(self.t, click_t, side="right")
        return np.unique(idx[(idx > 0) & (idx < len(self.t))])

    def stroke_arrays(self, brk: np.ndarray, share: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """x and y with NaN between strokes, for one plot call.

        Where share[k] is true the stroke after break brk[k] starts at the last
        point of the previous one (the cursor really moved on from there), so
        the two strokes meet at the click or rest position.
        """
        xs, ys, prev = [], [], 0
        for i, sh in zip(brk, share):
            xs += [self.x[prev:i], [np.nan]]
            ys += [self.y[prev:i], [np.nan]]
            if sh:
                xs.append(self.x[i - 1:i])
                ys.append(self.y[i - 1:i])
            prev = int(i)
        xs.append(self.x[prev:])
        ys.append(self.y[prev:])
        return np.concatenate(xs), np.concatenate(ys)

    def step_lengths(self, gap_ns: int) -> np.ndarray:
        """Distance from sample i to i+1, zero across idle gaps and session changes."""
        if len(self.t) < 2:
            return np.zeros(0)
        d = np.hypot(np.diff(self.x), np.diff(self.y))
        d[(np.diff(self.t) > gap_ns) | (np.diff(self.sid) != 0)] = 0.0
        return d


def load_motion(conn: sqlite3.Connection, rng: Range, stride: int = 1) -> Motion:
    where, args = rng.sql()
    cur = conn.execute(f"SELECT t_ns, x, y, session_id FROM motion{where} ORDER BY t_ns", args)
    a = _int_matrix(cur, 4)
    if stride > 1:
        a = a[::stride]
    return Motion(a[:, 0], a[:, 1].astype(float), a[:, 2].astype(float), a[:, 3])


def merge_presses(t: np.ndarray, within_ns: int) -> np.ndarray:
    """Drop presses that follow another press within within_ns (double and triple clicks)."""
    if len(t) == 0:
        return t
    keep = np.ones(len(t), dtype=bool)
    keep[1:] = np.diff(t) > within_ns
    return t[keep]


def load_presses(conn: sqlite3.Connection, rng: Range) -> tuple[np.ndarray, np.ndarray]:
    """(t_ns, code) of button presses, sorted by time."""
    where, args = rng.sql()
    where = (where + " AND pressed = 1") if where else " WHERE pressed = 1"
    a = _int_matrix(conn.execute(f"SELECT t_ns, code FROM buttons{where} ORDER BY t_ns", args), 2)
    return a[:, 0], a[:, 1]


def load_focus(conn: sqlite3.Connection, rng: Range) -> tuple[np.ndarray, list[str | None]]:
    """Focus change log covering the range, including the state in force at its start."""
    where, args = rng.sql()
    rows = conn.execute(f"SELECT t_ns, app_id FROM focus{where} ORDER BY t_ns", args).fetchall()
    if rng.since_ns is not None:
        prev = conn.execute(
            "SELECT t_ns, app_id FROM focus WHERE t_ns < ? ORDER BY t_ns DESC LIMIT 1", (rng.since_ns,)
        ).fetchone()
        if prev:
            rows.insert(0, prev)
    t = np.array([r[0] for r in rows], dtype=np.int64)
    return t, [r[1] for r in rows]


def app_at(focus_t: np.ndarray, focus_app: list[str | None], t: np.ndarray) -> list[str]:
    """Focused app for each time in t, '(none)' when nothing was focused or known."""
    if len(focus_t) == 0:
        return ["(none)"] * len(t)
    idx = np.searchsorted(focus_t, t, side="right") - 1
    return [(focus_app[i] or "(none)") if i >= 0 else "(none)" for i in idx]


def monitor_rects(conn: sqlite3.Connection, rng: Range) -> list[tuple[float, float, float, float]]:
    """Logical (x, y, w, h) of each monitor from the newest session in range."""
    conds, args = [], []
    if rng.session is not None:
        conds.append("id = ?")
        args.append(rng.session)
    if rng.until_ns is not None:
        conds.append("started_ns < ?")
        args.append(rng.until_ns)
    if rng.machine is not None:
        conds.append("hostname = ?")
        args.append(rng.machine)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    row = conn.execute(f"SELECT monitors FROM sessions{where} ORDER BY started_ns DESC LIMIT 1", args).fetchone()
    if not row or not row[0]:
        return []
    rects = []
    for m in json.loads(row[0]):
        scale = float(m.get("scale") or 1.0)
        w, h = m["width"] / scale, m["height"] / scale
        if int(m.get("transform", 0)) % 2 == 1:  # 90/270 degree rotations
            w, h = h, w
        rects.append((float(m["x"]), float(m["y"]), w, h))
    return rects


def bounds(rects, x: np.ndarray, y: np.ndarray) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1): union of monitor rects, else the data extent with padding."""
    if rects:
        x0 = min(r[0] for r in rects)
        y0 = min(r[1] for r in rects)
        x1 = max(r[0] + r[2] for r in rects)
        y1 = max(r[1] + r[3] for r in rects)
        return x0, y0, x1, y1
    if len(x) == 0:
        return 0.0, 0.0, 1.0, 1.0
    pad = 0.02 * max(np.ptp(x), np.ptp(y), 1.0)
    return x.min() - pad, y.min() - pad, x.max() + pad, y.max() + pad


def hour_edges(t_min: int, t_max: int) -> np.ndarray:
    """Bin edges (unix ns) on local clock hours spanning [t_min, t_max]."""
    start = datetime.fromtimestamp(t_min / NS).astimezone().replace(minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(t_max / NS).astimezone()
    edges = []
    cur = start
    while cur <= end:
        edges.append(int(cur.timestamp() * NS))
        cur += timedelta(hours=1)
    edges.append(int(cur.timestamp() * NS))
    return np.array(edges, dtype=np.int64)
