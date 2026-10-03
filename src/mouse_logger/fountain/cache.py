"""Feather chunk files: one per immutable input, memory-mapped when read.

Layout under the cache dir (default ~/.local/share/mouse_logger/fountain/v1,
env MOUSE_LOGGER_FOUNTAIN_CACHE):

    manifest.json                         file numbers per input key, never reused
    legacy/<file stem>.feather            one per legacy cursor recording
    days/<machine>/<YYYY-MM-DD>.feather   one per day file in the data dir

A chunk is one Arrow IPC file, one row per path, list columns for the points,
uncompressed so that reading it is a zero-copy memory map. Its schema metadata
records the input it came from (size, mtime) and the build parameters, which
decides staleness. Nothing in the data dir or the live database is ever written.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.ipc as ipc

from .. import __version__
from . import CACHE_VERSION
from .metrics import NAMES as METRIC_NAMES

PARAMS = {"double_click": 0.3, "min_points": 2, "norm": "primary", "coords": "f32"}  # metrics are computed on the float32 coordinates that are stored
META_KEY = b"fountain"
LIVE_FILE_NO = 0x7FFFFFFF  # ids of strokes still in the live tail; never written to disk


def default_cache_dir() -> Path:
    env = os.environ.get("MOUSE_LOGGER_FOUNTAIN_CACHE")
    if env:
        return Path(env).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(data_home) / "mouse_logger" / "fountain" / f"v{CACHE_VERSION}"


# --------------------------------------------------------------------- chunk
@dataclass
class Chunk:
    """The paths of one input. Per-path arrays have one entry per path, point arrays are CSR by offsets."""

    meta: dict
    t0_ns: np.ndarray        # int64, unix ns of the first point
    offsets: np.ndarray      # int64, n_paths + 1
    app: np.ndarray          # object (str) per path
    screen_w: np.ndarray     # uint16 logical px, 0 when unknown
    screen_h: np.ndarray
    has_color: np.ndarray    # bool
    metrics: dict[str, np.ndarray]
    x: np.ndarray            # float32 normalised
    y: np.ndarray
    t: np.ndarray            # int32 ms since the path's t0_ns
    rgb: np.ndarray | None   # uint8, 3 per point, or None
    open: np.ndarray | None = None                    # bool per path, only for the live tail
    _keep: object = field(default=None, repr=False)  # the memory map behind the arrays

    def __len__(self) -> int:
        return len(self.t0_ns)

    @property
    def n_points(self) -> int:
        return len(self.x)

    @classmethod
    def empty(cls, meta: dict) -> "Chunk":
        z = np.zeros(0)
        return cls(meta, z.astype(np.int64), np.zeros(1, dtype=np.int64), np.zeros(0, dtype=object),
                   z.astype(np.uint16), z.astype(np.uint16), z.astype(bool),
                   {k: z.astype(np.uint32 if k == "n_points" else np.float64) for k in METRIC_NAMES},
                   z.astype(np.float32), z.astype(np.float32), z.astype(np.int32), None)


def make_meta(kind: str, source: str, machine: str, recording: str, day: str, complete: bool,
              input_path: Path | None, file_no: int) -> dict:
    st = input_path.stat() if input_path is not None else None
    return {
        "cache_version": CACHE_VERSION, "params": PARAMS, "logger_version": __version__,
        "kind": kind, "source": source, "machine": machine, "recording": recording, "day": day,
        "complete": bool(complete), "file_no": file_no,
        "input": str(input_path) if input_path else "", "input_size": st.st_size if st else 0,
        "input_mtime_ns": st.st_mtime_ns if st else 0, "built_ns": time.time_ns(),
    }


def write_chunk(path: Path, c: Chunk) -> None:
    offsets = c.offsets.astype(np.int32)

    def lists(values: np.ndarray, typ, per: int = 1) -> pa.ListArray:
        return pa.ListArray.from_arrays(pa.array(offsets * per, type=pa.int32()), pa.array(values, type=typ))

    cols = {
        "t0_ns": pa.array(c.t0_ns, type=pa.int64()),
        "n_points": pa.array(np.diff(c.offsets).astype(np.uint32), type=pa.uint32()),
        "app": pa.array([str(a) for a in c.app], type=pa.string()).dictionary_encode(),
        "screen_w": pa.array(c.screen_w, type=pa.uint16()),
        "screen_h": pa.array(c.screen_h, type=pa.uint16()),
        "has_color": pa.array(c.has_color, type=pa.bool_()),
        **{k: pa.array(c.metrics[k], type=pa.float64()) for k in METRIC_NAMES if k != "n_points"},
        "x": lists(c.x, pa.float32()),
        "y": lists(c.y, pa.float32()),
        "t": lists(c.t, pa.int32()),
    }
    if c.rgb is not None:
        cols["rgb"] = lists(c.rgb, pa.uint8(), per=3)
    table = pa.table(cols).replace_schema_metadata({META_KEY: json.dumps(c.meta).encode()})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with pa.OSFile(str(tmp), "wb") as sink:
        with ipc.new_file(sink, table.schema, options=ipc.IpcWriteOptions(compression=None)) as w:
            w.write_table(table)
    os.replace(tmp, path)


def read_meta(path: Path) -> dict | None:
    try:
        with pa.memory_map(str(path), "r") as mm:
            md = ipc.open_file(mm).schema.metadata or {}
        return json.loads(md[META_KEY]) if META_KEY in md else None
    except (OSError, pa.ArrowInvalid, KeyError, ValueError):
        return None


def read_chunk(path: Path) -> Chunk:
    mm = pa.memory_map(str(path), "r")
    table = ipc.open_file(mm).read_all()
    meta = json.loads((table.schema.metadata or {})[META_KEY])
    if table.num_rows == 0:
        return Chunk.empty(meta)

    def col(name: str) -> pa.Array:
        ca = table.column(name)
        # combine_chunks() would copy even a single chunk; we always write exactly one batch
        return ca.chunk(0) if ca.num_chunks == 1 else ca.combine_chunks()

    def prim(name: str, zero_copy: bool = True) -> np.ndarray:
        return col(name).to_numpy(zero_copy_only=zero_copy)

    def lst(name: str) -> np.ndarray:
        return col(name).values.to_numpy(zero_copy_only=True)  # .values is a view, .flatten() may copy

    app_col = col("app")
    app = np.array(app_col.dictionary.to_pylist(), dtype=object)[app_col.indices.to_numpy()]
    x_col = col("x")
    offsets = x_col.offsets.to_numpy(zero_copy_only=True).astype(np.int64)
    metrics = {"n_points": prim("n_points")}
    metrics.update({k: prim(k) for k in METRIC_NAMES if k != "n_points"})
    return Chunk(
        meta, prim("t0_ns"), offsets, app, prim("screen_w"), prim("screen_h"), prim("has_color", False), metrics,
        lst("x"), lst("y"), lst("t"), lst("rgb") if "rgb" in table.column_names else None, _keep=(mm, table),
    )


# -------------------------------------------------------------------- inputs
@dataclass(frozen=True)
class Input:
    key: str          # "legacy/<stem>" or "days/<machine>/<day>"
    kind: str         # "legacy" or "day"
    path: Path        # the input file
    out: Path         # its chunk


def find_inputs(legacy_dir: Path | None, data_dir: Path | None, cache_dir: Path) -> list[Input]:
    found: list[Input] = []
    if legacy_dir is not None and legacy_dir.is_dir():
        for f in sorted(legacy_dir.glob("*.json")):
            if f.is_file():
                found.append(Input(f"legacy/{f.stem}", "legacy", f, cache_dir / "legacy" / f"{f.stem}.feather"))
    if data_dir is not None and data_dir.is_dir():
        for mdir in sorted(p for p in data_dir.iterdir() if p.is_dir()):
            days: dict[str, Path] = {}
            for f in sorted(mdir.glob("*.sqlite")):
                day = f.name.removesuffix(".sqlite").removesuffix(".partial")
                if day not in days or not f.name.endswith(".partial.sqlite"):  # a complete file wins
                    days[day] = f
            for day, f in sorted(days.items()):
                found.append(Input(f"days/{mdir.name}/{day}", "day", f, cache_dir / "days" / mdir.name / f"{day}.feather"))
    return found


def is_current(inp: Input) -> bool:
    meta = read_meta(inp.out) if inp.out.exists() else None
    if meta is None:
        return False
    try:
        st = inp.path.stat()
    except OSError:
        return False
    return (meta.get("cache_version") == CACHE_VERSION and meta.get("params") == PARAMS
            and meta.get("input_size") == st.st_size and meta.get("input_mtime_ns") == st.st_mtime_ns)


class Manifest:
    """Append-only mapping of input key to file number, so ids stay stable across rebuilds."""

    def __init__(self, cache_dir: Path):
        self.path = cache_dir / "manifest.json"
        self.files: dict[str, int] = {}
        self.next_no = 1
        if self.path.exists():
            d = json.loads(self.path.read_text())
            self.files = dict(d.get("files", {}))
            self.next_no = int(d.get("next_no", max(self.files.values(), default=0) + 1))

    def file_no(self, key: str) -> int:
        if key not in self.files:
            self.files[key] = self.next_no
            self.next_no += 1
            self.save()
        return self.files[key]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"version": CACHE_VERSION, "next_no": self.next_no, "files": self.files}, indent=1))
        os.replace(tmp, self.path)


def _build_one(kind: str, in_path: str, out_path: str, file_no: int) -> tuple[int, int, str | None]:
    """Worker: build one chunk. Returns (paths, points, error).

    An input that cannot be read gets an empty chunk whose meta carries the
    error, so it counts as current and is not retried on every rescan;
    `fountain build --force` tries again.
    """
    if kind == "legacy":
        from .ingest_legacy import load
    else:
        from .ingest_days import load
    try:
        c = load(Path(in_path), file_no)
    except Exception as e:
        src = "legacy" if kind == "legacy" else "mouse_logger"
        c = Chunk.empty(make_meta(kind, src, "", Path(in_path).stem, "", True, Path(in_path), file_no))
        c.meta["error"] = f"{type(e).__name__}: {e}"[:500]
        write_chunk(Path(out_path), c)
        return 0, 0, c.meta["error"]
    write_chunk(Path(out_path), c)
    return len(c), c.n_points, None


def build(inputs: list[Input], cache_dir: Path, jobs: int | None = None, force: bool = False,
          log=None) -> tuple[list[Input], list[Input], list[Input]]:
    """Build every stale chunk, in parallel unless jobs is 1. Returns (built, failed, up to date)."""
    manifest = Manifest(cache_dir)
    stale = [i for i in inputs if force or not is_current(i)]
    current = [i for i in inputs if i not in stale]
    built: list[Input] = []
    failed: list[Input] = []

    def done(inp: Input, result) -> None:
        if isinstance(result, BaseException) or result[2] is not None:
            failed.append(inp)
            if log:
                log(f"failed {inp.key}: {result if isinstance(result, BaseException) else result[2]}")
            return
        built.append(inp)
        if log:
            log(f"wrote {inp.key}: {result[0]:,} paths, {result[1]:,} points")

    if not stale:
        return built, failed, current
    jobs = jobs or min(os.cpu_count() or 4, len(stale))
    if jobs <= 1:
        for i in stale:
            try:
                done(i, _build_one(i.kind, str(i.path), str(i.out), manifest.file_no(i.key)))
            except Exception as e:  # one bad input must not stop the rest
                done(i, e)
        return built, failed, current
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        futs = {pool.submit(_build_one, i.kind, str(i.path), str(i.out), manifest.file_no(i.key)): i for i in stale}
        for fut in as_completed(futs):
            try:
                done(futs[fut], fut.result())
            except Exception as e:
                done(futs[fut], e)
    return built, failed, current
