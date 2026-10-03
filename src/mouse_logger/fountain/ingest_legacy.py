"""One legacy cursor recording (schwittlick/cursor) -> Chunk.

The file is a Python literal {'base64(zip(o))': '<base64 of zlib of JSON>'}.
The JSON is {"mouse": {"paths": [[{"x", "y", "ts", "c"?}, ...], ...], "timestamp"},
"keys": [...]} with x, y already normalised to the primary monitor, ts in
seconds, "c" an optional [r, g, b]. Paths are already split at clicks.
"""

from __future__ import annotations

import ast
import base64
import zlib
from pathlib import Path

import numpy as np
import orjson

from . import metrics
from .cache import Chunk, make_meta

ZIP_KEY = "base64(zip(o))"


def decode_file(path: Path) -> dict:
    text = path.read_text()
    try:
        data = base64.b64decode(ast.literal_eval(text)[ZIP_KEY])
    except (ValueError, SyntaxError, KeyError, TypeError):
        raw = text.encode()  # an uncompressed recording
    else:
        try:
            raw = zlib.decompress(data)
        except zlib.error:
            # a damaged checksum: inflate the raw deflate stream behind the 2-byte zlib header
            raw = zlib.decompressobj(-15).decompress(data[2:])
    try:
        return orjson.loads(raw)
    except orjson.JSONDecodeError:
        return ast.literal_eval(raw.decode())  # a few files hold str(dict) instead of JSON


def _resolution(o: dict) -> tuple[int, int]:
    res = o.get("resolution") or o.get("mouse", {}).get("resolution")
    if isinstance(res, dict):
        return int(res.get("w") or res.get("width") or 0), int(res.get("h") or res.get("height") or 0)
    if isinstance(res, (list, tuple)) and len(res) == 2:
        return int(res[0]), int(res[1])
    return 0, 0


def load(path: Path, file_no: int) -> Chunk:
    o = decode_file(path)
    meta = make_meta("legacy", "legacy", "", path.stem, "", True, path, file_no)
    paths = o.get("mouse", {}).get("paths", []) or []
    pts = [q for p in paths for q in p]
    if not pts:
        return Chunk.empty(meta)

    n_pts = len(pts)
    lengths = np.fromiter((len(p) for p in paths), dtype=np.int64, count=len(paths))
    offsets = np.concatenate([[0], np.cumsum(lengths)])
    x = np.fromiter((q["x"] for q in pts), dtype=np.float64, count=n_pts)
    y = np.fromiter((q["y"] for q in pts), dtype=np.float64, count=n_pts)
    ts = np.fromiter((q.get("ts") or 0.0 for q in pts), dtype=np.float64, count=n_pts)
    colours = [q.get("c") for q in pts]
    has_c = np.fromiter((c is not None for c in colours), dtype=bool, count=n_pts)
    rgb = None
    if has_c.any():
        rgb = np.zeros((n_pts, 3), dtype=np.uint8)
        idx = np.flatnonzero(has_c)
        rgb[idx] = np.array([colours[i][:3] for i in idx], dtype=np.int64).clip(0, 255)
        rgb = rgb.reshape(-1)

    # per-path time base: ms offsets since the first point
    n = len(lengths)
    first = np.minimum(offsets[:-1], max(n_pts - 1, 0))
    ts0 = ts[first]
    pid = np.repeat(np.arange(n), lengths)
    t = np.rint((ts - ts0[pid]) * 1000.0).astype(np.int32)
    t0_ns = np.rint(ts0 * 1e9).astype(np.int64)
    cum_c = np.concatenate([[0], np.cumsum(has_c)])
    has_color = (cum_c[offsets[1:]] - cum_c[offsets[:-1]]) > 0

    # clean the way cursor's Collection.clean() does
    keep, offsets = metrics.dedupe(x, y, offsets)
    x, y, t = x[keep], y[keep], t[keep]
    if rgb is not None:
        rgb = rgb.reshape(-1, 3)[keep].reshape(-1)
    path_keep = np.diff(offsets) >= 2
    pkeep, offsets = metrics.drop_paths(offsets, path_keep)
    x, y, t = x[pkeep], y[pkeep], t[pkeep]
    if rgb is not None:
        rgb = rgb.reshape(-1, 3)[pkeep].reshape(-1)
    t0_ns, has_color = t0_ns[path_keep], has_color[path_keep]
    n = len(t0_ns)
    if n == 0:
        return Chunk.empty(meta)

    w, h = _resolution(o)
    x, y = x.astype(np.float32), y.astype(np.float32)  # metrics come from exactly what is stored and served
    return Chunk(
        meta, t0_ns, offsets, np.array([""] * n, dtype=object),
        np.full(n, w, dtype=np.uint16), np.full(n, h, dtype=np.uint16), has_color,
        metrics.compute(x, y, t, offsets), x, y, t, rgb,
    )
