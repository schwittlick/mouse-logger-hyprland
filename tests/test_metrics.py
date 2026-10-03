"""The vectorised metrics must equal cursor's per-path definitions."""

import time

import numpy as np
import pytest

from mouse_logger.fountain import metrics


# --- per-path reference, written the way cursor/path.py does it -------------
def ref_entropy(values) -> float:
    _, counts = np.unique(np.asarray(values, dtype=float), return_counts=True)
    p = counts / counts.sum()
    return float(-(p * np.log(p)).sum())


def ref_direction_changes(x, y) -> list[float]:
    return [float(np.rad2deg(np.arctan2(y[i - 1], x[i - 1]) - np.arctan2(y[i], x[i]))) for i in range(1, len(x))]


def ref_variation(v) -> float:
    v = np.asarray(v, dtype=float)
    with np.errstate(all="ignore"):
        r = np.std(v, ddof=1) / np.mean(v)
    if np.isnan(r):
        return 0.0
    if np.isinf(r):
        return 100.0
    return float(r)


def ref_metrics(x, y, t) -> dict[str, float]:
    x, y, t = (np.asarray(a, dtype=float) for a in (x, y, t))
    w, h = x.max() - x.min(), y.max() - y.min()
    return {
        "n_points": len(x),
        "distance": float(np.sum(np.hypot(np.diff(x), np.diff(y)))),
        "duration_s": (t[-1] - t[0]) / 1000.0,
        "x0": x.min(), "y0": y.min(), "x1": x.max(), "y1": y.max(),
        "aspect": np.inf if w == 0 else (-np.inf if h == 0 else h / w),
        "entropy_x": ref_entropy(x),
        "entropy_y": ref_entropy(y),
        "entropy_dc": ref_entropy(ref_direction_changes(x, y)),
        "variation_x": ref_variation(x),
        "variation_y": ref_variation(y),
    }


def make_paths(rng, n_paths: int, max_len: int = 60, decimals: int | None = 2):
    xs, ys, ts, offsets = [], [], [], [0]
    for _ in range(n_paths):
        n = int(rng.integers(2, max_len))
        x, y = rng.random(n) * 1.5 - 0.2, rng.random(n) * 1.5 - 0.2
        if decimals is not None:  # repeated values, like 4-decimal legacy data
            x, y = np.round(x, decimals), np.round(y, decimals)
        xs.append(x)
        ys.append(y)
        ts.append(np.cumsum(rng.integers(0, 50, n)))
        offsets.append(offsets[-1] + n)
    return (np.concatenate(xs).astype(np.float32), np.concatenate(ys).astype(np.float32),
            np.concatenate(ts).astype(np.int32), np.array(offsets, dtype=np.int64))


def assert_matches(x, y, t, offsets):
    got = metrics.compute(x, y, t, offsets)
    for k in range(len(offsets) - 1):
        s, e = offsets[k], offsets[k + 1]
        want = ref_metrics(x[s:e], y[s:e], t[s:e])
        for name in metrics.NAMES:
            g, w = float(got[name][k]), float(want[name])
            if np.isinf(w):
                assert g == w, (name, k, g, w)
            else:
                assert abs(g - w) <= 1e-9, (name, k, g, w)


def test_random_paths_match_reference():
    rng = np.random.default_rng(1)
    x, y, t, offsets = make_paths(rng, 300)
    assert_matches(x, y, t, offsets)


def test_continuous_values_match_reference():
    rng = np.random.default_rng(2)
    x, y, t, offsets = make_paths(rng, 100, decimals=None)
    assert_matches(x, y, t, offsets)


def test_degenerate_paths():
    paths = [
        ([0.0, 1.0], [0.0, 1.0], [0, 1000]),                       # two points
        ([0.5, 0.5, 0.5], [0.1, 0.2, 0.3], [0, 10, 20]),             # constant x: entropy 0, variation 0, vertical line
        ([0.1, 0.2, 0.3], [0.5, 0.5, 0.5], [0, 10, 20]),             # horizontal line: aspect -inf
        ([-1.0, 1.0], [0.0, 0.0], [0, 5]),                           # mean-zero x: variation inf -> 100
        ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0, 5, 7]),               # all zero: 0/0 -> 0.0
        ([0.3, 0.3, 0.7], [0.3, 0.7, 0.3], [0, 0, 0]),               # zero duration
    ]
    x = np.concatenate([p[0] for p in paths]).astype(np.float32)
    y = np.concatenate([p[1] for p in paths]).astype(np.float32)
    t = np.concatenate([p[2] for p in paths]).astype(np.int32)
    offsets = np.cumsum([0] + [len(p[0]) for p in paths])
    assert_matches(x, y, t, offsets)
    got = metrics.compute(x, y, t, offsets)
    assert got["aspect"][1] == np.inf and got["aspect"][2] == -np.inf
    assert got["variation_x"][3] == 100.0 and got["variation_x"][4] == 0.0
    assert got["entropy_x"][1] == 0.0 and got["duration_s"][5] == 0.0


def test_dedupe_and_drop():
    x = np.array([0, 0, 1, 1, 2, 5, 5, 7, 7, 7], dtype=np.float32)
    y = np.array([0, 0, 1, 1, 2, 5, 5, 7, 7, 7], dtype=np.float32)
    offsets = np.array([0, 5, 7, 10])  # path0: 5 pts -> 3, path1: 2 pts -> 1, path2: 3 pts -> 1
    keep, off = metrics.dedupe(x, y, offsets)
    assert keep.tolist() == [True, False, True, False, True, True, False, True, False, False]
    assert off.tolist() == [0, 3, 4, 5]
    x, y = x[keep], y[keep]
    path_keep = np.diff(off) >= 2
    assert path_keep.tolist() == [True, False, False]
    pkeep, off2 = metrics.drop_paths(off, path_keep)
    assert x[pkeep].tolist() == [0, 1, 2] and off2.tolist() == [0, 3]
    # every path empty / nothing left
    pkeep, off3 = metrics.drop_paths(off, np.zeros(3, dtype=bool))
    assert pkeep.sum() == 0 and off3.tolist() == [0]
    assert metrics.compute(x[:0], y[:0], x[:0], off3)["n_points"].shape == (0,)


def test_requires_two_points():
    with pytest.raises(ValueError):
        metrics.compute(np.zeros(3), np.zeros(3), np.zeros(3), np.array([0, 1, 3]))


def test_speed_half_million_points():
    rng = np.random.default_rng(3)
    x, y, t, offsets = make_paths(rng, 10_000, max_len=100)
    assert len(x) > 400_000
    t0 = time.perf_counter()
    metrics.compute(x, y, t, offsets)
    assert time.perf_counter() - t0 < 1.0
