"""Cut the recording into strokes and measure how alike two strokes are.

Every metric sees strokes resampled to the same number of points along their
arc length. What counts as a difference is decided separately, see Invariance:

  position   centre each stroke on its centroid, or keep screen coordinates
  size       scale each stroke to unit RMS radius, or keep logical pixels
  rotation   turn each candidate about its centroid so it fits the query best
             (least squares, as in Procrustes analysis), or leave it as drawn
  direction  also try each candidate drawn backwards and keep the better fit

Pure numpy. The Qt window in similar.py sits on top of this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from . import query
from .query import NS, NoData


# ---------------------------------------------------------------- segmentation
@dataclass
class StrokeSet:
    """The strokes of one time range. Stroke k is the motion samples [start[k], stop[k])."""

    m: query.Motion
    start: np.ndarray    # int64, inclusive
    stop: np.ndarray     # int64, exclusive
    length: np.ndarray   # path length in logical px
    app: list[str]       # focused app when the stroke began

    def __len__(self) -> int:
        return len(self.start)

    @property
    def t0(self) -> np.ndarray:
        return self.m.t[self.start]

    @property
    def t1(self) -> np.ndarray:
        return self.m.t[self.stop - 1]

    def points(self, k: int) -> np.ndarray:
        """(n, 2) raw samples of stroke k in logical px."""
        s, e = int(self.start[k]), int(self.stop[k])
        return np.column_stack([self.m.x[s:e], self.m.y[s:e]])

    def resampled(self, n: int) -> np.ndarray:
        """(M, n, 2): every stroke resampled to n points along its arc length."""
        out = np.empty((len(self), n, 2))
        for k in range(len(self)):
            out[k] = resample(self.points(k), n)
        return out


def segment(m: query.Motion, press_t: np.ndarray, double_click: float = 0.3,
            min_points: int = 4, min_length: float = 0.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(start, stop, length) of the click-to-click strokes in the motion m.

    A stroke ends at a button press (presses within double_click seconds count
    as one) or at a logger restart. After a click the next stroke starts at the
    click position, so neighbours share that sample. Strokes with fewer than
    min_points samples or shorter than min_length px are dropped.
    """
    if len(m) < 2:
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty, np.zeros(0)
    hard = m.session_breaks()
    soft = m.click_breaks(query.merge_presses(press_t, int(double_click * NS)))
    brk = np.unique(np.concatenate([soft, hard])).astype(np.int64)
    share = ~np.isin(brk, hard)  # after a click the next stroke starts where the click happened
    start = np.concatenate([[0], brk - share.astype(np.int64)]).astype(np.int64)
    stop = np.concatenate([brk, [len(m)]]).astype(np.int64)

    cum = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(m.x), np.diff(m.y)))])
    length = cum[stop - 1] - cum[start]
    keep = (stop - start >= max(min_points, 2)) & (length >= min_length) & (length > 0)
    return start[keep], stop[keep], length[keep]


def load_strokes(conn, rng: query.Range, double_click: float = 0.3,
                 min_points: int = 4, min_length: float = 0.0) -> StrokeSet:
    """Click-to-click strokes of a time range, as in `viz path`; see segment()."""
    m = query.load_motion(conn, rng)
    if len(m) < 2:
        raise NoData("no cursor movement in this range")
    press_t, _ = query.load_presses(conn, rng)
    start, stop, length = segment(m, press_t, double_click, min_points, min_length)
    if len(start) == 0:
        raise NoData("no strokes in this range")
    ft, fa = query.load_focus(conn, rng)
    return StrokeSet(m, start, stop, length, query.app_at(ft, fa, m.t[start]))


