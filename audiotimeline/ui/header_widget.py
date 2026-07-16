from PyQt5.QtCore import Qt, QRect, QSize
from PyQt5.QtGui import QPainter, QPen, QBrush
from PyQt5.QtWidgets import QWidget, QInputDialog

from .. import commands
from .timeline_constants import TRACK_HEADER_WIDTH, TRACK_HEIGHT
from .timeline_icons import krita_icon


class AudioTimelineHeaderWidget(QWidget):
    """The per-track header column (name + mute/delete buttons), split out
    from AudioTimelineWidget into its own widget so it can sit outside that
    widget's QScrollArea (see the docker) -- always visible on the left
    regardless of horizontal scroll position, while still scrolling
    vertically in lockstep with it. Reads tracks/active_track_index live
    off `timeline` rather than owning any of its own state; only mute,
    delete and rename actually mutate it (through its undo_stack, same as
    before this widget existed).
    """

    def __init__(self, timeline, parent=None):
        super().__init__(parent)
        self.timeline = timeline
        self.setFixedWidth(TRACK_HEADER_WIDTH)
        self.setMouseTracking(True)

    def sizeHint(self):
        return QSize(TRACK_HEADER_WIDTH, self.timeline.sizeHint().height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        timeline = self.timeline
        theme = timeline._theme_colors()
        painter.fillRect(self.rect(), theme['canvas_bg'])

        for index, track in enumerate(timeline.tracks):
            y = timeline.track_y(index)
            header_rect = QRect(0, y, TRACK_HEADER_WIDTH, TRACK_HEIGHT)
            is_active = index == timeline.active_track_index
            painter.fillRect(header_rect, theme['header_active_bg'] if is_active else theme['header_bg'])
            painter.setPen(theme['header_text'])
            painter.drawText(header_rect.adjusted(8, 0, -50, 0),
                              Qt.AlignVCenter | Qt.AlignLeft, track.name)

            if index < len(timeline.tracks) - 1:
                painter.setPen(QPen(theme['border']))
                bottom = header_rect.bottom()
                painter.drawLine(header_rect.left(), bottom, header_rect.right(), bottom)

            mute_rect = timeline.mute_rect_for(index)
            # Red background when muted (audio-none), neutral when audible
            # (audio-volume-high) -- matches Krita's own themed icon names.
            painter.setBrush(QBrush(theme['button_muted_bg'] if track.muted else theme['button_bg']))
            painter.setPen(QPen(theme['border']))
            painter.drawRoundedRect(mute_rect, 3, 3)
            mute_icon = krita_icon("audio-none" if track.muted else "audio-volume-high")
            mute_icon.paint(painter, mute_rect.adjusted(2, 2, -2, -2))

            delete_rect = timeline.delete_rect_for(index)
            painter.setBrush(QBrush(theme['button_bg']))
            painter.setPen(QPen(theme['border']))
            painter.drawRoundedRect(delete_rect, 3, 3)
            delete_icon = krita_icon("deletelayer")
            delete_icon.paint(painter, delete_rect.adjusted(2, 2, -2, -2))
        painter.end()

    def mousePressEvent(self, event):
        pos = event.pos()
        timeline = self.timeline
        idx = timeline.track_index_at_y(pos.y())
        if idx < 0:
            return
        if timeline.mute_rect_for(idx).contains(pos):
            track = timeline.tracks[idx]
            timeline.undo_stack.push(commands.MuteTrackCommand(timeline, track))
            return
        if timeline.delete_rect_for(idx).contains(pos):
            track = timeline.tracks[idx]
            timeline.undo_stack.push(commands.DeleteTrackCommand(timeline, track))
            return
        # Clicking the name area (not a button) makes this the active
        # track -- the one new imports land on. set_active_track_index()
        # emits activeTrackChanged, which the docker wires to self.update()
        # (see AudioTimelineDocker._build_ui) -- no need to repaint here too.
        timeline.set_active_track_index(idx)

    def mouseDoubleClickEvent(self, event):
        pos = event.pos()
        timeline = self.timeline
        idx = timeline.track_index_at_y(pos.y())
        if idx < 0:
            return
        if timeline.mute_rect_for(idx).contains(pos) or timeline.delete_rect_for(idx).contains(pos):
            return
        track = timeline.tracks[idx]
        new_name, ok = QInputDialog.getText(self, "Rename Track", "Name:", text=track.name)
        new_name = new_name.strip()
        if ok and new_name and new_name != track.name:
            timeline.undo_stack.push(commands.RenameTrackCommand(timeline, track, new_name))
