"""Per-path metrics, vectorised over many paths at once.

The definitions copy the cursor project (cursor/path.py, cursor/bb.py,
cursor/algorithm/entropy.py) so that the thresholds in its compositions keep
their meaning:

  n_points      after cleaning: a point equal in x and y to the previous one
                of the same path is dropped (Path.clean), paths with fewer
                than 2 points are dropped (Collection.clean)
  distance      sum of the segment lengths
  duration_s    last timestamp minus first
  x0 y0 x1 y1   axis-aligned bounding box
  aspect        bbox height / width, +inf when the width is 0, -inf when the height is 0
  entropy_x/y   Shannon entropy in nats of the counts of the distinct x (y) values
  entropy_dc    the same over cursor's "direction changes":
                rad2deg(atan2(y[i-1], x[i-1]) - atan2(y[i], x[i])) for i >= 1,
                differences of the angles of the absolute positions, n-1 values
  variation_x/y std(ddof=1) / mean, NaN -> 0.0, inf -> 100.0

Paths come in CSR form: flat x, y, t arrays plus offsets of length n_paths + 1,
path k being the points [offsets[k], offsets[k+1]). t is in milliseconds.
"""

from __future__ import annotations

import numpy as np

NAMES = ("n_points", "distance", "duration_s", "x0", "y0", "x1", "y1", "aspect",
         "entropy_x", "entropy_y", "entropy_dc", "variation_x", "variation_y")


# ------------------------------------------------------------------ cleaning
def _offsets_after(offsets: np.ndarray, keep: np.ndarray) -> np.ndarray:
    """Offsets of the same paths once the points where keep is False are gone."""
    cum = np.concatenate([[0], np.cumsum(keep, dtype=np.int64)])
    return cum[offsets]


def dedupe(x: np.ndarray, y: np.ndarray, offsets: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(point mask, new offsets): drop points equal in x and y to the previous point of their path."""
    offsets = np.asarray(offsets, dtype=np.int64)
    n = len(x)
    keep = np.ones(n, dtype=bool)
    if n > 1:
        keep[1:] = (x[1:] != x[:-1]) | (y[1:] != y[:-1])
    first = offsets[:-1]
    keep[first[first < n]] = True
    return keep, _offsets_after(offsets, keep)


def drop_paths(offsets: np.ndarray, path_keep: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(point mask, new offsets) that remove whole paths."""
    offsets = np.asarray(offsets, dtype=np.int64)
    cnt = np.diff(offsets)
    keep = np.repeat(path_keep, cnt)
    bounds = np.concatenate([np.flatnonzero(path_keep), [len(cnt)]])
    return keep, _offsets_after(offsets, keep)[bounds]


# ------------------------------------------------------------------- metrics
def _entropy(v: np.ndarray, pid: np.ndarray, n_paths: int, cnt: np.ndarray) -> np.ndarray:
    """Entropy of the value counts within each path. pid is non-decreasing, cnt the values per path."""
    if len(v) == 0:
        return np.zeros(n_paths)
    order = np.lexsort((v, pid))
    vs, ps = v[order], pid[order]
    new = np.ones(len(vs), dtype=bool)
    new[1:] = (ps[1:] != ps[:-1]) | (vs[1:] != vs[:-1])
    starts = np.flatnonzero(new)
    run_len = np.diff(np.append(starts, len(vs)))
    run_pid = ps[starts]
    p = run_len / cnt[run_pid]
    return np.bincount(run_pid, weights=-p * np.log(p), minlength=n_paths)


def _variation(v: np.ndarray, pid: np.ndarray, start: np.ndarray, cnt: np.ndarray) -> np.ndarray:
    mean = np.add.reduceat(v, start) / cnt
    dev = v - mean[pid]
    var = np.add.reduceat(dev * dev, start) / (cnt - 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.sqrt(var) / mean
    return np.where(np.isnan(r), 0.0, np.where(np.isinf(r), 100.0, r))


def compute(x: np.ndarray, y: np.ndarray, t: np.ndarray, offsets: np.ndarray) -> dict[str, np.ndarray]:
    """All metrics for every path. Every path must have at least 2 points (see dedupe, drop_paths)."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    t = np.asarray(t, dtype=np.float64)
    offsets = np.asarray(offsets, dtype=np.int64)
    start, stop = offsets[:-1], offsets[1:]
    cnt = stop - start
    n_paths = len(cnt)
    if n_paths == 0:
        return {k: np.zeros(0, dtype=np.uint32 if k == "n_points" else np.float64) for k in NAMES}
    if (cnt < 2).any():
        raise ValueError("every path needs at least 2 points; apply dedupe and drop_paths first")
    pid = np.repeat(np.arange(n_paths), cnt)

    seg = np.zeros(len(x))
    seg[1:] = np.hypot(np.diff(x), np.diff(y))
    seg[start] = 0.0
    distance = np.add.reduceat(seg, start)
    duration_s = (t[stop - 1] - t[start]) / 1000.0

    x0, x1 = np.minimum.reduceat(x, start), np.maximum.reduceat(x, start)
    y0, y1 = np.minimum.reduceat(y, start), np.maximum.reduceat(y, start)
    w, h = x1 - x0, y1 - y0
    with np.errstate(divide="ignore", invalid="ignore"):
        aspect = np.where(w == 0, np.inf, np.where(h == 0, -np.inf, h / w))

    angle = np.arctan2(y, x)
    same = pid[1:] == pid[:-1]
    dc = np.rad2deg(angle[:-1] - angle[1:])[same]

    return {
        "n_points": cnt.astype(np.uint32),
        "distance": distance,
        "duration_s": duration_s,
        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
        "aspect": aspect,
        "entropy_x": _entropy(x, pid, n_paths, cnt),
        "entropy_y": _entropy(y, pid, n_paths, cnt),
        "entropy_dc": _entropy(dc, pid[1:][same], n_paths, cnt - 1),
        "variation_x": _variation(x, pid, start, cnt),
        "variation_y": _variation(y, pid, start, cnt),
    }