def resample(pts: np.ndarray, n: int) -> np.ndarray:
    """n points spaced evenly along the arc length of the polyline pts (k, 2)."""
    pts = np.asarray(pts, dtype=float).reshape(-1, 2)
    if len(pts) > 1:  # repeated points would give a flat spot in the arc length
        pts = pts[np.concatenate([[True], np.any(np.diff(pts, axis=0) != 0, axis=1)])]
    if len(pts) < 2:
        return np.repeat(pts[:1] if len(pts) else np.zeros((1, 2)), n, axis=0)
    seg = np.diff(pts, axis=0)
    s = np.concatenate([[0.0], np.cumsum(np.hypot(seg[:, 0], seg[:, 1]))])
    u = np.linspace(0.0, s[-1], n)
    return np.column_stack([np.interp(u, s, pts[:, 0]), np.interp(u, s, pts[:, 1])])


# ------------------------------------------------------------- normalisation
@dataclass(frozen=True)
class Invariance:
    """Which differences between two strokes are ignored."""

    position: bool = True
    size: bool = True
    rotation: bool = False
    direction: bool = False


def normalise(p: np.ndarray, inv: Invariance) -> np.ndarray:
    """Centre and/or scale resampled strokes, p is (..., n, 2). Scaling is about the centroid."""
    c = p.mean(axis=-2, keepdims=True)
    d = p - c
    if inv.size:
        r = np.sqrt((d * d).sum(axis=(-1, -2), keepdims=True))
        d = d / np.where(r > 0, r, 1.0)
    return d if inv.position else d + c


