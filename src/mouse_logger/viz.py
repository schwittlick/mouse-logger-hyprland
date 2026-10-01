"""Render recordings to images. Needs the optional extra: uv sync --extra viz

Three views:
  path      the trajectory as thin strokes, clicks as markers
  heatmap   where the cursor spends its time
  activity  distance and active minutes per hour, clicks per focused app
"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import query
from .query import BUTTON_NAMES, NS, NoData, Range, fmt_time

# Brand-neutral reference palette (light and dark steps validated separately).
# series: click colours for left / right / middle, fixed order.
# seq: one-hue ramp; on the light surface it runs light->dark, on the dark
# surface dark->light so that "near zero" always recedes into the surface.
THEMES = {
    "light": dict(
        surface="#fcfcfb", ink="#0b0b0b", ink2="#52514e", muted="#898781",
        grid="#e1e0d9", axis="#c3c2b7",
        series=("#2a78d6", "#eb6834", "#1baf7a"),
        seq=("#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5",
             "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"),
    ),
    "dark": dict(
        surface="#1a1a19", ink="#ffffff", ink2="#c3c2b7", muted="#898781",
        grid="#2c2c2a", axis="#383835",
        series=("#3987e5", "#d95926", "#199e70"),
        seq=("#0d366b", "#104281", "#184f95", "#1c5cab", "#256abf", "#2a78d6", "#3987e5",
             "#5598e7", "#6da7ec", "#86b6ef", "#9ec5f4", "#b7d3f6", "#cde2fb"),
    ),
}

# (label, series slot or None for the gray "other" fold, marker shape).
# Hue and shape both carry identity, so the markers survive greyscale and CVD.
CLICK_STYLES = [("left", 0, "o"), ("right", 1, "^"), ("middle", 2, "s"), ("other", None, "D")]


@dataclass
class Options:
    db: Path
    rng: Range
    out: Path
    theme: str = "light"
    dpi: int = 150
    width: float = 12.0
    gap: float = 2.0          # seconds without movement that end a stroke
    stride: int = 1           # keep every Nth sample
    color: str = "ink"        # path: "ink" or "time"
    clicks: bool = True
    art: bool = False         # path: no chrome at all
    line_width: float = 0.5   # points
    cell: float = 8.0         # heatmap cell size, logical px
    top: int = 10             # activity: apps in the click chart


def _mpl():
    try:
        import matplotlib
    except ImportError:
        raise SystemExit("matplotlib is not installed. Run: uv sync --extra viz") from None
    matplotlib.use("agg")
    import matplotlib.pyplot as plt

    return matplotlib, plt


def _style(plt, th: dict) -> None:
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.size": 9,
        "text.color": th["ink"],
        "axes.titlecolor": th["ink"],
        "axes.titlesize": 10,
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
        "axes.labelcolor": th["muted"],
        "axes.labelsize": 8,
        "xtick.color": th["muted"],
        "ytick.color": th["muted"],
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "xtick.major.size": 0,
        "ytick.major.size": 0,
        "axes.edgecolor": th["axis"],
        "axes.linewidth": 0.6,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "grid.color": th["grid"],
        "grid.linewidth": 0.6,
        "grid.linestyle": "-",
        "figure.facecolor": th["surface"],
        "axes.facecolor": th["surface"],
        "savefig.facecolor": th["surface"],
        "legend.frameon": False,
        "legend.fontsize": 8,
        "legend.labelcolor": th["ink2"],
        "figure.titlesize": 12,
        "figure.titleweight": "bold",
    })


def _span(t0: int, t1: int) -> str:
    same_day = fmt_time(t0)[:10] == fmt_time(t1)[:10]
    return f"{fmt_time(t0)} → {fmt_time(t1, with_date=not same_day)}"


def _titles(ax, th, title: str, sub: str) -> None:
    ax.set_title(title, loc="left", pad=10)
    ax.set_title(sub, loc="right", pad=10, fontsize=8, fontweight="normal", color=th["muted"])


def _screen_axes(ax, th, rects, x0, y0, x1, y1) -> None:
    from matplotlib.patches import Rectangle

    for rx, ry, rw, rh in rects:
        ax.add_patch(Rectangle((rx, ry), rw, rh, fill=False, edgecolor=th["axis"], lw=0.6, zorder=0))
    ax.set_xlim(x0, x1)
    ax.set_ylim(y1, y0)  # screen coordinates: y grows downwards
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)


def _save(fig, o: Options, **kw) -> None:
    """Write to a temp name and rename, so a viewer never sees a half-written file."""
    tmp = o.out.with_name(o.out.stem + ".tmp" + o.out.suffix)
    fig.savefig(tmp, dpi=o.dpi, **kw)
    os.replace(tmp, o.out)


# --------------------------------------------------------------------------- path
def render_path(conn, o: Options) -> str:
    _, plt = _mpl()
    th = THEMES[o.theme]
    _style(plt, th)

    m = query.load_motion(conn, o.rng, o.stride)
    if len(m) < 2:
        raise NoData("no cursor movement in this range")
    rects = query.monitor_rects(conn, o.rng)
    x0, y0, x1, y1 = query.bounds(rects, m.x, m.y)
    w, h = x1 - x0, y1 - y0
    gap_ns = int(o.gap * NS)
    brk = m.breaks(gap_ns)
    travelled = float(m.step_lengths(gap_ns).sum())

    if o.art:
        fig = plt.figure(figsize=(o.width, o.width * h / w))
        ax = fig.add_axes((0, 0, 1, 1))
        ax.set_axis_off()
    else:
        fig, ax = plt.subplots(figsize=(o.width, o.width * h / w + 1.0), layout="constrained")

    if o.color == "time":
        from matplotlib.cm import ScalarMappable
        from matplotlib.collections import LineCollection
        from matplotlib.colors import LinearSegmentedColormap, Normalize

        cmap = LinearSegmentedColormap.from_list("seq", th["seq"])
        pts = np.column_stack([m.x, m.y])
        segs = np.stack([pts[:-1], pts[1:]], axis=1)
        keep = np.ones(len(segs), dtype=bool)
        keep[brk - 1] = False  # segments that would bridge an idle gap
        frac = (m.t - m.t[0]) / max(int(m.t[-1] - m.t[0]), 1)
        ax.add_collection(LineCollection(
            segs[keep], colors=cmap(frac[:-1][keep]), linewidths=o.line_width,
            capstyle="round", joinstyle="round",
        ))
        if not o.art:
            cb = fig.colorbar(ScalarMappable(norm=Normalize(0, 1), cmap=cmap), ax=ax,
                              orientation="horizontal", shrink=0.35, aspect=60, pad=0.01, anchor=(1.0, 1.0))
            cb.set_ticks([0, 1])
            cb.set_ticklabels([fmt_time(int(m.t[0]), with_date=False), fmt_time(int(m.t[-1]), with_date=False)])
            cb.outline.set_visible(False)
            cb.ax.tick_params(size=0, labelsize=7, colors=th["muted"])
    else:
        xs = np.insert(m.x, brk, np.nan)
        ys = np.insert(m.y, brk, np.nan)
        ax.plot(xs, ys, color=th["ink"] if o.art else th["ink2"], lw=o.line_width,
                solid_joinstyle="round", solid_capstyle="round")

    n_clicks = 0
    if o.clicks:
        ct, code = query.load_presses(conn, o.rng)
        n_clicks = len(ct)
        if n_clicks:
            # the cursor position at click time is the last sample before it
            idx = np.clip(np.searchsorted(m.t, ct, side="right") - 1, 0, len(m) - 1)
            cx, cy = m.x[idx], m.y[idx]
            names = np.array([BUTTON_NAMES.get(int(c), "other") for c in code])
            plain = o.art or o.color == "time"  # shapes only, no hue competing with the strokes
            for label, slot, marker in CLICK_STYLES:
                sel = names == label
                if not sel.any():
                    continue
                if plain:
                    color = th["ink"]
                else:
                    color = th["series"][slot] if slot is not None else th["muted"]
                ax.scatter(cx[sel], cy[sel], s=18, marker=marker, color=color, edgecolors=th["surface"],
                           linewidths=0.7, zorder=3, label=f"{label} ({int(sel.sum()):,})")

    if o.art:
        ax.set_xlim(x0, x1)
        ax.set_ylim(y1, y0)
        ax.set_aspect("equal")
        _save(fig, o, pad_inches=0)
    else:
        _screen_axes(ax, th, rects, x0, y0, x1, y1)
        sub = (f"{len(m):,} samples · {len(brk) + 1:,} strokes · {n_clicks:,} clicks · "
               f"{travelled / 1000:,.0f}k logical px travelled")
        _titles(ax, th, f"Cursor path · {_span(int(m.t[0]), int(m.t[-1]))}", sub)
        if n_clicks:
            ax.legend(loc="upper left", bbox_to_anchor=(0, 0), ncol=4, handletextpad=0.4, columnspacing=1.5,
                      borderaxespad=0.2)
        _save(fig, o)
    plt.close(fig)
    return f"{o.out}: path, {len(m):,} samples, {n_clicks:,} clicks, {_span(int(m.t[0]), int(m.t[-1]))}"


# ------------------------------------------------------------------------ heatmap
def render_heatmap(conn, o: Options) -> str:
    _, plt = _mpl()
    from matplotlib.colors import LinearSegmentedColormap, LogNorm
    from matplotlib.ticker import FuncFormatter

    th = THEMES[o.theme]
    _style(plt, th)

    m = query.load_motion(conn, o.rng, o.stride)
    if len(m) < 2:
        raise NoData("no cursor movement in this range")
    rects = query.monitor_rects(conn, o.rng)
    x0, y0, x1, y1 = query.bounds(rects, m.x, m.y)
    w, h = x1 - x0, y1 - y0
    nx, ny = max(1, int(np.ceil(w / o.cell))), max(1, int(np.ceil(h / o.cell)))
    hist, _, _ = np.histogram2d(m.x, m.y, bins=[nx, ny],
                                range=[[x0, x0 + nx * o.cell], [y0, y0 + ny * o.cell]])
    hist = hist.T  # rows are y
    cmap = LinearSegmentedColormap.from_list("seq", th["seq"])
    cmap.set_bad(th["surface"])  # empty cells recede into the surface

    fig, ax = plt.subplots(figsize=(o.width, o.width * h / w + 1.0), layout="constrained")
    im = ax.imshow(np.ma.masked_equal(hist, 0), origin="upper", cmap=cmap, interpolation="nearest",
                   extent=(x0, x0 + nx * o.cell, y0 + ny * o.cell, y0),
                   norm=LogNorm(vmin=1, vmax=max(float(hist.max()), 2.0)))
    _screen_axes(ax, th, rects, x0, y0, x1, y1)
    cb = fig.colorbar(im, ax=ax, orientation="horizontal", shrink=0.35, aspect=60, pad=0.01, anchor=(1.0, 1.0))
    cb.set_label(f"samples per {o.cell:g} px cell (log scale)", color=th["muted"], fontsize=7)
    cb.outline.set_visible(False)
    cb.ax.tick_params(size=0, labelsize=7, colors=th["muted"])
    cb.ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    cb.ax.xaxis.set_minor_formatter(FuncFormatter(lambda v, _: ""))
    busiest = int(hist.max())
    _titles(ax, th, f"Cursor heatmap · {_span(int(m.t[0]), int(m.t[-1]))}",
            f"{len(m):,} samples · {int((hist > 0).sum()):,} of {nx * ny:,} cells visited · busiest cell {busiest:,}")
    _save(fig, o)
    plt.close(fig)
    return f"{o.out}: heatmap, {len(m):,} samples, {_span(int(m.t[0]), int(m.t[-1]))}"


# ----------------------------------------------------------------------- activity
def _bar_width_hours(fig_width_in: float, dpi: int, n_bins: int) -> float:
    """Bar width as a fraction of an hour: 70% of the slot, but never wider than 24 px."""
    px_per_slot = fig_width_in * dpi * 0.9 / max(n_bins, 1)
    return min(0.7, 24.0 / px_per_slot)


def render_activity(conn, o: Options) -> str:
    _, plt = _mpl()
    import matplotlib.dates as mdates

    th = THEMES[o.theme]
    _style(plt, th)

    m = query.load_motion(conn, o.rng, o.stride)
    if len(m) < 2:
        raise NoData("no cursor movement in this range")
    gap_ns = int(o.gap * NS)
    edges = query.hour_edges(int(m.t[0]), int(m.t[-1]))
    dist, _ = np.histogram(m.t[:-1], bins=edges, weights=m.step_lengths(gap_ns))
    minutes = np.unique(m.t // (60 * NS)) * (60 * NS)  # minutes with at least one sample
    active, _ = np.histogram(minutes, bins=edges)
    ct, code = query.load_presses(conn, o.rng)
    ft, fa = query.load_focus(conn, o.rng)
    per_app = Counter(query.app_at(ft, fa, ct)).most_common(o.top)

    # naive local datetimes: matplotlib would otherwise re-render aware ones in UTC
    starts = [datetime.fromtimestamp(int(e) / NS) for e in edges[:-1]]
    centers = [s + timedelta(minutes=30) for s in starts]
    bar_w = timedelta(hours=_bar_width_hours(o.width, o.dpi, len(starts)))

    h_rows = max(1.4, 0.32 * len(per_app) + 0.8)  # inches for the clicks panel
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(o.width, 6.0 + h_rows), layout="constrained",
                                        height_ratios=[3.0, 3.0, h_rows])
    fig.suptitle(f"Mouse activity · {_span(int(m.t[0]), int(m.t[-1]))}", x=0.01, ha="left")

    for ax, values, title, unit in (
        (ax1, dist / 1000.0, "Distance travelled per hour", "thousand logical px"),
        (ax2, active.astype(float), "Active minutes per hour", "minutes with movement"),
    ):
        ax.bar(centers, values, width=bar_w, color=th["series"][0], linewidth=0)
        ax.set_title(title, loc="left")
        ax.set_ylabel(unit)
        ax.grid(axis="y")
        ax.set_axisbelow(True)
        ax.spines["left"].set_visible(False)
        ax.set_xlim(starts[0], starts[-1] + timedelta(hours=1))
        if ax is ax2:
            ax.set_ylim(0, 60)
        if values.max() > 0:
            i = int(values.argmax())  # direct-label the peak only
            ax.annotate(f"{values[i]:,.0f}", (centers[i], values[i]), xytext=(0, 3), textcoords="offset points",
                        ha="center", va="bottom", fontsize=8, color=th["ink2"])
        loc = mdates.AutoDateLocator(minticks=4, maxticks=12)
        ax.xaxis.set_major_locator(loc)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))

    ax3.set_title(f"Clicks per focused app (top {o.top})", loc="left")
    if per_app:
        labels = [a for a, _ in per_app][::-1]
        counts = [c for _, c in per_app][::-1]
        row_px = h_rows * o.dpi * 0.75 / len(labels)
        ax3.barh(labels, counts, height=min(0.6, 24.0 / row_px), color=th["series"][0], linewidth=0)
        ax3.set_ylim(-0.5, len(labels) - 0.5)
        ax3.set_xlim(0, max(counts) * 1.08)
        ax3.set_xlabel("clicks")
        ax3.grid(axis="x")
        ax3.set_axisbelow(True)
        ax3.spines["left"].set_visible(False)
        ax3.tick_params(axis="y", labelcolor=th["ink2"])
        j = int(np.argmax(counts))
        ax3.annotate(f"{counts[j]:,}", (counts[j], j), xytext=(4, 0), textcoords="offset points",
                     ha="left", va="center", fontsize=8, color=th["ink2"])
    else:
        ax3.text(0.5, 0.5, "no clicks recorded in this range", transform=ax3.transAxes,
                 ha="center", va="center", color=th["muted"])
        ax3.set_axis_off()

    _save(fig, o)
    plt.close(fig)
    return (f"{o.out}: activity, {dist.sum() / 1000:,.0f}k px travelled, {int(active.sum())} active minutes, "
            f"{len(ct):,} clicks, {_span(int(m.t[0]), int(m.t[-1]))}")


RENDERERS = {"path": render_path, "heatmap": render_heatmap, "activity": render_activity}


def run(view: str, o: Options, watch: float | None) -> int:
    render = RENDERERS[view]
    while True:
        try:
            with closing(query.connect(o.db)) as conn:
                print(render(conn, o), flush=True)
        except NoData as e:
            print(f"nothing to draw: {e}", file=sys.stderr, flush=True)
            if watch is None:
                return 1
        if watch is None:
            return 0
        try:
            time.sleep(watch)
        except KeyboardInterrupt:
            return 0
