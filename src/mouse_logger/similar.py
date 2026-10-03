"""Draw a stroke with the mouse, see the recorded strokes that look most and
least like it. Needs the optional extra: uv sync --extra similar

The window: a canvas on the left shaped like the monitor layout, two grids on
the right with the nearest and farthest strokes. Each result tile overlays the
candidate (solid) on the query (faint), both exactly as the metric saw them,
with a dot at the start. Clicking a tile makes that stroke the query.
"""

from __future__ import annotations

import sys
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt, Signal
except ImportError:  # pragma: no cover
    raise SystemExit("PySide6 is not installed. Run: uv sync --extra similar") from None

from . import query, strokes
from .query import NS, NoData, Range, fmt_time
from .strokes import METRICS, Invariance, Result, StrokeSet


@dataclass
class Options:
    db: Path
    rng: Range
    double_click: float = 0.3   # presses closer than this are one stroke boundary
    min_points: int = 4         # strokes with fewer samples are not candidates
    points: int = 64            # resample count, changeable in the window
    count: int = 12             # tiles per grid, changeable in the window


THUMB = 150     # px, drawing area of a result tile
CAPTION = 40    # px, text under it


def _pen(color: QtGui.QColor, width: float) -> QtGui.QPen:
    pen = QtGui.QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _poly(pts: np.ndarray) -> QtGui.QPolygonF:
    return QtGui.QPolygonF([QtCore.QPointF(float(x), float(y)) for x, y in pts])


def _fit(pts: np.ndarray, box: QtCore.QRectF) -> np.ndarray:
    """Map points into box, keeping the aspect ratio and centring."""
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    span = np.maximum(hi - lo, 1e-9)
    s = min(box.width() / span[0], box.height() / span[1])
    off = np.array([box.center().x(), box.center().y()]) - (lo + hi) / 2 * s
    return pts * s + off


def _dim(c: QtGui.QColor, alpha: int) -> QtGui.QColor:
    c = QtGui.QColor(c)
    c.setAlpha(alpha)
    return c


