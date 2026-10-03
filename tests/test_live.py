import json
import socket
import time

import pytest
from fastapi.testclient import TestClient

from mouse_logger import live
from mouse_logger.fountain.api import create_app
from mouse_logger.fountain.livehub import Hub
from mouse_logger.fountain.serve import Options, State


def test_publisher_listener_roundtrip(tmp_path):
    path = tmp_path / "live.sock"
    pub = live.Publisher(path)
    pub.put("motion", (1, 2, 3, 4))  # nobody listens yet: dropped, no error
    assert pub.dropped == 1 and pub.sent == 0
    lis = live.Listener(path)
    pub.put("motion", (10, 11, 300, 400))
    pub.put("buttons", (12, 13, "mouse", 272, "BTN_LEFT", 1))
    time.sleep(0.01)
    assert lis.drain() == [("motion", (10, 11, 300, 400)), ("buttons", (12, 13, "mouse", 272, "BTN_LEFT", 1))]
    assert pub.sent == 2 and lis.received == 2
    lis.close()
    assert not path.exists()
    pub.put("motion", (1, 2, 3, 4))
    assert pub.dropped == 2
    pub.close()


def test_tee_survives_a_broken_publisher():
    class W:
        rows = []

        def put(self, table, row):
            self.rows.append((table, row))

    class Broken:
        def put(self, table, row):
            raise RuntimeError("boom")

    w = W()
    tee = live.Tee(w, Broken())
    tee.put("motion", (1, 2, 3, 4))
    assert w.rows == [("motion", (1, 2, 3, 4))]


def test_hub_websocket_and_position(tmp_path):
    sock = tmp_path / "live.sock"
    st = State(Options(data_dir=None, db=None, cache_dir=tmp_path / "cache", live=True, live_sock=sock, jobs=1))
    st.hub = Hub(sock, st)
    with TestClient(create_app(st)) as c:
        assert c.get("/live/position").status_code == 204
        pub = live.Publisher(sock)
        with c.websocket_connect("/live?hz=0&format=json&events=motion,button") as ws:
            time.sleep(0.05)
            pub.put("motion", (1_000, 1, 640, 360))
            pub.put("buttons", (1_001, 1, "mouse", 272, "BTN_LEFT", 1))
            pub.put("scroll", (1_002, 1, "mouse", "v", 1, 0))  # not subscribed
            pub.put("motion", (1_003, 1, 650, 370))
            got = [json.loads(ws.receive_text()) for _ in range(3)]
        assert [g["e"] for g in got] == ["motion", "button", "motion"]
        assert got[0]["px"] == 640 and got[1]["name"] == "BTN_LEFT" and got[2]["t"] == 1_003
        pos = c.get("/live/position").json()
        assert pos["px"] == 650 and pos["py"] == 370 and pos["t_ns"] == 1_003 and "age_ms" in pos
        with c.websocket_connect("/live?hz=50&format=msgpack") as ws:
            time.sleep(0.05)
            for i in range(10):  # coalesced: the newest sample per tick
                pub.put("motion", (2_000 + i, 1, i, i))
            import msgpack
            ev = msgpack.unpackb(ws.receive_bytes(), raw=False)
            assert ev["e"] == "motion" and ev["t"] == 2_009
        st_live = c.get("/health").json()["live"]
        assert st_live["events"] == 14 and st_live["connected_clients"] == 0
        pub.close()
