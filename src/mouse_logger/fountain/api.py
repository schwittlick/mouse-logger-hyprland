"""The HTTP endpoints. State (serve.py) carries the Index, the live tail and the hub."""

from __future__ import annotations

import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import JSONResponse

from .. import __version__, query
from . import WIRE_VERSION, wire
from .store import SORT_KEYS, SOURCES, Query

MEDIA = {"msgpack": "application/msgpack", "json": "application/json",
         "arrow": "application/vnd.apache.arrow.stream"}
FIELDS = ("xy", "xyt", "xytc")


class BadRequest(HTTPException):
    def __init__(self, msg: str):
        super().__init__(status_code=400, detail=msg)


def _num(qp, name: str, conv):
    v = qp.get(name)
    if v is None or v == "":
        return None
    try:
        return conv(v)
    except ValueError:
        raise BadRequest(f"{name}: not a number: {v!r}") from None


def _bool(qp, name: str) -> bool | None:
    v = qp.get(name)
    if v is None or v == "":
        return None
    return v.lower() in ("1", "true", "yes", "on")


def _when(qp, name: str, end: bool) -> int | None:
    v = qp.get(name)
    if v is None or v == "":
        return None
    if re.fullmatch(r"\d{9,}(\.\d+)?", v):  # epoch seconds
        return int(float(v) * query.NS)
    try:
        return query.parse_when(v, end=end)
    except ValueError as e:
        raise BadRequest(f"{name}: {e}") from None


def parse_query(qp) -> Query:
    q = Query()
    for name in ("min_points", "max_points"):
        setattr(q, name, _num(qp, name, int))
    for name in ("entropy_x_min", "entropy_x_max", "entropy_y_min", "entropy_y_max", "entropy_dc_min",
                 "entropy_dc_max", "distance_min", "distance_max", "aspect_min", "aspect_max",
                 "variation_x_min", "variation_x_max", "variation_y_min", "variation_y_max",
                 "duration_min", "duration_max"):
        setattr(q, name, _num(qp, name, float))
    if qp.get("bbox"):
        try:
            parts = [float(p) for p in qp["bbox"].split(",")]
            if len(parts) != 4:
                raise ValueError
        except ValueError:
            raise BadRequest("bbox: expected x0,y0,x1,y1") from None
        q.bbox = (min(parts[0], parts[2]), min(parts[1], parts[3]), max(parts[0], parts[2]), max(parts[1], parts[3]))
    q.bbox_mode = "intersects" if qp.get("bbox_mode") == "intersects" else "inside"
    q.since_ns, q.until_ns = _when(qp, "since", False), _when(qp, "until", True)
    for name in ("source", "machine", "app", "recording"):
        vals = [v for raw in qp.getlist(name) for v in raw.split(",") if v]
        if name == "source" and any(v not in SOURCES for v in vals):
            raise BadRequest(f"source: one of {SOURCES}")
        setattr(q, name, vals)
    q.has_color = _bool(qp, "has_color")
    if qp.get("ids"):
        try:
            q.ids = [int(v) for v in qp["ids"].split(",") if v]
        except ValueError:
            raise BadRequest("ids: comma separated integers") from None
    q.include_open = bool(_bool(qp, "include_open"))
    q.sort = qp.get("sort") or None
    if q.sort is not None and q.sort not in SORT_KEYS:
        raise BadRequest(f"sort: one of {', '.join(SORT_KEYS)}")
    q.order = "desc" if qp.get("order") == "desc" else "asc"
    q.seed = _num(qp, "seed", int)
    q.limit = _num(qp, "limit", int)
    q.limit = 200 if q.limit is None else max(0, q.limit)
    q.offset = max(0, _num(qp, "offset", int) or 0)
    return q


def _format(request: Request) -> str:
    fmt = request.query_params.get("format")
    if fmt is None:
        accept = request.headers.get("accept", "")
        fmt = "json" if "json" in accept and "msgpack" not in accept else "msgpack"
    if fmt not in MEDIA:
        raise BadRequest(f"format: one of {', '.join(MEDIA)}")
    return fmt


def create_app(state) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        if state.hub is not None:
            state.hub.start()
        yield
        if state.hub is not None:
            state.hub.stop()

    app = FastAPI(title="mouse-logger fountain", version=__version__, lifespan=lifespan)

    def index_or_503():
        idx = state.index
        if idx is None:
            raise HTTPException(status_code=503, detail={"ready": False, **state.progress})
        return idx

    def respond(request: Request, q: Query) -> Response:
        idx = index_or_503()
        qp = request.query_params
        fmt = _format(request)
        fields = qp.get("fields", "xyt")
        if fields not in FIELDS:
            raise BadRequest(f"fields: one of {', '.join(FIELDS)}")
        meta = _bool(qp, "meta") is not False
        resample = _num(qp, "resample", int)
        if resample is not None and resample < 2:
            raise BadRequest("resample: at least 2")
        t = time.perf_counter()
        rows, total = idx.select(q)
        g = idx.gather(rows, with_t="t" in fields, with_rgb="c" in fields, resample=resample)
        took = (time.perf_counter() - t) * 1000
        if fmt == "arrow":
            from .wire import encode_arrow
            body = encode_arrow(g, total, took, idx.dicts, meta)
        else:
            body = (wire.encode_msgpack if fmt == "msgpack" else wire.encode_json)(
                wire.document(g, total, took, idx.dicts, meta, binary=fmt == "msgpack"))
        return Response(content=body, media_type=MEDIA[fmt], headers={"X-Took-Ms": f"{took:.2f}", "X-Total": str(total)})

    @app.get("/health")
    def health():
        idx = state.index
        live = state.live_status()
        return {"ready": idx is not None, "version": __version__, "wire_version": WIRE_VERSION,
                "uptime_s": round(time.time() - state.started, 1), "startup_ms": state.startup_ms,
                "paths": len(idx) if idx is not None else 0, "chunks": len(idx.chunks) if idx is not None else 0,
                "cache_dir": str(state.opts.cache_dir), "live": live, **state.progress}

    @app.get("/stats")
    def stats():
        return JSONResponse(content=_clean({**index_or_503().stats(), "live": state.live_status()}))

    @app.get("/sources")
    def sources():
        idx = index_or_503()
        rows = []
        for c in idx.chunks:
            m = dict(c.meta)
            m.pop("params", None)
            rows.append({**m, "paths": len(c), "points": c.n_points})
        return rows

    @app.get("/paths/count")
    def count(request: Request):
        idx = index_or_503()
        q = parse_query(request.query_params)
        return {"total": int(idx.mask(q).sum())}

    @app.get("/paths/{path_id}")
    def one(path_id: int, request: Request):
        q = Query(ids=[path_id], include_open=True, limit=1)
        r = respond(request, q)
        if r.headers.get("X-Total") == "0":
            raise HTTPException(status_code=404, detail="no such path")
        return r

    @app.get("/paths")
    def paths(request: Request):
        return respond(request, parse_query(request.query_params))

    @app.post("/refresh")
    def refresh():
        state.request_rescan()
        return {"ok": True}

    @app.get("/live/position")
    def position():
        pos = state.hub.latest() if state.hub is not None else None
        if pos is None:
            return Response(status_code=204)
        return pos

    @app.websocket("/live")
    async def live(ws: WebSocket):
        if state.hub is None:
            await ws.close(code=1013, reason="live stream disabled")
            return
        await state.hub.serve(ws)

    return app


def _clean(o):
    """JSON cannot carry inf/nan; the stats use them for degenerate aspects."""
    import math
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o
