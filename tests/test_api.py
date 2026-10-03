import msgpack
import numpy as np
import pytest
from fastapi.testclient import TestClient

from mouse_logger.fountain.api import create_app
from mouse_logger.fountain.serve import Options, State
from tests.conftest import T0


@pytest.fixture
def client(data_dir, tmp_path):
    st = State(Options(data_dir=data_dir, db=None, cache_dir=tmp_path / "cache", live=False, jobs=1))
    with TestClient(create_app(st)) as c:
        assert c.get("/paths").status_code == 503
        st.build_and_index()
        yield c


def test_health_stats_sources(client):
    h = client.get("/health").json()
    assert h["ready"] and h["paths"] == 3 and h["chunks"] == 1
    s = client.get("/stats").json()
    assert s["points"] == 37 and s["by_source"] == {"mouse_logger": 3} and s["top_apps"] == {"firefox": 2, "kitty": 1}
    src = client.get("/sources").json()
    assert len(src) == 1 and src[0]["day"] == "2026-01-01" and src[0]["paths"] == 3


def test_paths_msgpack_and_json(client):
    d = msgpack.unpackb(client.get("/paths").content, raw=False)
    assert d["n"] == 3 and d["m"] == 37
    off = np.frombuffer(d["offsets"], "<u4")
    assert off.tolist() == [0, 10, 31, 37]
    x = np.frombuffer(d["x"], "<f4")
    assert np.isclose(x[0], 100 / 2560)
    t = np.frombuffer(d["t"], "<i4")
    assert t[:3].tolist() == [0, 4, 8]
    assert np.frombuffer(d["t0_ns"], "<i8")[0] == T0
    assert d["dict"]["app"][np.frombuffer(d["app"], "<u2")[1]] == "firefox"
    assert np.frombuffer(d["metrics"]["n_points"], "<u4").tolist() == [10, 21, 6]
    j = client.get("/paths", params={"format": "json", "fields": "xy", "meta": 0}).json()
    assert j["n"] == 3 and len(j["x"]) == 37 and "t" not in j and "metrics" not in j
    assert client.get("/paths", headers={"Accept": "application/json"}).headers["content-type"].startswith("application/json")


def test_filters_sort_ids(client):
    assert client.get("/paths/count", params={"min_points": 10, "max_points": 10}).json() == {"total": 1}
    assert client.get("/paths/count", params={"app": "fire*"}).json() == {"total": 2}
    assert client.get("/paths/count", params={"since": "2026-01-01", "until": "2026-01-02"}).json()["total"] == 3
    assert client.get("/paths/count", params={"since": str(T0 // 10**9 + 1)}).json()["total"] == 0
    assert client.get("/paths/count", params={"bbox": "0,0,0.5,0.5"}).json()["total"] == 1
    assert client.get("/paths/count", params={"bbox": "0,0,0.5,0.5", "bbox_mode": "intersects"}).json()["total"] == 3
    assert client.get("/paths/count", params={"source": "legacy"}).json()["total"] == 0
    d = msgpack.unpackb(client.get("/paths", params={"sort": "n_points", "order": "desc"}).content, raw=False)
    assert np.frombuffer(d["metrics"]["n_points"], "<u4").tolist() == [21, 10, 6]
    ids = np.frombuffer(d["id"], "<i8")
    one = msgpack.unpackb(client.get(f"/paths/{ids[0]}").content, raw=False)
    assert one["n"] == 1 and one["m"] == 21
    assert client.get("/paths/123").status_code == 404
    d = msgpack.unpackb(client.get("/paths", params={"resample": 8, "limit": 2, "offset": 1}).content, raw=False)
    assert d["n"] == 2 and d["m"] == 16 and d["total"] == 3


def test_bad_requests(client):
    for params in ({"sort": "nope"}, {"bbox": "1,2"}, {"since": "garbage"}, {"format": "xml"},
                   {"fields": "abc"}, {"min_points": "x"}, {"source": "other"}, {"resample": 1}):
        assert client.get("/paths", params=params).status_code == 400, params


def test_arrow_format(client):
    import json

    import pyarrow as pa
    import pyarrow.ipc as ipc

    r = client.get("/paths", params={"format": "arrow", "fields": "xyt"})
    assert r.headers["content-type"].startswith("application/vnd.apache.arrow.stream")
    table = ipc.open_stream(r.content).read_all()
    assert table.num_rows == 3 and table.column("app").to_pylist() == ["kitty", "firefox", "firefox"]
    assert [len(v) for v in table.column("x").to_pylist()] == [10, 21, 6]
    assert table.column("n_points").to_pylist() == [10, 21, 6] and table.column("source").to_pylist()[0] == "mouse_logger"
    assert json.loads(table.schema.metadata[b"fountain"])["total"] == 3
    assert pa.types.is_int32(table.column("t").type.value_type)
