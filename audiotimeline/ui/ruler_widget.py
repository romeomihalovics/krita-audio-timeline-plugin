import math

from PyQt5.QtCore import QSize
from PyQt5.QtGui import QPainter, QColor, QPen
from PyQt5.QtWidgets import QWidget

from .timeline_constants import RULER_HEIGHT, RULER_MIN_TICK_SPACING_PX, RULER_MIN_LABEL_SPACING_PX, NICE_SECOND_STEPS
from .timeline_icons import fps_divisors
from .timeline_theme import paint_out_of_range_overlay


class AudioTimelineRulerWidget(QWidget):
    """The frame ruler + out-of-range overlay, split out from
    AudioTimelineWidget into its own widget so it can sit outside that
    widget's QScrollArea (see the docker) -- always visible above the
    tracks regardless of vertical scroll position, while still scrolling
    horizontally in lockstep with it. Reads all of its geometry (fps,
    total_frames, px_per_frame, tracks-for-content-end) live off `timeline`
    rather than owning any of its own state.
    """

    def __init__(self, timeline, parent=None):
        super().__init__(parent)
        self.timeline = timeline
        self._dragging = False
        self.setFixedHeight(RULER_HEIGHT)
        self.setMouseTracking(True)

    def sizeHint(self):
        return QSize(self.timeline.sizeHint().width(), RULER_HEIGHT)

    def _tick_step_frames(self, min_spacing_px):
        """Picks a "nice" spacing (in frames) satisfying `min_spacing_px`
        for the current zoom level: coarser round-second steps when zoomed
        out, finer fps-divisor sub-second steps when zoomed in."""
        timeline = self.timeline
        px_per_frame = max(1e-6, timeline.px_per_frame)
        fps = max(1, int(timeline.fps))
        min_frames = max(1, math.ceil(min_spacing_px / px_per_frame))

        if min_frames <= fps:
            for divisor in fps_divisors(fps):
                if divisor >= min_frames:
                    return divisor
            return fps

        needed_seconds = math.ceil(min_frames / fps)
        for seconds in NICE_SECOND_STEPS:
            if seconds >= needed_seconds:
                return seconds * fps
        return NICE_SECOND_STEPS[-1] * fps

    def _label_and_tick_step_frames(self):
        """The label spacing is computed exactly as before (unchanged
        values/positions from before ticks-without-labels existed). The
        tick spacing is then derived as an even subdivision *of* that --
        not an independent "nice" spacing of its own -- so every labeled
        position is still guaranteed to land exactly on a drawn tick."""
        timeline = self.timeline
        label_step = self._tick_step_frames(RULER_MIN_LABEL_SPACING_PX)
        label_step_px = label_step * timeline.px_per_frame

        max_subdivisions = max(1, int(label_step_px // RULER_MIN_TICK_SPACING_PX))
        divisor = 1
        for k in range(min(max_subdivisions, label_step), 0, -1):
            if label_step % k == 0:
                divisor = k
                break
        return label_step, label_step // divisor

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        timeline = self.timeline
        theme = timeline._theme_colors()

        painter.fillRect(0, 0, self.width(), RULER_HEIGHT, theme['ruler_bg'])
        fps = max(1, int(timeline.fps))
        major_pen = QPen(theme['ruler_text'])
        minor_color = QColor(theme['ruler_text'])
        minor_color.setAlphaF(0.5)
        minor_pen = QPen(minor_color)

        label_step, tick_step = self._label_and_tick_step_frames()

        f = 0
        while f <= timeline._content_end_frame():
            # Whole-second marks stay fully opaque; any finer sub-second
            # ticks shown at higher zoom are dimmed instead, so the
            # seconds grid still reads clearly among them.
            painter.setPen(major_pen if f % fps == 0 else minor_pen)
            x = timeline.frame_to_x(f)
            painter.drawLine(x, RULER_HEIGHT - 6, x, RULER_HEIGHT)
            if f % label_step == 0:
                painter.drawText(x + 2, RULER_HEIGHT - 8, f"{f}")
            f += tick_step

        paint_out_of_range_overlay(
            painter, timeline.frame_to_x(timeline.total_frames), self.width(), self.height())

        x = timeline.frame_to_x(timeline.current_frame)
        painter.setPen(QPen(QColor(255, 80, 80), 2))
        painter.drawLine(x, 0, x, self.height())
        painter.end()

    def mousePressEvent(self, event):
        self._dragging = True
        self.timeline.set_current_frame(self.timeline.x_to_frame(event.pos().x()), emit=True)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.timeline.set_current_frame(self.timeline.x_to_frame(event.pos().x()), emit=True)

    def mouseReleaseEvent(self, event):
        self._dragging = False