# -------------------------------------------------------------------- canvas
class Canvas(QtWidgets.QWidget):
    """Where the query is drawn. Shows the monitors so position-aware searches make sense."""

    strokeDrawn = Signal(object)

    def __init__(self, rects, bounds):
        super().__init__()
        self.rects = rects
        self.bounds = bounds  # logical x0, y0, x1, y1
        self.query: np.ndarray | None = None
        self._live: list[tuple[float, float]] | None = None
        self.setMinimumSize(360, 240)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _transform(self) -> tuple[float, float, float]:
        """widget = logical * s + (ox, oy)"""
        x0, y0, x1, y1 = self.bounds
        w, h, m = x1 - x0, y1 - y0, 16
        s = min((self.width() - 2 * m) / w, (self.height() - 2 * m) / h)
        return s, (self.width() - w * s) / 2 - x0 * s, (self.height() - h * s) / 2 - y0 * s

    def to_logical(self, p: QtCore.QPointF) -> tuple[float, float]:
        s, ox, oy = self._transform()
        return (p.x() - ox) / s, (p.y() - oy) / s

    def to_widget(self, pts: np.ndarray) -> np.ndarray:
        s, ox, oy = self._transform()
        return pts * s + np.array([ox, oy])

    def set_query(self, pts: np.ndarray | None) -> None:
        self.query = pts
        self.update()

    def mousePressEvent(self, e: QtGui.QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._live = [self.to_logical(e.position())]
            self.update()

    def mouseMoveEvent(self, e: QtGui.QMouseEvent) -> None:
        if self._live is not None:
            self._live.append(self.to_logical(e.position()))
            self.update()

    def mouseReleaseEvent(self, e: QtGui.QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self._live is not None:
            pts = np.array(self._live)
            self._live = None
            if len(pts) >= 2 and np.any(pts != pts[0]):
                self.query = pts
                self.strokeDrawn.emit(pts)
            self.update()

    def paintEvent(self, _e) -> None:
        pal = self.palette()
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), pal.color(QtGui.QPalette.ColorRole.Base))
        p.setPen(_pen(pal.color(QtGui.QPalette.ColorRole.Mid), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for rx, ry, rw, rh in self.rects or [(self.bounds[0], self.bounds[1],
                                              self.bounds[2] - self.bounds[0], self.bounds[3] - self.bounds[1])]:
            a = self.to_widget(np.array([[rx, ry]]))[0]
            b = self.to_widget(np.array([[rx + rw, ry + rh]]))[0]
            p.drawRect(QtCore.QRectF(a[0], a[1], b[0] - a[0], b[1] - a[1]))

        pts = np.array(self._live) if self._live is not None else self.query
        if pts is None or len(pts) < 2:
            p.setPen(pal.color(QtGui.QPalette.ColorRole.PlaceholderText))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Draw a stroke here")
            return
        w = self.to_widget(pts)
        hi = pal.color(QtGui.QPalette.ColorRole.Highlight)
        p.setPen(_pen(hi, 2.5))
        p.drawPolyline(_poly(w))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(hi)
        p.drawEllipse(QtCore.QPointF(*w[0]), 5, 5)
        p.setBrush(pal.color(QtGui.QPalette.ColorRole.Base))
        p.setPen(_pen(hi, 2))
        p.drawEllipse(QtCore.QPointF(*w[-1]), 4, 4)


# ---------------------------------------------------------------------- tile
class Tile(QtWidgets.QWidget):
    """One result: candidate over query as the metric saw them, where on screen it was, a caption."""

    picked = Signal(int)

    def __init__(self, index: int, cand: np.ndarray, q: np.ndarray, dist: float,
                 line1: str, line2: str, where: tuple[float, float], screen_aspect: float, tip: str):
        super().__init__()
        self.index, self.cand, self.q, self.dist = index, cand, q, dist
        self.line1, self.line2, self.where, self.aspect = line1, line2, where, screen_aspect
        self._hover = False
        self.setFixedSize(THUMB, THUMB + CAPTION)
        self.setToolTip(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, _e) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, _e) -> None:
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, e: QtGui.QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.picked.emit(self.index)

    def paintEvent(self, _e) -> None:
        pal = self.palette()
        ink = pal.color(QtGui.QPalette.ColorRole.Text)
        mid = pal.color(QtGui.QPalette.ColorRole.Mid)
        muted = pal.color(QtGui.QPalette.ColorRole.PlaceholderText)
        hi = pal.color(QtGui.QPalette.ColorRole.Highlight)
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(pal.color(QtGui.QPalette.ColorRole.AlternateBase if self._hover else QtGui.QPalette.ColorRole.Base))
        p.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)

        # the two strokes share one frame so their alignment is visible
        box = QtCore.QRectF(12, 12, THUMB - 24, THUMB - 24)
        both = _fit(np.concatenate([self.cand, self.q]), box)
        c, q = both[:len(self.cand)], both[len(self.cand):]
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(_pen(_dim(hi, 110), 1.5))
        p.drawPolyline(_poly(q))
        p.setPen(_pen(ink, 2))
        p.drawPolyline(_poly(c))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawEllipse(QtCore.QPointF(*c[0]), 3.2, 3.2)

        # where on the screen the stroke happened
        mw = 26.0
        mh = mw * self.aspect
        mm = QtCore.QRectF(THUMB - 6 - mw, 6, mw, mh)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(_pen(mid, 1))
        p.drawRect(mm)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(hi)
        p.drawEllipse(QtCore.QPointF(mm.left() + self.where[0] * mw, mm.top() + self.where[1] * mh), 2.2, 2.2)

        f = p.font()
        fm = QtGui.QFontMetrics(f)
        y1 = THUMB + 2
        bold = QtGui.QFont(f)
        bold.setBold(True)
        p.setFont(bold)
        p.setPen(ink)
        p.drawText(QtCore.QRectF(8, y1, THUMB - 16, fm.height()), Qt.AlignmentFlag.AlignLeft, self.line1)
        p.setFont(f)
        p.setPen(muted)
        small = QtGui.QFont(f)
        small.setPointSizeF(max(f.pointSizeF() * 0.85, 6.0))
        p.setFont(small)
        sm = QtGui.QFontMetrics(small)
        p.drawText(QtCore.QRectF(8, y1 + fm.height(), THUMB - 16, sm.height()), Qt.AlignmentFlag.AlignLeft,
                   sm.elidedText(self.line2, Qt.TextElideMode.ElideRight, THUMB - 16))