def rotate_onto(q: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Turn each candidate (M, n, 2) about its centroid by the angle that best fits q (n, 2)."""
    qc = q - q.mean(axis=0)
    cm = c.mean(axis=-2, keepdims=True)
    cc = c - cm
    a = (qc[:, 0] * cc[..., 0] + qc[:, 1] * cc[..., 1]).sum(axis=-1)
    b = (qc[:, 1] * cc[..., 0] - qc[:, 0] * cc[..., 1]).sum(axis=-1)
    th = np.arctan2(b, a)
    cos, sin = np.cos(th)[:, None], np.sin(th)[:, None]
    x, y = cc[..., 0], cc[..., 1]
    return np.stack([x * cos - y * sin, x * sin + y * cos], axis=-1) + cm


# ------------------------------------------------------------------- metrics
def _wrap(a: np.ndarray) -> np.ndarray:
    return (a + np.pi) % (2 * np.pi) - np.pi


def headings(p: np.ndarray) -> np.ndarray:
    """Absolute direction of each segment, (..., n-1)."""
    d = np.diff(p, axis=-2)
    return np.arctan2(d[..., 1], d[..., 0])


def turning(p: np.ndarray) -> np.ndarray:
    """Change of direction at each inner point, (..., n-2). Invariant to position, size and rotation."""
    return _wrap(np.diff(headings(p), axis=-1))


def _warp(cost_row: Callable[[int], np.ndarray], n: int, frechet: bool) -> np.ndarray:
    """Dynamic programming over the n x k alignment grid for all M candidates at once.

    cost_row(i) returns the (k, M) local costs between query step i and every
    candidate step. DTW sums the costs along the warping path, the discrete
    Fréchet distance takes their maximum.
    """
    first = cost_row(0)
    k, m = first.shape
    comb = np.maximum if frechet else np.add
    prev = np.full((k + 1, m), np.inf)
    prev[0] = 0.0  # lets D[0, 0] start from zero; every later row has prev[0] = inf
    for i in range(n):
        c = first if i == 0 else cost_row(i)
        cur = np.full((k + 1, m), np.inf)
        for j in range(k):
            best = np.minimum(np.minimum(prev[j + 1], prev[j]), cur[j])
            cur[j + 1] = comb(best, c[j])
        prev = cur
    return prev[k]


def _point_cost(q: np.ndarray, c: np.ndarray) -> Callable[[int], np.ndarray]:
    return lambda i: np.sqrt(((c - q[i]) ** 2).sum(axis=-1)).T


def _angle_cost(qa: np.ndarray, ca: np.ndarray) -> Callable[[int], np.ndarray]:
    return lambda i: np.abs(_wrap(ca - qa[i])).T


def d_shape(q, c):
    return np.sqrt(((c - q) ** 2).sum(axis=(-1, -2)))


def d_dtw(q, c):
    return _warp(_point_cost(q, c), len(q), frechet=False) / len(q)


def d_frechet(q, c):
    return _warp(_point_cost(q, c), len(q), frechet=True)


def d_hausdorff(q, c, chunk: int = 256):
    out = np.empty(len(c))
    for s in range(0, len(c), chunk):
        cc = c[s:s + chunk]
        d = np.sqrt(((cc[:, None, :, :] - q[None, :, None, :]) ** 2).sum(axis=-1))  # (B, n_q, n_c)
        out[s:s + chunk] = np.maximum(d.min(axis=2).max(axis=1), d.min(axis=1).max(axis=1))
    return out


def d_turning(q, c):
    return np.sqrt((_wrap(turning(c) - turning(q)) ** 2).mean(axis=-1))


def d_turning_dtw(q, c):
    qa = turning(q)
    return _warp(_angle_cost(qa, turning(c)), len(qa), frechet=False) / len(qa)


def d_heading(q, c):
    return np.sqrt((_wrap(headings(c) - headings(q)) ** 2).mean(axis=-1))


def d_heading_dtw(q, c):
    qa = headings(q)
    return _warp(_angle_cost(qa, headings(c)), len(qa), frechet=False) / len(qa)


@dataclass(frozen=True)
class Metric:
    label: str
    about: str
    fn: Callable[[np.ndarray, np.ndarray], np.ndarray]


METRICS: dict[str, Metric] = {
    "shape": Metric("Shape (Procrustes)",
                    "Point-wise distance between the two resampled strokes after normalisation. "
                    "Fast and strict: point i must sit near point i.", d_shape),
    "dtw": Metric("Points, DTW",
                  "Dynamic time warping over the points: the two strokes may run at different "
                  "speeds along the way. Mean cost along the warping path.", d_dtw),
    "frechet": Metric("Points, Fréchet",
                      "Discrete Fréchet distance: the shortest leash that lets two walkers "
                      "traverse both strokes in order. Penalises the single worst deviation.", d_frechet),
    "hausdorff": Metric("Points, Hausdorff",
                        "Largest distance from any point of one stroke to the nearest point of the "
                        "other. Ignores drawing order entirely.", d_hausdorff),
    "turning": Metric("Turning angles",
                      "RMS difference of the direction change at each point. Captures wiggliness; "
                      "invariant to rotation by construction.", d_turning),
    "turning_dtw": Metric("Turning angles, DTW",
                          "Turning angle sequences aligned elastically, so the same bends in a "
                          "different rhythm still match.", d_turning_dtw),
    "heading": Metric("Headings",
                      "RMS difference of the absolute direction of each segment. Like turning "
                      "angles, but a rotated copy counts as different.", d_heading),
    "heading_dtw": Metric("Headings, DTW",
                          "Heading sequences aligned elastically.", d_heading_dtw),
}


# -------------------------------------------------------------------- search
@dataclass
class Result:
    dist: np.ndarray    # (M,) distance of every candidate to the query, inf where undefined
    cand: np.ndarray    # (M, n, 2) the candidates as the metric saw them: normalised, aligned, maybe reversed
    query: np.ndarray   # (n, 2) the normalised query
    order: np.ndarray   # candidate indices, nearest first


def search(metric: str, query_pts: np.ndarray, cands: np.ndarray, inv: Invariance) -> Result:
    """Rank cands (M, n, 2 resampled, raw coordinates) by distance to the drawn query."""
    n = cands.shape[1]
    q = normalise(resample(query_pts, n), inv)
    c = normalise(cands, inv)
    fn = METRICS[metric].fn

    def run(cc: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if inv.rotation:
            cc = rotate_onto(q, cc)
        return fn(q, cc), cc

    d, c1 = run(c)
    if inv.direction:
        d2, c2 = run(c[:, ::-1])
        swap = d2 < d
        d = np.where(swap, d2, d)
        c1 = np.where(swap[:, None, None], c2, c1)
    d = np.where(np.isfinite(d), d, np.inf)
    return Result(d, c1, q, np.argsort(d, kind="stable"))
