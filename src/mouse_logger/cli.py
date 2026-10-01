from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .db import default_db_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mouse-logger", description="Passive mouse recorder for Hyprland.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="record until SIGTERM/Ctrl-C")
    r.add_argument("--db", type=Path, default=None, help=f"sqlite file (default {default_db_path()}, or $MOUSE_LOGGER_DB)")
    r.add_argument("--hz", type=int, default=250, help="cursor poll rate (default 250)")
    r.add_argument("--no-evdev", action="store_true", help="skip button/scroll capture from /dev/input")

    i = sub.add_parser("install-service", help="install and start the systemd user service")
    i.add_argument("--db", type=Path, default=None)
    i.add_argument("--hz", type=int, default=250)

    sub.add_parser("uninstall-service", help="stop, disable and remove the systemd user service")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=None, help="sqlite file (default as for run)")
    common.add_argument("--since", default="today",
                        help="start: all, today, yesterday, 30m, 2h, 7d, 2026-09-30 or 2026-09-30 14:00 (default today)")
    common.add_argument("--until", default=None, help="end, same forms (default now)")
    common.add_argument("--session", type=int, default=None, help="only this session id")
    common.add_argument("--out", type=Path, default=None,
                        help="output image; the extension picks png, svg or pdf (default mouse_<view>.png)")
    common.add_argument("--dark", action="store_true", help="dark surface")
    common.add_argument("--dpi", type=int, default=150)
    common.add_argument("--width", type=float, default=12.0, help="figure width in inches (default 12)")
    common.add_argument("--gap", type=float, default=2.0, help="seconds without movement that end a stroke (default 2)")
    common.add_argument("--stride", type=int, default=1, help="use every Nth sample, for long ranges")
    common.add_argument("--watch", type=float, metavar="SECONDS", default=None,
                        help="re-render every N seconds until Ctrl-C")

    v = sub.add_parser("viz", help="render recordings to an image (needs: uv sync --extra viz)")
    vs = v.add_subparsers(dest="view", required=True)
    vp = vs.add_parser("path", parents=[common], help="the trajectory as strokes, clicks as markers")
    vp.add_argument("--color", choices=["ink", "time"], default="ink",
                    help="one colour, or a light-to-dark ramp from the start to the end of the range")
    vp.add_argument("--no-clicks", action="store_true", help="strokes only")
    vp.add_argument("--art", action="store_true", help="no title, frame or legend: just the drawing (good with .svg)")
    vp.add_argument("--line-width", type=float, default=0.5, help="stroke width in points (default 0.5)")
    vh = vs.add_parser("heatmap", parents=[common], help="where the cursor spends its time")
    vh.add_argument("--cell", type=float, default=8.0, help="cell size in logical px (default 8)")
    va = vs.add_parser("activity", parents=[common], help="distance and active minutes per hour, clicks per app")
    va.add_argument("--top", type=int, default=10, help="apps in the clicks chart (default 10)")

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
        return run(a.db or default_db_path(), a.hz, use_evdev=not a.no_evdev)
    if a.cmd == "install-service":
        from .service import install

        install(a.hz, a.db)
        return 0
    if a.cmd == "uninstall-service":
        from .service import uninstall

        uninstall()
        return 0
    if a.cmd == "viz":
        from .query import Range
        from .viz import Options, run as run_viz

        try:
            rng = Range.from_args(a.since, a.until, a.session)
        except ValueError as e:
            p.error(str(e))
        o = Options(
            db=a.db or default_db_path(), rng=rng, out=a.out or Path(f"mouse_{a.view}.png"),
            theme="dark" if a.dark else "light", dpi=a.dpi, width=a.width, gap=a.gap, stride=a.stride,
            color=getattr(a, "color", "ink"), clicks=not getattr(a, "no_clicks", False),
            art=getattr(a, "art", False), line_width=getattr(a, "line_width", 0.5),
            cell=getattr(a, "cell", 8.0), top=getattr(a, "top", 10),
        )
        return run_viz(a.view, o, a.watch)
    return 2


if __name__ == "__main__":
    sys.exit(main())
