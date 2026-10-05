from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .db import default_db_path
from .dayfiles import default_archive_path, default_data_dir


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mouse-logger", description="Passive mouse recorder for Hyprland.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="record until SIGTERM/Ctrl-C")
    r.add_argument("--db", type=Path, default=None, help=f"sqlite file (default {default_db_path()}, or $MOUSE_LOGGER_DB)")
    r.add_argument("--hz", type=int, default=250, help="cursor poll rate (default 250)")
    r.add_argument("--no-evdev", action="store_true", help="skip button/scroll capture from /dev/input")
    r.add_argument("--no-live", action="store_true", help="do not publish events to the live socket")
    r.add_argument("--live-sock", type=Path, default=None, help="unix datagram socket to publish to (default $XDG_RUNTIME_DIR/mouse_logger/live.sock)")

    i = sub.add_parser("install-service", help="install and start the systemd user service")
    i.add_argument("--db", type=Path, default=None)
    i.add_argument("--hz", type=int, default=250)
    i.add_argument("--data-dir", type=Path, default=None, help=f"where the daily export writes (default {default_data_dir()})")
    i.add_argument("--no-live", action="store_true", help="recorder without the live socket")
    i.add_argument("--fountain", action="store_true", help="also install the data fountain service (needs: uv sync --extra fountain)")
    i.add_argument("--port", type=int, default=7777, help="fountain port (default 7777)")
    i.add_argument("--legacy-dir", type=Path, default=None, help="fountain: legacy cursor-project recordings to serve too")

    sub.add_parser("uninstall-service", help="stop, disable and remove the systemd user service")

    e = sub.add_parser("export", help="write completed days as per-machine day files for syncing")
    e.add_argument("--db", type=Path, default=None, help="live sqlite file (default as for run)")
    e.add_argument("--dir", type=Path, default=None, help=f"data directory (default {default_data_dir()}, or $MOUSE_LOGGER_DATA)")
    e.add_argument("--today", action="store_true", help="also write today's partial day (replaced on each run)")
    e.add_argument("--force", action="store_true", help="rewrite day files that already exist")

    im = sub.add_parser("import", help="merge all machines' day files into one archive database")
    im.add_argument("--dir", type=Path, default=None, help=f"data directory (default {default_data_dir()})")
    im.add_argument("--db", type=Path, default=None, help=f"archive sqlite file (default {default_archive_path()})")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=None, help="sqlite file (default: the live database)")
    common.add_argument("--archive", action="store_true", help=f"read the merged archive {default_archive_path()} instead")
    common.add_argument("--since", default="today",
                        help="start: all, today, yesterday, 30m, 2h, 7d, 2026-09-30 or 2026-09-30 14:00 (default today)")
    common.add_argument("--until", default=None, help="end, same forms (default now)")
    common.add_argument("--session", type=int, default=None, help="only this session id")
    common.add_argument("--machine", default=None, metavar="HOSTNAME", help="only sessions recorded on this machine")
    common.add_argument("--app", action="append", default=None, metavar="APP_ID",
                        help="only what happened while this app-id was focused (glob, repeatable; "
                             "ids as listed by viz activity)")
    common.add_argument("--out", type=Path, default=None,
                        help="output image; the extension picks png, svg or pdf (default mouse_<view>.png)")
    common.add_argument("--dark", action="store_true", help="dark surface")
    common.add_argument("--dpi", type=int, default=150)
    common.add_argument("--width", type=float, default=12.0, help="figure width in inches (default 12)")
    common.add_argument("--gap", type=float, default=2.0,
                        help="seconds without movement that end a stroke with --split rest/both (default 2)")
    common.add_argument("--stride", type=int, default=1, help="use every Nth sample, for long ranges")
    common.add_argument("--watch", type=float, metavar="SECONDS", default=None,
                        help="re-render every N seconds until Ctrl-C")

    v = sub.add_parser("viz", help="render recordings to an image (needs: uv sync --extra viz)")
    vs = v.add_subparsers(dest="view", required=True)
    vp = vs.add_parser("path", parents=[common], help="the trajectory as strokes, clicks as markers")
    vp.add_argument("--color", choices=["ink", "time"], default="ink",
                    help="one colour, or a light-to-dark ramp from the start to the end of the range")
    vp.add_argument("--split", choices=["click", "rest", "both"], default="click",
                    help="what ends a stroke: a button press (default), a rest longer than --gap, or either")
    vp.add_argument("--double-click", type=float, default=0.3, metavar="SECONDS",
                    help="presses closer together than this count as one boundary (default 0.3)")
    vp.add_argument("--no-clicks", action="store_true", help="no click markers")
    vp.add_argument("--art", action="store_true", help="no title, frame or legend: just the drawing (good with .svg)")
    vp.add_argument("--line-width", type=float, default=0.5, help="stroke width in points (default 0.5)")
    vh = vs.add_parser("heatmap", parents=[common], help="where the cursor spends its time")
    vh.add_argument("--cell", type=float, default=8.0, help="cell size in logical px (default 8)")
    va = vs.add_parser("activity", parents=[common], help="distance and active minutes per hour, clicks per app")
    va.add_argument("--top", type=int, default=10, help="apps in the clicks chart (default 10)")

    sm = sub.add_parser("similar", help="draw a stroke, see the most and least similar recorded strokes "
                                        "(needs: uv sync --extra similar)")
    sm.add_argument("--db", type=Path, default=None, help="sqlite file (default: the live database)")
    sm.add_argument("--archive", action="store_true", help=f"read the merged archive {default_archive_path()} instead")
    sm.add_argument("--since", default="all", help="start, same forms as viz (default all)")
    sm.add_argument("--until", default=None, help="end (default now)")
    sm.add_argument("--session", type=int, default=None, help="only this session id")
    sm.add_argument("--machine", default=None, metavar="HOSTNAME", help="only sessions recorded on this machine")
    sm.add_argument("--double-click", type=float, default=0.3, metavar="SECONDS",
                    help="presses closer together than this count as one stroke boundary (default 0.3)")
    sm.add_argument("--min-points", type=int, default=4, help="skip strokes with fewer samples (default 4)")
    sm.add_argument("--points", type=int, default=64, help="resample every stroke to this many points (default 64)")
    sm.add_argument("-n", "--count", type=int, default=12, help="strokes shown in each grid (default 12)")

    fo = sub.add_parser("fountain", help="serve every recorded path over HTTP (needs: uv sync --extra fountain)")
    fs = fo.add_subparsers(dest="action", required=True)
    fcommon = argparse.ArgumentParser(add_help=False)
    fcommon.add_argument("--legacy-dir", type=Path, default=None,
                         help="directory of legacy cursor-project recordings (default $MOUSE_LOGGER_LEGACY)")
    fcommon.add_argument("--data-dir", type=Path, default=None, help=f"day files (default {default_data_dir()})")
    fcommon.add_argument("--cache-dir", type=Path, default=None,
                         help="chunk cache (default ~/.local/share/mouse_logger/fountain/v1 or $MOUSE_LOGGER_FOUNTAIN_CACHE)")
    fcommon.add_argument("--jobs", type=int, default=None, help="parallel builds (default: all cores)")
    fb = fs.add_parser("build", parents=[fcommon], help="build or refresh the chunk cache and exit")
    fb.add_argument("--force", action="store_true", help="rebuild chunks that are up to date")
    fv = fs.add_parser("serve", parents=[fcommon], help="build what is stale, then serve")
    fv.add_argument("--host", default="127.0.0.1")
    fv.add_argument("--port", type=int, default=7777)
    fv.add_argument("--db", type=Path, default=None, help=f"live sqlite for today (default {default_db_path()}); 'none' to skip")
    fv.add_argument("--no-live", action="store_true", help="no live cursor stream")
    fv.add_argument("--live-sock", type=Path, default=None, help="unix datagram socket the recorder publishes to")

    a = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if a.cmd == "run":
        from .recorder import run

        if a.hz < 1 or a.hz > 1000:
            p.error("--hz must be between 1 and 1000")
        from .live import default_live_sock

        live = None if a.no_live else (a.live_sock or default_live_sock())
        return run(a.db or default_db_path(), a.hz, use_evdev=not a.no_evdev, live_sock=live)
    if a.cmd == "install-service":
        from .service import install

        install(a.hz, a.db, a.data_dir, live=not a.no_live, fountain=a.fountain, port=a.port, legacy_dir=a.legacy_dir)
        return 0
    if a.cmd == "export":
        from .dayfiles import export_days

        try:
            written = export_days(a.db or default_db_path(), a.dir or default_data_dir(), a.today, a.force)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 1
        for path, n in written:
            print(f"wrote {path} ({n:,} rows)")
        if not written:
            print("nothing new to export")
        return 0
    if a.cmd == "import":
        from .dayfiles import import_days

        try:
            imported, skipped = import_days(a.dir or default_data_dir(), a.db or default_archive_path())
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 1
        for path, n in imported:
            print(f"imported {path} ({n:,} rows)")
        print(f"{len(imported)} file(s) imported, {skipped} already up to date, archive: {a.db or default_archive_path()}")
        return 0
    if a.cmd == "uninstall-service":
        from .service import uninstall

        uninstall()
        return 0
    if a.cmd == "viz":
        from .query import Range
        from .viz import Options, run as run_viz

        try:
            rng = Range.from_args(a.since, a.until, a.session, a.machine, a.app)
        except ValueError as e:
            p.error(str(e))
        o = Options(
            db=a.db or (default_archive_path() if a.archive else default_db_path()), rng=rng, out=a.out or Path(f"mouse_{a.view}.png"),
            theme="dark" if a.dark else "light", dpi=a.dpi, width=a.width, gap=a.gap, stride=a.stride,
            split=getattr(a, "split", "click"), double_click=getattr(a, "double_click", 0.3),
            color=getattr(a, "color", "ink"), clicks=not getattr(a, "no_clicks", False),
            art=getattr(a, "art", False), line_width=getattr(a, "line_width", 0.5),
            cell=getattr(a, "cell", 8.0), top=getattr(a, "top", 10),
        )
        return run_viz(a.view, o, a.watch)
    if a.cmd == "fountain":
        import os

        from .fountain import cache as fcache

        legacy = a.legacy_dir or (Path(os.environ["MOUSE_LOGGER_LEGACY"]).expanduser() if os.environ.get("MOUSE_LOGGER_LEGACY") else None)
        data_dir = a.data_dir or default_data_dir()
        cache_dir = a.cache_dir or fcache.default_cache_dir()
        if a.action == "build":
            inputs = fcache.find_inputs(legacy, data_dir, cache_dir)
            built, failed, current = fcache.build(inputs, cache_dir, a.jobs, a.force, log=print)
            print(f"{len(built)} built, {len(failed)} failed, {len(current)} up to date, {len(inputs)} inputs, cache: {cache_dir}")
            return 1 if failed else 0
        from .live import default_live_sock
        from .fountain.serve import Options as FountainOptions, serve as run_fountain

        db = None if str(a.db).lower() == "none" else (a.db or default_db_path())
        return run_fountain(FountainOptions(
            host=a.host, port=a.port, legacy_dir=legacy, data_dir=data_dir, db=db, cache_dir=cache_dir,
            live=not a.no_live, live_sock=a.live_sock or default_live_sock(), jobs=a.jobs,
        ))
    if a.cmd == "similar":
        from .query import Range
        from .similar import Options as SimilarOptions, run as run_similar

        try:
            rng = Range.from_args(a.since, a.until, a.session, a.machine)
        except ValueError as e:
            p.error(str(e))
        return run_similar(SimilarOptions(
            db=a.db or (default_archive_path() if a.archive else default_db_path()), rng=rng,
            double_click=a.double_click, min_points=a.min_points, points=a.points, count=a.count,
        ))
    return 2


if __name__ == "__main__":
    sys.exit(main())
