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
day. Completed days never change, so any sync tool handles them well.

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
local time (default: today until now). Also `--session N`, `--dark`, `--out`
(extension picks png, svg or pdf), `--width`, `--dpi`. The screen frame comes
from the monitor layout stored with the session.

## Layout

- `src/mouse_logger/hypr.py`: Hyprland IPC client.
- `src/mouse_logger/inputdev.py`: evdev discovery, hotplug rescan, readers.
- `src/mouse_logger/db.py`: schema and the batching writer thread.
- `src/mouse_logger/recorder.py`: poller, focus tracker, wiring, signals.
- `src/mouse_logger/service.py`: systemd units (recorder, export timer).
- `src/mouse_logger/dayfiles.py`: per-day export files and the archive import.
- `src/mouse_logger/query.py`: time-range parsing, loading into numpy.
- `src/mouse_logger/viz.py`: the three renderers.
- `src/mouse_logger/cli.py`: the `mouse-logger` entry point.
