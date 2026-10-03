# mouse_logger: working context

Passive Hyprland cursor recorder (SQLite) plus the tools on top of its data.
`readme.md` is the user documentation and is kept current; this file holds what
a session needs beyond it.

## Commands

- `uv sync --all-extras --group dev` (extras: `viz`, `similar`, `fountain`); Python 3.14 only.
- `uv run pytest` (synthetic day-file fixture in `tests/conftest.py`).
- Services on this machine: `mouse-logger.service` (recorder, 250 Hz),
  `mouse-logger-export.timer` (daily day files into `~/mouse-data`),
  `mouse-logger-fountain.service` (HTTP on 127.0.0.1:7777). Use
  `systemctl --user status <unit>` and `journalctl --user -u <unit>`. A code
  change needs `systemctl --user restart <unit>`; the units run this checkout's venv.
- `uv run mouse-logger fountain build|serve --legacy-dir ~/dev/cursor/data/cursor-recordings`.

## Layout

See readme "Layout". The data fountain is `src/mouse_logger/fountain/`
(`metrics`, `cache`, `ingest_legacy`, `ingest_days` with the live tail, `store`,
`wire`, `livehub`, `api`, `serve`); `src/mouse_logger/live.py` is the
recorder-to-fountain datagram socket.

## The cursor project (`~/dev/cursor`)

- `~/dev/cursor/cursor`: Marcel's art project (github schwittlick/cursor, branch
  `develop`), the fountain's consumer. It carries no notes of its own; everything
  about it lives here.
- `~/dev/cursor/data/cursor-recordings`: 186 legacy recordings (2019 to 2025),
  immutable source of truth, read by `fountain/ingest_legacy.py`. Decided
  2026-10-03: never convert them into the recorder's format (lossy both ways);
  the fountain is the unification layer.
- `~/dev/cursor/data/compositions/compositionNNN/`: the compositions (not a git repo).

### Working in the cursor repo on this machine

- The package is not installed: run from `~/dev/cursor/cursor` with
  `PYTHONPATH=. .venv/bin/python <script>`. `.venv` (gitignored) has
  `requirements.txt` installed. `pyproject.toml` pins `requires-python == "3.14.0"`
  while the system has 3.14.7, which blocks `pip install -e .`; relax to `>=3.14` if wanted.
- Tests: `.venv/bin/python -m pytest cursor/tests/test_fountain.py`. The parity
  test needs the fountain serving on 127.0.0.1:7777 and skips otherwise.
- `config_local.ini` (gitignored) exists there with `data_dir = /home/marcel/dev/cursor/data`;
  exports go to `<data_dir>/experiments/<name>/{hpgl,jpg}`. `DataDirHandler().recordings()`
  expects `<data_dir>/recordings`, but the folder is `cursor-recordings`: use the fountain, not `Loader`.

### The client, `cursor/load/fountain.py`

- `Fountain().paths(min_points=50, entropy_min=(3.5, 3.5), ..., limit=N)` returns a
  `Collection`; keyword names mirror the Filter classes (`entropy_min=(a, b)` is
  `EntropyMinFilter(a, b)`, `entropy_dc=(lo, hi)` is `DirectionChangeEntropyFilter`).
  Also `count()`, `path(id)`, `recordings()`, `live(hz)`.
- Each returned `Path` keeps `properties["fountain"]` with id, source (`legacy` or
  `mouse_logger`), app, recording, screen_w/h, t0_ns and the metrics, computed with
  cursor's own formulas, so the Collection passes the same filters again.
- The old loaders (`cursor/load/loader.py`, `arrow_loader.py`, `algorithm/h5.py`)
  still exist; new compositions should use `Fountain`.

### Compositions

- One directory per composition, main file `cNNN.py`: plotter constants
  (`PlotterType`, `MinmaxMapping.maps`, `XYFactors.fac`, a padded `BoundingBox`),
  `run()` builds a `Collection`, then
  `ExportWrapper(collection, PLOTTER, PADDING_MM, "cNNN", collection.hash(), export_jpg_preview=True)`
  with `.fit()` and `.ex()`.
- `composition105/`: scaffold (2026-10-03) with a fountain `QUERY` and a
  placeholder `build()`. Still to do: Marcel will provide a text description of
  the geometric idea; fill README.md "Geometric idea" and implement `build()`,
  then set the plotter, pens and query to match.

## Invariants

- Only the recorder writes `mouse.db`; only `export` writes `~/mouse-data`, and
  day files are immutable. The fountain cache (`~/.local/share/mouse_logger/fountain/v1`)
  is derived and disposable; changing `PARAMS` in `fountain/cache.py` rebuilds every chunk.
- `fountain/metrics.py` replicates `cursor.path.Path` formulas exactly, computed
  on the stored float32 coordinates, so client-side recomputation matches the server.
- Served coordinates are normalised to the primary monitor (0..1), as the legacy recorder did.
- The recorder must never fail because of the fountain: `Tee` and `Publisher` swallow every error.

## Gotchas

- `cache.build()` uses a process pool; scripts fed through `python -` cannot
  spawn workers (forkserver re-imports `<stdin>`). Use a file or `jobs=1`.
- `pkill -f "fountain serve"` matches the invoking shell; use `pkill -f "[f]ountain serve"`.
- One legacy file, `1676293094.665719_random_monday`, is corrupt and served empty on purpose.
