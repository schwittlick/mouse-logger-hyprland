"""The Index: every path's metadata and metrics in RAM, points left in their chunks.

A query is a boolean mask over the Index, a sort, a slice, and one gather per
chunk that copies the selected points into contiguous output arrays.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

import numpy as np

from .cache import LIVE_FILE_NO, Chunk
from .metrics import NAMES as METRIC_NAMES

SOURCES = ["legacy", "mouse_logger"]
SORT_KEYS = tuple(METRIC_NAMES) + ("t0", "id", "entropy_cross", "random")
META_COLUMNS = ("id", "t0_ns", "screen_w", "screen_h", "has_color", "open", "source", "machine", "app", "recording")


class StringTable:
    """Strings to small integer codes, shared by every chunk of an Index."""

    def __init__(self, values: list[str] | None = None):
        self.values: list[str] = []
        self.codes: dict[str, int] = {}
        for v in values or []:
            self.code(v)

    def code(self, s: str) -> int:
        if s not in self.codes:
            self.codes[s] = len(self.values)
            self.values.append(s)
        return self.codes[s]

    def encode(self, arr: np.ndarray) -> np.ndarray:
        if len(arr) == 0:
            return np.zeros(0, dtype=np.uint16)
        uniq, inv = np.unique(arr.astype(str), return_inverse=True)
        table = np.array([self.code(u) for u in uniq], dtype=np.uint16)
        return table[inv]

    def matching(self, patterns: list[str]) -> list[int]:
        return [i for i, v in enumerate(self.values) if any(fnmatch.fnmatchcase(v, p) for p in patterns)]


@dataclass
class Index:
    chunks: list[Chunk]
    id: np.ndarray          # int64: file_no << 32 | row
    chunk: np.ndarray       # uint16 index into chunks
    local: np.ndarray       # int64 row within the chunk
    t0_ns: np.ndarray
    n_points: np.ndarray    # uint32
    source: np.ndarray      # uint8, index into SOURCES
    machine: np.ndarray     # uint16 codes
    app: np.ndarray
    recording: np.ndarray
    screen_w: np.ndarray
    screen_h: np.ndarray
    has_color: np.ndarray
    open: np.ndarray        # bool, the live stroke still being drawn
    metrics: dict[str, np.ndarray]
    tables: dict[str, StringTable]

    def __len__(self) -> int:
        return len(self.id)

    @property
    def dicts(self) -> dict[str, list[str]]:
        return {"source": SOURCES, **{k: t.values for k, t in self.tables.items()}}

    @classmethod
    def build(cls, chunks: list[Chunk], base: "Index | None" = None) -> "Index":
        """Index over chunks; with base, its arrays are reused and the chunks appended (cheap for a live layer)."""
        tables = base.tables if base else {k: StringTable() for k in ("machine", "app", "recording")}
        names = ("id", "chunk", "local", "t0_ns", "n_points", "source", "machine", "app",
                 "recording", "screen_w", "screen_h", "has_color", "open")
        cols: dict[str, list[np.ndarray]] = {k: ([getattr(base, k)] if base else []) for k in names}
        mets: dict[str, list[np.ndarray]] = {k: ([base.metrics[k]] if base else []) for k in METRIC_NAMES}
        first = len(base.chunks) if base else 0
        chunks = (base.chunks if base else []) + list(chunks)
        for ci, c in enumerate(chunks[first:], start=first):
            n = len(c)
            file_no = int(c.meta.get("file_no", LIVE_FILE_NO))
            local = np.arange(n, dtype=np.int64)
            cols["id"].append((np.int64(file_no) << 32) | local)
            cols["chunk"].append(np.full(n, ci, dtype=np.uint16))
            cols["local"].append(local)
            cols["t0_ns"].append(c.t0_ns.astype(np.int64))
            cols["n_points"].append(np.diff(c.offsets).astype(np.uint32))
            cols["source"].append(np.full(n, SOURCES.index(c.meta.get("source", "mouse_logger")), dtype=np.uint8))
            cols["machine"].append(np.full(n, tables["machine"].code(c.meta.get("machine", "")), dtype=np.uint16))
            cols["recording"].append(np.full(n, tables["recording"].code(c.meta.get("recording", "")), dtype=np.uint16))
            cols["app"].append(tables["app"].encode(c.app))
            cols["screen_w"].append(c.screen_w.astype(np.uint16))
            cols["screen_h"].append(c.screen_h.astype(np.uint16))
            cols["has_color"].append(c.has_color.astype(bool))
            cols["open"].append(c.open.astype(bool) if c.open is not None else np.zeros(n, dtype=bool))
            for k in METRIC_NAMES:
                mets[k].append(c.metrics[k])
        cat = {k: (np.concatenate(v) if v else np.zeros(0)) for k, v in cols.items()}
        metrics = {k: (np.concatenate(v) if v else np.zeros(0)) for k, v in mets.items()}
        return cls(chunks, metrics=metrics, tables=tables, **cat)

    # ------------------------------------------------------------- selection
    def mask(self, q: "Query") -> np.ndarray:
        m = np.ones(len(self), dtype=bool)
        if not q.include_open:
            m &= ~self.open
        met = self.metrics

        def rng(col: np.ndarray, lo, hi, lo_strict: bool, hi_strict: bool) -> None:
            nonlocal m
            if lo is not None:
                m &= (col > lo) if lo_strict else (col >= lo)
            if hi is not None:
                m &= (col < hi) if hi_strict else (col <= hi)

        rng(met["n_points"], q.min_points, q.max_points, False, False)
        rng(met["entropy_x"], q.entropy_x_min, q.entropy_x_max, True, True)
        rng(met["entropy_y"], q.entropy_y_min, q.entropy_y_max, True, True)
        rng(met["entropy_dc"], q.entropy_dc_min, q.entropy_dc_max, True, True)
        rng(met["distance"], q.distance_min, q.distance_max, True, False)
        rng(met["aspect"], q.aspect_min, q.aspect_max, True, True)
        rng(met["variation_x"], q.variation_x_min, q.variation_x_max, False, False)
        rng(met["variation_y"], q.variation_y_min, q.variation_y_max, False, False)
        rng(met["duration_s"], q.duration_min, q.duration_max, False, False)
        rng(self.t0_ns, q.since_ns, q.until_ns, False, True)
        if q.bbox is not None:
            bx0, by0, bx1, by1 = q.bbox
            if q.bbox_mode == "intersects":
                m &= (met["x1"] >= bx0) & (met["x0"] <= bx1) & (met["y1"] >= by0) & (met["y0"] <= by1)
            else:  # every point inside, inclusive, as cursor's Position.inside
                m &= (met["x0"] >= bx0) & (met["x1"] <= bx1) & (met["y0"] >= by0) & (met["y1"] <= by1)
        if q.source:
            m &= np.isin(self.source, [SOURCES.index(s) for s in q.source if s in SOURCES])
        for name, want in (("machine", q.machine), ("app", q.app), ("recording", q.recording)):
            if want:
                m &= np.isin(getattr(self, name), self.tables[name].matching(want))
        if q.has_color is not None:
            m &= self.has_color == q.has_color
        if q.ids is not None:
            m &= np.isin(self.id, np.asarray(q.ids, dtype=np.int64))
        return m

    def select(self, q: "Query") -> tuple[np.ndarray, int]:
        """(rows in output order, total matches before offset/limit)."""
        cand = np.flatnonzero(self.mask(q))
        total = len(cand)
        if q.sort == "random":
            cand = cand[np.random.default_rng(q.seed).permutation(total)]
        elif q.sort:
            key = self.sort_key(q.sort)[cand]
            order = np.argsort(key, kind="stable")
            cand = cand[order[::-1] if q.order == "desc" else order]
        elif q.order == "desc":
            cand = cand[::-1]
        stop = None if q.limit == 0 else q.offset + q.limit
        return cand[q.offset:stop], total

    def sort_key(self, name: str) -> np.ndarray:
        if name == "t0":
            return self.t0_ns
        if name == "id":
            return self.id
        if name == "entropy_cross":
            return self.metrics["entropy_x"] * self.metrics["entropy_y"]
        return self.metrics[name]

    # ---------------------------------------------------------------- gather
    def gather(self, rows: np.ndarray, with_t: bool = True, with_rgb: bool = False,
               resample: int | None = None) -> dict:
        """Columnar arrays for the given rows, points copied in row order."""
        rows = np.asarray(rows, dtype=np.int64)
        n = len(rows)
        cnt = self.n_points[rows].astype(np.int64)
        out_off = np.concatenate([[0], np.cumsum(cnt)])
        m = int(out_off[-1])
        x = np.empty(m, dtype=np.float32)
        y = np.empty(m, dtype=np.float32)
        t = np.empty(m, dtype=np.int32) if with_t else None
        rgb = np.zeros((m, 3), dtype=np.uint8) if with_rgb else None
        ch, loc = self.chunk[rows], self.local[rows]
        for ci in np.unique(ch):
            p = np.flatnonzero(ch == ci)
            c = self.chunks[ci]
            k = cnt[p]
            total = int(k.sum())
            src = np.repeat(c.offsets[loc[p]] - (np.cumsum(k) - k), k) + np.arange(total)
            dst = np.repeat(out_off[p] - (np.cumsum(k) - k), k) + np.arange(total)
            x[dst] = c.x[src]
            y[dst] = c.y[src]
            if t is not None:
                t[dst] = c.t[src]
            if rgb is not None and c.rgb is not None:
                rgb[dst] = c.rgb.reshape(-1, 3)[src]
        if resample:
            x, y, t, rgb, out_off = _resample_all(x, y, t, rgb, out_off, resample)
        g = {
            "offsets": out_off.astype(np.uint32), "x": x, "y": y,
            "id": self.id[rows], "t0_ns": self.t0_ns[rows],
            "screen_w": self.screen_w[rows], "screen_h": self.screen_h[rows],
            "has_color": self.has_color[rows].astype(np.uint8), "open": self.open[rows].astype(np.uint8),
            "source": self.source[rows], "machine": self.machine[rows], "app": self.app[rows],
            "recording": self.recording[rows],
            "metrics": {k: v[rows] for k, v in self.metrics.items()},
        }
        if t is not None:
            g["t"] = t
        if rgb is not None:
            g["rgb"] = rgb.reshape(-1)
        return g

    # ----------------------------------------------------------------- stats
    def stats(self) -> dict:
        n = len(self)
        if n == 0:
            return {"paths": 0, "points": 0}

        def by(codes: np.ndarray, names: list[str]) -> dict[str, int]:
            cnt = np.bincount(codes, minlength=len(names))
            return {names[i]: int(cnt[i]) for i in np.flatnonzero(cnt)}

        pct = {}
        for k in METRIC_NAMES:
            v = self.metrics[k].astype(np.float64)
            v = v[np.isfinite(v)]
            if len(v):
                p = np.percentile(v, [5, 50, 95])
                pct[k] = {"p5": float(p[0]), "p50": float(p[1]), "p95": float(p[2])}
        apps = by(self.app, self.tables["app"].values)
        top = dict(sorted(apps.items(), key=lambda kv: -kv[1])[:15])
        return {
            "paths": n, "points": int(self.n_points.sum(dtype=np.int64)),
            "open": int(self.open.sum()), "chunks": len(self.chunks),
            "t0_min_ns": int(self.t0_ns.min()), "t0_max_ns": int(self.t0_ns.max()),
            "by_source": by(self.source, SOURCES),
            "by_machine": by(self.machine, self.tables["machine"].values),
            "by_recording": by(self.recording, self.tables["recording"].values),
            "top_apps": top, "metrics": pct,
        }


def _resample_all(x, y, t, rgb, off, n: int):
    """Every path to n points spaced evenly along its arc length (t and rgb interpolated)."""
    k = len(off) - 1
    nx = np.empty(k * n, dtype=np.float32)
    ny = np.empty(k * n, dtype=np.float32)
    nt = np.empty(k * n, dtype=np.int32) if t is not None else None
    nrgb = np.empty((k * n, 3), dtype=np.uint8) if rgb is not None else None
    for i in range(k):
        s, e = int(off[i]), int(off[i + 1])
        px, py = x[s:e].astype(np.float64), y[s:e].astype(np.float64)
        d = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(px), np.diff(py)))])
        if d[-1] <= 0:
            d = np.arange(len(px), dtype=np.float64)
        u = np.linspace(0.0, d[-1], n)
        nx[i * n:(i + 1) * n] = np.interp(u, d, px)
        ny[i * n:(i + 1) * n] = np.interp(u, d, py)
        if nt is not None:
            nt[i * n:(i + 1) * n] = np.rint(np.interp(u, d, t[s:e].astype(np.float64)))
        if nrgb is not None:
            for ch in range(3):
                nrgb[i * n:(i + 1) * n, ch] = np.rint(np.interp(u, d, rgb[s:e, ch].astype(np.float64)))
    return nx, ny, nt, nrgb, np.arange(0, (k + 1) * n, n, dtype=np.int64)


@dataclass
class Query:
    min_points: int | None = None
    max_points: int | None = None
    entropy_x_min: float | None = None
    entropy_x_max: float | None = None
    entropy_y_min: float | None = None
    entropy_y_max: float | None = None
    entropy_dc_min: float | None = None
    entropy_dc_max: float | None = None
    distance_min: float | None = None
    distance_max: float | None = None
    aspect_min: float | None = None
    aspect_max: float | None = None
    variation_x_min: float | None = None
    variation_x_max: float | None = None
    variation_y_min: float | None = None
    variation_y_max: float | None = None
    duration_min: float | None = None
    duration_max: float | None = None
    bbox: tuple[float, float, float, float] | None = None
    bbox_mode: str = "inside"
    since_ns: int | None = None
    until_ns: int | None = None
    source: list[str] = field(default_factory=list)
    machine: list[str] = field(default_factory=list)
    app: list[str] = field(default_factory=list)
    recording: list[str] = field(default_factory=list)
    has_color: bool | None = None
    ids: list[int] | None = None
    include_open: bool = False
    sort: str | None = None
    order: str = "asc"
    seed: int | None = None
    limit: int = 200
    offset: int = 0
