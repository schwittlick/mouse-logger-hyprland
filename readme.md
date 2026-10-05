# mouse_logger

Passive recorder of cursor movement, mouse buttons, scroll wheel and the
focused window on Hyprland (Wayland). Runs as a systemd user service, writes
one SQLite database, and can render the recordings as drawings and charts.

Hyprland-specific by design: Wayland has no compositor-independent way to read
the cursor position or the focused window. Buttons and scroll come from evdev.

## Requirements

- Hyprland (tested with 0.56) started as a systemd user session.
- [uv](https://docs.astral.sh/uv/). It fetches Python 3.14 itself if the machine lacks it.
- A C compiler and kernel headers, because `evdev` builds a small extension.
  Arch: `base-devel`. Debian/Ubuntu: `build-essential linux-libc-dev`.
- Read access to `/dev/input`. systemd-logind grants it to the active seat
  user on most setups. If the log shows a permission warning instead, run
  `sudo usermod -aG input $USER` and log in again. Position and focus are
  recorded either way; only buttons and scroll need this.

## Set up on a new machine

```sh
git clone <your remote> ~/dev/mouse_logger     # or copy the folder
cd ~/dev/mouse_logger
uv sync                                        # core recorder
uv sync --extra viz                            # optional: matplotlib for rendering
uv run mouse-logger install-service            # recorder service plus the daily export timer
journalctl --user -u mouse-logger -n 20        # expect "session N started" and one "reading /dev/input/..." per mouse
```

The unit is bound to `graphical-session.target`, which uwsm activates. Check
with `systemctl --user is-active graphical-session.target`. If that says
`inactive`, start the service from Hyprland instead by adding this line to
`hyprland.conf`:

```
exec-once = systemctl --user start mouse-logger
```

Everyday operations:

```sh
systemctl --user status mouse-logger           # running? event counts are in the journal on stop
systemctl --user stop mouse-logger             # pause recording; start to resume
uv run mouse-logger run -v                     # run in the foreground instead, Ctrl-C to stop
uv run mouse-logger uninstall-service
```

The unit points at the venv inside this folder. If you move the folder, run
`install-service` again. Each machine keeps its own database; the hostname is
stored per session, so recordings can be told apart later.

## Other machines

The live database stays local; recordings travel as one file per machine and
day. Completed days never change, so any sync tool handles them well. Each
file also carries the focus state in force when its day began, so it can be
read on its own.

```sh
uv run mouse-logger export            # completed days -> ~/mouse-data/<hostname>/<YYYY-MM-DD>.sqlite
uv run mouse-logger export --today    # also today's partial day, replaced on every run
uv run mouse-logger import            # all machines' day files -> ~/.local/share/mouse_logger/archive.db
uv run mouse-logger viz path --archive --since all             # explore the merged archive
uv run mouse-logger viz path --archive --since all --machine lush
```

`install-service` also installs a timer that runs `export` once a day, so
`~/mouse-data` fills up on its own. Sync that directory between machines with
Syncthing, rsync or similar (`--dir` or `MOUSE_LOGGER_DATA` changes the
location; pass `--data-dir` to `install-service` for the timer). Run `import`
wherever you want to explore: it skips files it has already merged, replaces a
partial day when the complete file arrives, and keeps every machine's sessions
apart by hostname. Never sync the live database itself.

## What it records

| Signal | Source | Notes |
|---|---|---|
| Cursor position | Hyprland IPC, polled at 250 Hz | Stored only when it changed. Logical (scaled) global coordinates: a 3840x2160 screen at 1.5x spans 2560x1440. |
| Focused window | Hyprland event socket | Change log of app-id (window class) and title. |
| Buttons and scroll | evdev on `/dev/input` | Only pointer devices are opened, only mouse button codes and wheel axes are stored. Keyboard keys are never read. |

Poll rate and database path can be changed with `--hz` and `--db` on `run`
and `install-service`. CPU cost at 250 Hz is about 1% of one core while the
mouse moves, near zero when idle.

## Data

`~/.local/share/mouse_logger/mouse.db` (override with `--db` or
`MOUSE_LOGGER_DB`). SQLite in WAL mode, commits about once per second. For a
consistent copy while the service runs:

```sh
sqlite3 ~/.local/share/mouse_logger/mouse.db ".backup mouse-backup.db"
```

Every event row has `t_ns` (unix wall clock) and `mono_ns` (`CLOCK_MONOTONIC`,
for replay timing within one boot), both in nanoseconds.

- `sessions`: one row per logger start: hostname, Hyprland version, monitor
  layout as JSON, poll rate. `ended_ns` is NULL after a hard kill.
- `motion(t_ns, mono_ns, x, y)`
- `buttons(t_ns, mono_ns, device, code, name, pressed)`: `pressed` is 1 or 0,
  `name` like `BTN_LEFT`.
- `scroll(t_ns, mono_ns, device, axis, value, hires)`: `axis` is `v` or `h`.
  `hires=0` rows are whole notches (+1 = up or left), `hires=1` rows are the
  same events in 1/120 notch units.
- `focus(t_ns, mono_ns, address, app_id, title)`: valid until the next row.
  NULL address means nothing was focused.

Example: distance travelled today in logical pixels.

```sql
WITH m AS (
  SELECT x, y, LAG(x) OVER (ORDER BY t_ns) px, LAG(y) OVER (ORDER BY t_ns) py
  FROM motion WHERE t_ns > unixepoch('now', 'start of day') * 1000000000
)
SELECT SUM(SQRT((x-px)*(x-px) + (y-py)*(y-py))) FROM m;
```

## Visualize

Needs the `viz` extra (`uv sync --extra viz`).

```sh
uv run mouse-logger viz path                      # today's strokes with click markers -> mouse_path.png
uv run mouse-logger viz path --color time         # strokes shaded light to dark over time
uv run mouse-logger viz path --art --out day.svg  # no chrome, just the drawing (plotter-friendly)
uv run mouse-logger viz heatmap --since 7d        # where the cursor dwells
uv run mouse-logger viz activity --since yesterday --until today
uv run mouse-logger viz path --since all --app dota2 --out dota.png   # only strokes made while Dota 2 was focused
uv run mouse-logger viz path --since all --app cs2 --out cs2.png      # the same for Counter-Strike 2
uv run mouse-logger viz path --watch 5            # re-render every 5 s; open the png in a viewer that reloads
```

- `path`: one stroke per stretch between two button presses (`--split click`,
  the default; presses within `--double-click` 0.3 s count as one, scroll does
  not count). `--split rest` cuts instead where the cursor rested longer than
  `--gap` seconds, `--split both` does either. A logger restart always starts
  a new stroke. Clicks are markers that differ in colour and shape. Options:
  `--no-clicks`, `--line-width`, `--stride N` for long ranges.
- `heatmap`: samples per `--cell` logical px (default 8), log scale.
- `activity`: distance and active minutes per hour, clicks per focused app.

Shared options: `--since` and `--until` take `all`, `today`, `yesterday`,
ages like `30m`, `2h`, `7d`, `1w`, or `2026-09-30` / `2026-09-30 14:00` in
local time (default: today until now). `--app ID` keeps only what happened
while a window with that app-id was focused (a glob, repeatable: `--app dota2
--app cs2`); the ids are the ones `viz activity` lists. Strokes are cut where
the focus left the app, so nothing is drawn across the time spent elsewhere.
Also `--session N`, `--dark`, `--out` (extension picks png, svg or pdf),
`--width`, `--dpi`. The screen frame comes from the monitor layout stored with
the session.

## Find similar strokes

Needs the `similar` extra (`uv sync --extra similar`, pulls in PySide6).

```sh
uv run mouse-logger similar                       # all recorded strokes
uv run mouse-logger similar --since 7d -n 20      # last week, 20 tiles per grid
```

Opens a Qt window. Draw a stroke with the mouse on the canvas, which has the
shape of your monitor layout. The right side fills with the N recorded strokes
that are most like it and the N that are least like it, nearest first, with
the distance, time, duration, length and focused app under each. Every tile
overlays the recorded stroke (solid, dot at the start) on your query (faint),
both exactly as the metric saw them, so you can see what it matched on. Click
a tile to make that stroke the query.

Strokes are click-to-click, as in `viz path`. The toolbar changes what counts
as similar, and every change re-runs the search:

- **Metric**: `Shape (Procrustes)` is point-wise distance after normalisation,
  fast and strict. `Points, DTW` lets the two strokes run at different speeds
  along the way, `Points, Fréchet` penalises the single worst deviation,
  `Points, Hausdorff` ignores drawing order. `Turning angles` compares the
  direction change at each point and so captures wiggliness independent of
  rotation; `Headings` compares absolute segment directions. The DTW variants
  of both align the angle sequences elastically. Hover an entry for a one-line
  description.
- **Ignore position / size**: centre and scale every stroke before comparing
  (both on by default). Off, where on the screen and how big count.
- **Ignore rotation**: turn each candidate to fit the query best, least
  squares. **Ignore direction**: also try each candidate backwards.
- **Points**: resample count. **Min length**: drop short candidates (default 20 px), which
  otherwise dominate the "farthest" grid.

Distances are not comparable across metrics. The point-based DTW, Fréchet and
Hausdorff metrics are the slow ones, about a second per 5,000 strokes; bound
the range with `--since` on a big database.

## Data fountain

A local service that holds every recorded path in memory and answers filtered
queries in milliseconds, for compositions and other programs. It serves two
sources under one path model: the recorder's click-to-click strokes, and the
legacy recordings of the `cursor` project (`~/dev/cursor/data/cursor-recordings`).
Needs the `fountain` extra (`uv sync --extra fountain`).

```sh
uv run mouse-logger fountain build --legacy-dir ~/dev/cursor/data/cursor-recordings   # optional: fill the cache now (~20 s)
uv run mouse-logger fountain serve --legacy-dir ~/dev/cursor/data/cursor-recordings   # http://127.0.0.1:7777, docs at /docs
uv run mouse-logger install-service --fountain --legacy-dir ~/dev/cursor/data/cursor-recordings   # as a user service
curl -s 'localhost:7777/paths/count?min_points=50&max_points=100&entropy_x_min=3.5&entropy_y_min=3.5'
curl -s 'localhost:7777/paths?app=dota2&sort=distance&order=desc&limit=20&format=json' | jq .n
```

What it does on start: every input (one legacy JSON file, one day file per
machine) becomes one Feather chunk under `~/.local/share/mouse_logger/fountain/v1`,
rebuilt only when the input's size or mtime changes; then every chunk is
memory-mapped and a per-path index of metrics is built (about 0.4 s for 820k
paths). Today's strokes come straight from the live database and are refreshed
every second; the stroke being drawn right now is served only with
`include_open=1`. Inputs are rescanned every minute, or on `POST /refresh`.
Nothing under `~/mouse-data` or in `mouse.db` is ever written.

Paths are normalised to the primary monitor (0..1 is the screen that contains
logical (0, 0); other monitors spill outside), which is what the legacy
recorder did too. Per path the fountain keeps `screen_w/h` in logical px (0
when unknown), `t0_ns`, the focused `app`, `source`, `machine`, `recording`,
and metrics computed the way `cursor.path.Path` does so existing composition
thresholds keep their meaning: `n_points` (after dropping consecutive duplicate
points), `distance`, `duration_s`, the bounding box, `aspect` (h/w, ±inf for
lines), `entropy_x`, `entropy_y`, `entropy_dc` (direction-change entropy),
`variation_x`, `variation_y`.

`GET /paths` parameters: `min_points`, `max_points`, `entropy_x_min/max`,
`entropy_y_min/max`, `entropy_dc_min/max`, `distance_min/max`, `aspect_min/max`,
`variation_x/y_min/max`, `duration_min/max`, `bbox=x0,y0,x1,y1`
(`bbox_mode=intersects` instead of fully inside), `since`/`until` (same forms
as viz, or epoch seconds), `source=legacy|mouse_logger`, `machine`, `app`,
`recording` (globs allowed, repeatable), `has_color`, `ids`, `include_open`,
`sort=<metric>|t0|id|entropy_cross|random` with `order` and `seed`, `limit`
(default 200, 0 for all) and `offset`, `resample=N`, `fields=xy|xyt|xytc`,
`meta=0|1`, `format=msgpack|json`. The response is columnar: `offsets`, `x`,
`y`, `t` (ms since the path's `t0_ns`), optional `rgb`, per-path arrays and a
`metrics` map, as little-endian byte strings in msgpack or lists in JSON; path
k is points `[offsets[k], offsets[k+1])`. 10k paths are about 9 MB and 30 ms
as msgpack, 30 MB and 110 ms as JSON. Also `GET /paths/count`,
`GET /paths/{id}`, `GET /stats`, `GET /sources`, `GET /health`.

Live: the recorder publishes every event to a unix datagram socket
(`$XDG_RUNTIME_DIR/mouse_logger/live.sock`, off with `run --no-live`); the
fountain listens there and fans out over `WS /live?hz=60&events=motion,button,scroll,focus&format=msgpack|json`,
and `GET /live/position` returns the newest cursor sample. The socket is fire
and forget: without a fountain the recorder just drops the datagrams.

The cursor project talks to it through `cursor/load/fountain.py`:

```python
from cursor.load.fountain import Fountain
pc = Fountain().paths(min_points=50, max_points=100, entropy_min=(3.5, 3.5), limit=200)   # a Collection
n = Fountain().count(recording="1712393388.189338_saturday_sad", entropy_dc=(5, 10))
for ev in Fountain().live(hz=30):
    print(ev["x"], ev["y"])
```

Keyword names mirror the filter classes (`entropy_min=(3.5, 3.5)` is
`EntropyMinFilter(3.5, 3.5)`), each `Path` carries the server's metadata and
metrics in `properties["fountain"]`, and timestamps are float seconds.

## Layout

- `src/mouse_logger/hypr.py`: Hyprland IPC client.
- `src/mouse_logger/inputdev.py`: evdev discovery, hotplug rescan, readers.
- `src/mouse_logger/db.py`: schema and the batching writer thread.
- `src/mouse_logger/recorder.py`: poller, focus tracker, wiring, signals.
- `src/mouse_logger/service.py`: systemd units (recorder, export timer).
- `src/mouse_logger/dayfiles.py`: per-day export files and the archive import.
- `src/mouse_logger/query.py`: time-range parsing, loading into numpy.
- `src/mouse_logger/viz.py`: the three renderers.
- `src/mouse_logger/strokes.py`: click-to-click segmentation, resampling, the similarity metrics.
- `src/mouse_logger/similar.py`: the Qt window for drawing a stroke and browsing matches.
- `src/mouse_logger/live.py`: the recorder's fire-and-forget event publisher and the socket listener.
- `src/mouse_logger/fountain/`: the data fountain: `metrics` (cursor-compatible path metrics), `cache` (Feather chunks), `ingest_legacy` and `ingest_days` (inputs and the live tail), `store` (the index and queries), `wire` (msgpack/JSON), `livehub` (WebSocket fan-out), `api` and `serve`.
- `tests/`: `uv run pytest`.
- `src/mouse_logger/cli.py`: the `mouse-logger` entry point.
