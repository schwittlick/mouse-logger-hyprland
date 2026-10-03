import json
import os
import time

import numpy as np

from mouse_logger.fountain import cache, ingest_days, ingest_legacy
from tests.conftest import T0, make_day_db


def test_day_ingest(day_db):
    c = ingest_days.load(day_db, 5)
    assert len(c) == 3 and c.meta["machine"] == "testbox" and c.meta["day"] == "2026-01-01" and c.meta["complete"]
    assert c.meta["file_no"] == 5 and c.meta["source"] == "mouse_logger" and c.meta["end_ns"] == T0 + 86400 * 10**9
    assert c.screen_w.tolist() == [2560] * 3 and c.screen_h.tolist() == [1440] * 3
    n = np.diff(c.offsets)
    assert n.tolist() == [10, 21, 6]
    assert np.isclose(c.x[0], 100 / 2560) and np.isclose(c.y[0], 100 / 1440)
    assert c.t[:3].tolist() == [0, 4, 8] and c.t0_ns[0] == T0
    assert c.app.tolist() == ["kitty", "firefox", "firefox"]
    assert c.metrics["n_points"].tolist() == [10, 21, 6]
    assert np.isclose(c.metrics["aspect"][0], 1.0 * 2560 / 1440)  # a diagonal in px is not one in normalised units


def test_roundtrip(day_db, tmp_path):
    c = ingest_days.load(day_db, 5)
    out = tmp_path / "x.feather"
    cache.write_chunk(out, c)
    r = cache.read_chunk(out)
    for name in ("t0_ns", "offsets", "screen_w", "screen_h", "has_color", "x", "y", "t"):
        assert np.array_equal(getattr(c, name), getattr(r, name)), name
    assert r.app.tolist() == c.app.tolist() and r.rgb is None and r.meta == c.meta
    for k in c.metrics:
        assert np.array_equal(c.metrics[k], r.metrics[k], equal_nan=True), k
    assert cache.read_meta(out)["input_size"] == day_db.stat().st_size


def test_empty_chunk_roundtrip(tmp_path):
    c = cache.Chunk.empty(cache.make_meta("day", "mouse_logger", "m", "m/d", "d", True, None, 3))
    out = tmp_path / "empty.feather"
    cache.write_chunk(out, c)
    r = cache.read_chunk(out)
    assert len(r) == 0 and r.n_points == 0 and r.meta["file_no"] == 3


def test_legacy_ingest(tmp_path):
    import ast, base64, zlib
    inner = {"mouse": {"paths": [
        [{"x": 0.1, "y": 0.1, "ts": 1700000000.0}, {"x": 0.1, "y": 0.1, "ts": 1700000000.0},  # duplicate
         {"x": 0.2, "y": 0.3, "ts": 1700000000.5, "c": [1, 2, 3]}],
        [{"x": 0.5, "y": 0.5, "ts": 1700000001}],                                             # one point: dropped
        [{"x": 0.0, "y": 0.0, "ts": 1700000002}, {"x": 1.0, "y": 1.0, "ts": 1700000005}],
    ], "timestamp": 1700000000.0}, "keys": []}
    f = tmp_path / "1700000000.0_test.json"
    f.write_text(str({ingest_legacy.ZIP_KEY: base64.b64encode(zlib.compress(json.dumps(inner).encode())).decode()}))
    c = ingest_legacy.load(f, 9)
    assert len(c) == 2 and np.diff(c.offsets).tolist() == [2, 2]
    assert c.has_color.tolist() == [True, False] and c.rgb is not None and c.rgb[3:6].tolist() == [1, 2, 3]
    assert c.t.tolist() == [0, 500, 0, 3000] and c.t0_ns[0] == 1700000000 * 10**9
    assert c.metrics["duration_s"].tolist() == [0.5, 3.0] and c.meta["recording"] == "1700000000.0_test"
    # str(dict) instead of JSON inside the zip
    g = tmp_path / "1700000001.0_literal.json"
    g.write_text(str({ingest_legacy.ZIP_KEY: base64.b64encode(zlib.compress(str(inner).encode())).decode()}))
    assert len(ingest_legacy.load(g, 10)) == 2


def test_inputs_staleness_and_build(data_dir, tmp_path):
    cache_dir = tmp_path / "cache"
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "1700000000.0_bad.json").write_text("not a recording")
    inputs = cache.find_inputs(legacy, data_dir, cache_dir)
    assert [i.key for i in inputs] == ["legacy/1700000000.0_bad", "days/testbox/2026-01-01"]
    assert not any(cache.is_current(i) for i in inputs)
    built, failed, current = cache.build(inputs, cache_dir, jobs=1, log=lambda s: None)
    assert [i.key for i in built] == ["days/testbox/2026-01-01"] and [i.key for i in failed] == ["legacy/1700000000.0_bad"]
    # a broken input gets an empty chunk that remembers the error, so it is not retried on every rescan
    assert cache.is_current(inputs[1]) and cache.is_current(inputs[0])
    assert "error" in cache.read_meta(inputs[0].out) and len(cache.read_chunk(inputs[0].out)) == 0
    built, failed, current = cache.build(inputs, cache_dir, jobs=1)
    assert built == [] and failed == [] and len(current) == 2
    built, failed, current = cache.build(inputs[:1], cache_dir, jobs=1, force=True)
    assert len(failed) == 1
    # touching the input makes the chunk stale, file numbers stay
    m = cache.Manifest(cache_dir)
    no = m.file_no("days/testbox/2026-01-01")
    time.sleep(0.01)
    os.utime(inputs[1].path)
    assert not cache.is_current(inputs[1])
    cache.build(inputs, cache_dir, jobs=1)
    assert cache.Manifest(cache_dir).file_no("days/testbox/2026-01-01") == no
    assert cache.read_chunk(inputs[1].out).meta["file_no"] == no
    # a partial day loses to a complete one
    make_day_db(data_dir / "testbox" / "2026-01-02.partial.sqlite", day="2026-01-02", complete=False)
    keys = [i.key for i in cache.find_inputs(None, data_dir, cache_dir)]
    assert keys == ["days/testbox/2026-01-01", "days/testbox/2026-01-02"]
    make_day_db(data_dir / "testbox" / "2026-01-02.sqlite", day="2026-01-02")
    paths = [i.path.name for i in cache.find_inputs(None, data_dir, cache_dir)]
    assert paths == ["2026-01-01.sqlite", "2026-01-02.sqlite"]
