"""mouse_logger recordings -> Chunk: a day file, or the live database.

Strokes run click to click (strokes.segment). Coordinates are normalised to
the session's reference monitor (query.reference_rect), timestamps become
ms offsets from the stroke's first sample.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import numpy as np

from .. import dayfiles, query, strokes
from .cache import PARAMS, Chunk, make_meta
from . import metrics

Rect = tuple[float, float, float, float]


def session_rects(conn) -> dict[int, Rect | None]:
    """Reference rect per session id."""
    return {int(sid): query.reference_rect(query.rects_from_json(mon))
            for sid, mon in conn.execute("SELECT id, monitors FROM sessions")}


def chunk_from_motion(m: query.Motion, start: np.ndarray, stop: np.ndarray, app: list[str],
                      rects: dict[int, Rect | None], meta: dict) -> Chunk:
    """Build a chunk from strokes [start, stop) over the motion m."""
    n = len(start)
    if n == 0:
        return Chunk.empty(meta)
    cnt = stop - start
    idx = np.repeat(start - (np.cumsum(cnt) - cnt), cnt) + np.arange(int(cnt.sum()))
    xp, yp, tn, sid = m.x[idx], m.y[idx], m.t[idx], m.sid[idx]

    fallback = None
    usid, inv = np.unique(sid, return_inverse=True)
    rows = []
    for s in usid:
        r = rects.get(int(s))
        if r is None:
            if fallback is None:
                x0, y0, x1, y1 = query.bounds([], m.x, m.y)
                fallback = (x0, y0, max(x1 - x0, 1.0), max(y1 - y0, 1.0), 0.0)  # unknown screen size
            rows.append(fallback)
        else:
            rows.append((*r, 1.0))
    table = np.array(rows, dtype=np.float64)        # rx, ry, rw, rh, known
    per_pt = table[inv]
    x = (xp - per_pt[:, 0]) / per_pt[:, 2]
    y = (yp - per_pt[:, 1]) / per_pt[:, 3]

    pid = np.repeat(np.arange(n), cnt)
    t0_ns = m.t[start]
    t = ((tn - t0_ns[pid] + 500_000) // 1_000_000).astype(np.int32)
    first = table[inv[np.cumsum(cnt) - cnt]]          # the session of each stroke's first sample
    known = first[:, 4] > 0
    screen_w = np.where(known, first[:, 2], 0).astype(np.uint16)
    screen_h = np.where(known, first[:, 3], 0).astype(np.uint16)
    app_arr = np.array(list(app), dtype=object)
    offsets = np.concatenate([[0], np.cumsum(cnt)])

    keep, offsets = metrics.dedupe(x, y, offsets)
    x, y, t = x[keep], y[keep], t[keep]
    path_keep = np.diff(offsets) >= 2
    pkeep, offsets = metrics.drop_paths(offsets, path_keep)
    x, y, t = x[pkeep], y[pkeep], t[pkeep]
    t0_ns, screen_w, screen_h, app_arr = t0_ns[path_keep], screen_w[path_keep], screen_h[path_keep], app_arr[path_keep]
    if len(t0_ns) == 0:
        return Chunk.empty(meta)
    x, y = x.astype(np.float32), y.astype(np.float32)  # metrics come from exactly what is stored and served
    return Chunk(
        meta, t0_ns, offsets, app_arr, screen_w, screen_h, np.zeros(len(t0_ns), dtype=bool),
        metrics.compute(x, y, t, offsets), x, y, t, None,
    )


def load(path: Path, file_no: int) -> Chunk:
    """A day file (or any database with the recorder schema) as one chunk."""
    info = dayfiles._read_meta(path) or {}
    machine = str(info.get("machine") or dayfiles.machine_name())
    day = str(info.get("day") or path.name.removesuffix(".sqlite").removesuffix(".partial"))
    complete = str(info.get("complete", "1")).lower() in ("1", "true")
    meta = make_meta("day", "mouse_logger", machine, f"{machine}/{day}", day, complete, path, file_no)
    meta["start_ns"] = int(info.get("start_ns") or 0)
    meta["end_ns"] = int(info.get("end_ns") or 0)
    with closing(query.connect(path)) as conn:
        try:
            ss = strokes.load_strokes(conn, query.Range(), PARAMS["double_click"], min_points=PARAMS["min_points"])
        except query.NoData:
            return Chunk.empty(meta)
        rects = session_rects(conn)
    return chunk_from_motion(ss.m, ss.start, ss.stop, ss.app, rects, meta)


# ------------------------------------------------------------------ live tail
class LiveTail:
    """Today's strokes straight from the live database, refreshed incrementally.

    Serves everything from covered_until_ns on, the end of the newest complete
    day chunk of this machine, so a day is read from the live db until its day
    file exists. Produces two chunks: the closed strokes, and the one still
    being drawn (flagged open, excluded from queries unless asked for).
    """

    def __init__(self, db: Path, covered_until_ns: int, machine: str, double_click: float = PARAMS["double_click"]):
        self.db, self.covered, self.machine, self.double_click = db, covered_until_ns, machine, double_click
        self.t = np.zeros(0, dtype=np.int64)
        self.x = np.zeros(0)
        self.y = np.zeros(0)
        self.sid = np.zeros(0, dtype=np.int64)
        self.last_id = 0
        self.rects: dict[int, Rect | None] = {}
        self.presses = np.zeros(0, dtype=np.int64)
        self.focus_t, self.focus_app = np.zeros(0, dtype=np.int64), []
        self.closed_start = np.zeros(0, dtype=np.int64)
        self.closed_stop = np.zeros(0, dtype=np.int64)
        self.open_start = 0
        self.closed_chunk = Chunk.empty(self._meta(False))
        self.open_chunk = Chunk.empty(self._meta(True))
        self._closed_built = 0
        self._open_built = (0, 0)
        self.refreshed_ns = 0

    def _meta(self, open_: bool) -> dict:
        from .cache import LIVE_FILE_NO
        m = make_meta("live", "mouse_logger", self.machine, f"{self.machine}/live", "", False, None,
                      LIVE_FILE_NO - 1 if open_ else LIVE_FILE_NO)
        m["open"] = open_
        return m

    def chunks(self) -> list[Chunk]:
        return [self.closed_chunk, self.open_chunk]

    @property
    def motion(self) -> query.Motion:
        return query.Motion(self.t, self.x, self.y, self.sid)

    def refresh(self) -> bool:
        """Pull new rows from the database. True when a chunk changed."""
        try:
            conn = query.connect(self.db)
        except query.NoData:
            return False
        with closing(conn):
            rows = conn.execute(
                "SELECT id, t_ns, x, y, session_id FROM motion WHERE id > ? AND t_ns >= ? ORDER BY id",
                (self.last_id, self.covered)).fetchall()
            if rows:
                a = np.array(rows, dtype=np.int64)
                self.t = np.concatenate([self.t, a[:, 1]])
                self.x = np.concatenate([self.x, a[:, 2].astype(float)])
                self.y = np.concatenate([self.y, a[:, 3].astype(float)])
                self.sid = np.concatenate([self.sid, a[:, 4]])
                self.last_id = int(a[-1, 0])
                if any(int(s) not in self.rects for s in np.unique(a[:, 4])):
                    self.rects.update(session_rects(conn))
            rng = query.Range(since_ns=self.covered)
            self.presses = query.load_presses(conn, rng)[0]
            self.focus_t, self.focus_app = query.load_focus(conn, rng)
        self.refreshed_ns = int(__import__("time").time_ns())
        self._segment()
        return self._rebuild()

    def _segment(self) -> None:
        """Close strokes that ended since the last call; the rest stays open."""
        o = self.open_start
        suf = query.Motion(self.t[o:], self.x[o:], self.y[o:], self.sid[o:])
        if len(suf) < 2:
            return
        hard = suf.session_breaks()
        soft = suf.click_breaks(query.merge_presses(self.presses, int(self.double_click * query.NS)))
        brk = np.unique(np.concatenate([soft, hard])).astype(np.int64)
        if len(brk) == 0:
            return
        last = int(brk[-1])
        share = last not in set(hard.tolist())
        closed = query.Motion(suf.t[:last], suf.x[:last], suf.y[:last], suf.sid[:last])
        cs, ce, _ = strokes.segment(closed, self.presses, self.double_click, min_points=PARAMS["min_points"])
        self.closed_start = np.concatenate([self.closed_start, cs + o])
        self.closed_stop = np.concatenate([self.closed_stop, ce + o])
        self.open_start = o + last - (1 if share else 0)

    def _rebuild(self) -> bool:
        changed = False
        m = self.motion
        if len(self.closed_start) != self._closed_built:
            app = query.app_at(self.focus_t, self.focus_app, self.t[self.closed_start])
            self.closed_chunk = chunk_from_motion(m, self.closed_start, self.closed_stop, app, self.rects, self._meta(False))
            self._closed_built = len(self.closed_start)
            changed = True
        s, e = self.open_start, len(self.t)
        if e - s >= 2:
            if (s, e) != self._open_built:
                app = query.app_at(self.focus_t, self.focus_app, self.t[[s]])
                c = chunk_from_motion(m, np.array([s]), np.array([e]), app, self.rects, self._meta(True))
                c.open = np.ones(len(c), dtype=bool)
                self.open_chunk = c
                self._open_built = (s, e)
                changed = True
        elif len(self.open_chunk):
            self.open_chunk = Chunk.empty(self._meta(True))
            self._open_built = (0, 0)
            changed = True
        return changed

    def status(self) -> dict:
        return {"covered_until_ns": self.covered, "motion_rows": int(len(self.t)), "presses": int(len(self.presses)),
                "closed_strokes": int(len(self.closed_start)), "open_points": int(len(self.t) - self.open_start),
                "refreshed_ns": self.refreshed_ns}