# --------------------------------------------------------------- flow layout
class FlowLayout(QtWidgets.QLayout):
    """Left to right, wrapping, like text."""

    def __init__(self, spacing: int = 8):
        super().__init__()
        self._items: list[QtWidgets.QLayoutItem] = []
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        return self._place(QtCore.QRect(0, 0, w, 0), dry=True)

    def setGeometry(self, r: QtCore.QRect) -> None:
        super().setGeometry(r)
        self._place(r, dry=False)

    def sizeHint(self) -> QtCore.QSize:
        return self.minimumSize()

    def minimumSize(self) -> QtCore.QSize:
        s = QtCore.QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _place(self, rect: QtCore.QRect, dry: bool) -> int:
        x, y, row_h = rect.x(), rect.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and row_h > 0:
                x, y, row_h = rect.x(), y + row_h + self._sp, 0
            if not dry:
                it.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
            x += hint.width() + self._sp
            row_h = max(row_h, hint.height())
        return y + row_h - rect.y()

    def clear(self) -> None:
        while self._items:
            w = self.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()


# -------------------------------------------------------------------- window
class _Bridge(QtCore.QObject):
    done = Signal(int, object, object, float)  # generation, Result, candidate indices, seconds


class Window(QtWidgets.QMainWindow):
    def __init__(self, o: Options, ss: StrokeSet, rects: list):
        super().__init__()
        self.o, self.ss = o, ss
        self.bounds = query.bounds(rects, ss.m.x, ss.m.y)
        self._resampled: dict[int, np.ndarray] = {}
        cx = np.concatenate([[0.0], np.cumsum(ss.m.x)])
        cy = np.concatenate([[0.0], np.cumsum(ss.m.y)])
        n = ss.stop - ss.start
        self.centroid = np.column_stack([(cx[ss.stop] - cx[ss.start]) / n, (cy[ss.stop] - cy[ss.start]) / n])
        self._gen = 0
        self.pool = QtCore.QThreadPool()
        self.pool.setMaxThreadCount(1)
        self.bridge = _Bridge()
        self.bridge.done.connect(self._show)

        self.setWindowTitle("mouse-logger · similar strokes")
        self._build_toolbar()

        self.canvas = Canvas(rects, self.bounds)
        self.canvas.strokeDrawn.connect(lambda _pts: self.refresh())

        self.about = QtWidgets.QLabel()
        self.about.setWordWrap(True)
        self.about.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        self.near_label, self.near_flow = self._section()
        self.far_label, self.far_flow = self._section()
        right = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(right)
        col.setContentsMargins(12, 8, 12, 12)
        col.setSpacing(8)
        col.addWidget(self.about)
        col.addWidget(self.near_label)
        col.addWidget(self.near_flow)
        col.addSpacing(12)
        col.addWidget(self.far_label)
        col.addWidget(self.far_flow)
        col.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(right)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        split = QtWidgets.QSplitter()
        split.addWidget(self.canvas)
        split.addWidget(scroll)
        scroll.setMinimumWidth(3 * (THUMB + 8) + 24 + scroll.verticalScrollBar().sizeHint().width())
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([600, 900])
        self.setCentralWidget(split)

        t0, t1 = int(ss.t0.min()), int(ss.t1.max())
        self._base_status = f"{len(ss):,} strokes · {fmt_time(t0)} → {fmt_time(t1)}"
        self.statusBar().showMessage(self._base_status + " · draw a stroke on the left")
        self._describe()

    # ---- ui pieces
    def _section(self) -> tuple[QtWidgets.QLabel, QtWidgets.QWidget]:
        label = QtWidgets.QLabel()
        f = label.font()
        f.setBold(True)
        label.setFont(f)
        host = QtWidgets.QWidget()
        host.setLayout(FlowLayout())
        return label, host

    def _build_toolbar(self) -> None:
        tb = QtWidgets.QToolBar()
        tb.setMovable(False)
        tb.setFloatable(False)
        self.addToolBar(tb)

        def lab(text: str) -> QtWidgets.QLabel:
            w = QtWidgets.QLabel(text)
            w.setContentsMargins(10, 0, 4, 0)
            return w

        tb.addWidget(lab("Metric"))
        self.metric_box = QtWidgets.QComboBox()
        for key, m in METRICS.items():
            self.metric_box.addItem(m.label, key)
            self.metric_box.setItemData(self.metric_box.count() - 1, m.about, Qt.ItemDataRole.ToolTipRole)
        self.metric_box.currentIndexChanged.connect(self._on_change)
        tb.addWidget(self.metric_box)

        tb.addWidget(lab("Ignore"))
        self.inv_boxes: dict[str, QtWidgets.QCheckBox] = {}
        for key, text, on, tip in (
            ("position", "position", True, "Centre every stroke on its centroid. Off: where on the screen it was counts."),
            ("size", "size", True, "Scale every stroke to the same RMS radius. Off: a bigger copy is a different stroke."),
            ("rotation", "rotation", False, "Turn each candidate to fit the query best. Off: a tilted copy is different."),
            ("direction", "direction", False, "Also try each candidate backwards. Off: start and end matter."),
        ):
            cb = QtWidgets.QCheckBox(text)
            cb.setChecked(on)
            cb.setToolTip(tip)
            cb.toggled.connect(self._on_change)
            tb.addWidget(cb)
            self.inv_boxes[key] = cb

        tb.addWidget(lab("Show"))
        self.count_box = QtWidgets.QSpinBox()
        self.count_box.setRange(1, 200)
        self.count_box.setValue(self.o.count)
        self.count_box.setToolTip("Tiles in each grid")
        self.count_box.valueChanged.connect(self._on_change)
        tb.addWidget(self.count_box)

        tb.addWidget(lab("Points"))
        self.points_box = QtWidgets.QSpinBox()
        self.points_box.setRange(8, 256)
        self.points_box.setSingleStep(8)
        self.points_box.setValue(self.o.points)
        self.points_box.setToolTip("Every stroke is resampled to this many points before comparing")
        self.points_box.valueChanged.connect(self._on_change)
        tb.addWidget(self.points_box)

        tb.addWidget(lab("Min length"))
        self.min_len = QtWidgets.QSpinBox()
        self.min_len.setRange(0, 100000)
        self.min_len.setSingleStep(50)
        self.min_len.setSuffix(" px")
        self.min_len.setValue(20)
        self.min_len.setToolTip("Only strokes at least this long (logical px) are candidates")
        self.min_len.valueChanged.connect(self._on_change)
        tb.addWidget(self.min_len)

        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self.clear)
        tb.addWidget(clear)

    # ---- state
    def invariance(self) -> Invariance:
        return Invariance(**{k: cb.isChecked() for k, cb in self.inv_boxes.items()})

    def _describe(self) -> None:
        self.about.setText(METRICS[self.metric_box.currentData()].about)

    def _on_change(self, *_a) -> None:
        self._describe()
        self.refresh()

    def clear(self) -> None:
        self._gen += 1
        self.canvas.set_query(None)
        self.near_flow.layout().clear()
        self.far_flow.layout().clear()
        self.near_label.clear()
        self.far_label.clear()
        self.statusBar().showMessage(self._base_status + " · draw a stroke on the left")

    def use_stroke(self, k: int) -> None:
        self.canvas.set_query(self.ss.points(k))
        self.refresh()

    def candidates(self, n: int) -> np.ndarray:
        if n not in self._resampled:
            self._resampled[n] = self.ss.resampled(n)
        return self._resampled[n]

    # ---- search
    def refresh(self) -> None:
        qpts = self.canvas.query
        if qpts is None:
            return
        n, metric, inv = self.points_box.value(), self.metric_box.currentData(), self.invariance()
        idx = np.flatnonzero(self.ss.length >= self.min_len.value())
        cands = self.candidates(n)[idx]
        self._gen += 1
        gen = self._gen
        self.statusBar().showMessage(f"{self._base_status} · searching {len(idx):,} strokes…")
        if len(idx) == 0:
            self._show(gen, None, idx, 0.0)
            return

        def work() -> None:
            if gen != self._gen:
                return
            t = time.perf_counter()
            res = strokes.search(metric, qpts, cands, inv)
            self.bridge.done.emit(gen, res, idx, time.perf_counter() - t)

        self.pool.start(work)

    def _show(self, gen: int, res: Result | None, idx: np.ndarray, secs: float) -> None:
        if gen != self._gen:
            return
        self.near_flow.layout().clear()
        self.far_flow.layout().clear()
        if res is None:
            self.near_label.setText("No strokes pass the length filter")
            self.far_label.clear()
            self.statusBar().showMessage(self._base_status)
            return
        k = min(self.count_box.value(), len(idx))
        self.near_label.setText(f"Nearest {k}")
        self.far_label.setText(f"Farthest {k}")
        self._fill(self.near_flow.layout(), res, idx, res.order[:k])
        self._fill(self.far_flow.layout(), res, idx, res.order[::-1][:k])
        self.statusBar().showMessage(
            f"{self._base_status} · {METRICS[self.metric_box.currentData()].label} over {len(idx):,} strokes in {secs * 1000:.0f} ms")

    def _fill(self, flow: FlowLayout, res: Result, idx: np.ndarray, local: np.ndarray) -> None:
        x0, y0, x1, y1 = self.bounds
        aspect = (y1 - y0) / max(x1 - x0, 1e-9)
        for j in local:
            g = int(idx[j])
            t0, t1 = int(self.ss.t0[g]), int(self.ss.t1[g])
            dur = (t1 - t0) / NS
            npts = int(self.ss.stop[g] - self.ss.start[g])
            d = float(res.dist[j])
            where = ((self.centroid[g, 0] - x0) / max(x1 - x0, 1e-9), (self.centroid[g, 1] - y0) / max(y1 - y0, 1e-9))
            tile = Tile(
                g, res.cand[j], res.query, d,
                line1=("∞" if not np.isfinite(d) else "0" if d < 1e-9 else f"{d:.3g}") + f"   {fmt_time(t0, with_date=False)}",
                line2=f"{dur:.1f} s · {self.ss.length[g]:,.0f} px · {self.ss.app[g]}",
                where=where, screen_aspect=aspect,
                tip=(f"stroke {g}\n{fmt_time(t0)} → {fmt_time(t1)}\n{npts:,} samples · {self.ss.length[g]:,.0f} px · "
                     f"{dur:.2f} s\napp: {self.ss.app[g]}\n\nclick to use this stroke as the query"),
            )
            tile.picked.connect(self.use_stroke)
            flow.addWidget(tile)


def load(o: Options) -> tuple[StrokeSet, list]:
    with closing(query.connect(o.db)) as conn:
        ss = strokes.load_strokes(conn, o.rng, o.double_click, o.min_points)
        return ss, query.monitor_rects(conn, o.rng)


def run(o: Options) -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    try:
        ss, rects = load(o)
    except NoData as e:
        print(e, file=sys.stderr)
        return 1
    w = Window(o, ss, rects)
    w.resize(1500, 900)
    w.show()
    return app.exec()
