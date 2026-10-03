"""Encode a gathered selection for the wire: columnar msgpack, or the same document as JSON.

Every array becomes one little-endian C-order byte string (msgpack bin) or
one list (JSON). A client rebuilds path k from points [offsets[k], offsets[k+1]).
"""

from __future__ import annotations

import msgpack
import numpy as np
import orjson

from . import WIRE_VERSION


def _le(a: np.ndarray) -> np.ndarray:
    a = np.ascontiguousarray(a)
    if a.dtype == np.bool_:
        a = a.astype(np.uint8)
    if a.dtype.byteorder == ">":
        a = a.byteswap().view(a.dtype.newbyteorder("<"))
    return a


def _bin(a: np.ndarray) -> bytes:
    return _le(a).tobytes()


def _lst(a: np.ndarray) -> list:
    return _le(a).tolist()


def document(g: dict, total: int, took_ms: float, dicts: dict, meta: bool, binary: bool) -> dict:
    conv = _bin if binary else _lst
    d = {
        "v": WIRE_VERSION, "coord": "norm", "took_ms": round(took_ms, 3), "total": int(total),
        "n": int(len(g["id"])), "m": int(len(g["x"])),
        "offsets": conv(g["offsets"]), "x": conv(g["x"]), "y": conv(g["y"]),
    }
    if "t" in g:
        d["t"] = conv(g["t"])
    if "rgb" in g:
        d["rgb"] = conv(g["rgb"])
    for k in ("id", "t0_ns", "screen_w", "screen_h", "has_color", "open", "source", "machine", "app", "recording"):
        d[k] = conv(g[k])
    d["dict"] = dicts
    if meta:
        d["metrics"] = {k: conv(v) for k, v in g["metrics"].items()}
    return d


def encode_msgpack(d: dict) -> bytes:
    return msgpack.packb(d, use_bin_type=True)


def encode_json(d: dict) -> bytes:
    return orjson.dumps(d)  # NaN and inf become null


def dtypes() -> dict[str, str]:
    """What a client should use with numpy.frombuffer."""
    return {"offsets": "<u4", "x": "<f4", "y": "<f4", "t": "<i4", "rgb": "u1", "id": "<i8", "t0_ns": "<i8",
            "screen_w": "<u2", "screen_h": "<u2", "has_color": "u1", "open": "u1", "source": "u1",
            "machine": "<u2", "app": "<u2", "recording": "<u2", "n_points": "<u4", "metrics": "<f8"}


def encode_arrow(g: dict, total: int, took_ms: float, dicts: dict, meta: bool) -> bytes:
    """The same selection as an Arrow IPC stream, one row per path, list columns for the points.

    For pandas, polars, DuckDB, R or any Arrow-speaking language:
        table = pyarrow.ipc.open_stream(body).read_all()
    Codes for source/machine/app/recording are resolved to strings here.
    """
    import json

    import pyarrow as pa
    import pyarrow.ipc as ipc

    offsets = pa.array(g["offsets"].astype(np.int32), type=pa.int32())

    def lists(values: np.ndarray, typ, per: int = 1) -> pa.ListArray:
        off = offsets if per == 1 else pa.array(g["offsets"].astype(np.int32) * per, type=pa.int32())
        return pa.ListArray.from_arrays(off, pa.array(values, type=typ))

    cols = {
        "id": pa.array(g["id"], type=pa.int64()),
        "t0_ns": pa.array(g["t0_ns"], type=pa.int64()),
        "source": pa.array([dicts["source"][i] for i in g["source"]], type=pa.string()),
        "machine": pa.array([dicts["machine"][i] for i in g["machine"]], type=pa.string()),
        "app": pa.array([dicts["app"][i] for i in g["app"]], type=pa.string()),
        "recording": pa.array([dicts["recording"][i] for i in g["recording"]], type=pa.string()),
        "screen_w": pa.array(g["screen_w"], type=pa.uint16()),
        "screen_h": pa.array(g["screen_h"], type=pa.uint16()),
        "has_color": pa.array(g["has_color"].astype(bool), type=pa.bool_()),
        "open": pa.array(g["open"].astype(bool), type=pa.bool_()),
    }
    if meta:
        for k, v in g["metrics"].items():
            cols[k] = pa.array(v, type=pa.uint32() if k == "n_points" else pa.float64())
    cols["x"] = lists(g["x"], pa.float32())
    cols["y"] = lists(g["y"], pa.float32())
    if "t" in g:
        cols["t"] = lists(g["t"], pa.int32())
    if "rgb" in g:
        cols["rgb"] = lists(g["rgb"], pa.uint8(), per=3)
    table = pa.table(cols).replace_schema_metadata({
        "fountain": json.dumps({"v": WIRE_VERSION, "coord": "norm", "total": int(total), "took_ms": round(took_ms, 3)}),
    })
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    return sink.getvalue().to_pybytes()
