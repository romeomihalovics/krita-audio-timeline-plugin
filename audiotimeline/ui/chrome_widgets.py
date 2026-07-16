from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPainter, QPen
from PyQt5.QtWidgets import QWidget

from .timeline_constants import TRACK_HEADER_WIDTH, RULER_HEIGHT


class AudioTimelineCornerWidget(QWidget):
    """Fills the top-left corner left empty by the ruler (above) and the
    header column (to its left) both living outside the tracks'
    scrollable area -- otherwise that corner would just show whatever's
    behind the docker."""

    def __init__(self, timeline, parent=None):
        super().__init__(parent)
        self.timeline = timeline
        self.setFixedSize(TRACK_HEADER_WIDTH, RULER_HEIGHT)

    def paintEvent(self, event):
        painter = QPainter(self)
        theme = self.timeline._theme_colors()
        painter.fillRect(self.rect(), theme['header_active_bg'])
        # Painted (not a stylesheet border) so it doesn't add to this
        # widget's fixed size -- that would grow row 0 of the docker's grid
        # past the ruler's own fixed height and misalign row 1 underneath.
        painter.setPen(QPen(theme['border']))
        y = self.height() - 1
        painter.drawLine(0, y, self.width(), y)
        painter.end()


class _BorderStripWidget(QWidget):
    """A 1px overlay strip, kept raised above its siblings via raise_().

    Widgets painted by their *parent's* paintEvent get drawn over by any
    child added to that parent afterwards (e.g. a QScrollArea) -- children
    always paint on top of their parent, regardless of add order. Using an
    actual (if tiny) child widget instead, and explicitly raising it, keeps
    the border visible above the scroll area rather than being silently
    painted-under."""

    def __init__(self, timeline, parent=None):
        super().__init__(parent)
        self.timeline = timeline
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.timeline._theme_colors()['border'])
        painter.end()


class AudioTimelineHeaderWrapperWidget(QWidget):
    """Wraps the header column's scroll area (and its scrollbar-gutter
    spacer -- see AudioTimelineDocker._build_ui) so the border separating
    track headers from track lanes spans the whole column, not just the
    header widget's own content. Drawn as a raised overlay strip (see
    _BorderStripWidget) rather than a stylesheet border, so it neither adds
    to the wrapper's width (which would grow the header column wider than
    the corner cell above it) nor gets painted over by the header scroll
    area added into this wrapper's layout afterwards."""

    def __init__(self, timeline, parent=None):
        super().__init__(parent)
        self.timeline = timeline
        self._border = _BorderStripWidget(timeline, self)
        self._position_border()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_border()

    def _position_border(self):
        self._border.setGeometry(max(0, self.width() - 1), 0, 1, self.height())
        self._border.raise_()
