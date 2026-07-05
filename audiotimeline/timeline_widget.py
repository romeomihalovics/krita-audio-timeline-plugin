import math

from PyQt5.QtCore import Qt, QRect, QPoint, QSize, QEvent, pyqtSignal
from PyQt5.QtGui import QPainter, QColor, QPen, QBrush, QIcon, QPalette
from PyQt5.QtWidgets import QWidget, QInputDialog, QMenu, QUndoStack

from . import commands

TRACK_HEIGHT = 64
RULER_HEIGHT = 24
TRACK_HEADER_WIDTH = 110
BUTTON_SIZE = 18
BUTTON_GAP = 4

# Ruler tick spacing: the smallest gap (in px) two adjacent tick labels can
# sit at before they'd start overlapping/crowding each other.
# The smallest gap (in px) two adjacent tick *marks* can sit at -- much
# tighter than the label spacing below, so ticks can show finer subdivisions
# than their labels do without the marks themselves smearing together.
RULER_MIN_TICK_SPACING_PX = 10
RULER_MIN_LABEL_SPACING_PX = 40
# "Nice" round second counts to fall back to once even a one-second step
# would be too dense (zoomed out far) -- ticks then land on these instead
# of an arbitrary number of seconds.
_NICE_SECOND_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)

_icon_cache = {}
_fps_divisors_cache = {}


def _fps_divisors(fps):
    """All divisors of `fps`, ascending (including fps itself last) --
    candidate sub-second tick spacings that land evenly within a second."""
    if fps not in _fps_divisors_cache:
        _fps_divisors_cache[fps] = [d for d in range(1, fps + 1) if fps % d == 0]
    return _fps_divisors_cache[fps]


def _krita_icon(name):
    """Look up one of Krita's own themed icons by name, cached per name.

    Returns a null QIcon if Krita's Python API isn't available (e.g. when
    this module is imported outside Krita) or the name isn't recognized --
    callers should have a fallback for that case.
    """
    if name not in _icon_cache:
        icon = QIcon()
        try:
            from krita import Krita
            icon = Krita.instance().icon(name)
        except Exception:
            pass
        _icon_cache[name] = icon
    return _icon_cache[name]


def _mix(c1, c2, t):
    """Linear-interpolate between two QColors (t=0 -> c1, t=1 -> c2)."""
    t = max(0.0, min(1.0, t))
    return QColor(
        int(c1.red() + (c2.red() - c1.red()) * t),
        int(c1.green() + (c2.green() - c1.green()) * t),
        int(c1.blue() + (c2.blue() - c1.blue()) * t),
    )


def _paint_out_of_range_overlay(painter, end_x, width, height):
    """When a clip runs past the animation's own length, the timeline is
    widened to show its full end (see _content_end_frame()) -- shade that
    trailing region (from `end_x`, the animation's real end, to `width`)
    over the full `height` so it reads as "outside the playable range"
    rather than more animation. Shared between the track widget (its own
    lanes) and the ruler widget (the ticks above them) so the shading
    lines up seamlessly across both."""
    if end_x >= width:
        return
    painter.fillRect(QRect(end_x, 0, width - end_x, height), QColor(15, 15, 15, 90))


class AudioTimelineWidget(QWidget):
    """
    Renders N audio tracks as horizontal lanes with waveforms, plus a
    shared playhead. Emits `scrubbed(frame)` when the user drags the
    playhead or ruler. Every track/clip mutation (add, delete, move, mute,
    rename) goes through `undo_stack` so it's independently undoable/
    redoable from within the docker, and emits `contentChanged(affects_audio)`
    so the docker knows whether that mutation needs a mixdown re-render
    (e.g. an empty track add/remove/rename doesn't, a clip add/move/delete
    or a mute toggle on a track with clips does).
    """

    scrubbed = pyqtSignal(int)
    contentChanged = pyqtSignal(bool)
    # Emitted whenever this widget's size/content geometry may have changed
    # (zoom, track add/remove, clip drag/resize) -- the ruler lives in its
    # own widget (see AudioTimelineRulerWidget) outside this one's vertical
    # scroll viewport, so it can't just piggyback on this widget's own
    # resize/paint calls and needs to be told to keep its width in sync.
    layoutChanged = pyqtSignal()
    # Emitted on every current-frame change (even ones not user-initiated,
    # e.g. Krita playback) so the separate ruler widget's playhead line
    # stays in sync.
    frameChanged = pyqtSignal()
    # Emitted whenever active_track_index changes (selecting a track by
    # clicking its lane, or dragging a clip onto a different track) --
    # the header widget's active-track highlight lives in its own separate
    # widget now (see AudioTimelineHeaderWidget) and needs telling to
    # repaint, since it's not part of this widget's own paintEvent.
    activeTrackChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tracks = []
        self.fps = 24
        self.total_frames = 240
        self.px_per_frame = 4.0
        self.current_frame = 0
        self.active_track_index = 0
        self.selected_clip = None
        self.undo_stack = QUndoStack(self)

        self._drag_mode = None  # None | 'scrub' | 'clip'
        self._drag_clip = None
        self._drag_track = None       # track the clip currently lives on mid-drag
        self._drag_origin_track = None  # track it started the drag on
        self._drag_offset_frames = 0
        self._drag_start_frame = 0

        self.setMouseTracking(True)
        self.setMinimumHeight(TRACK_HEIGHT)
        # Needed to actually receive keyPressEvent (Delete key on a
        # selected clip) -- widgets default to no keyboard focus.
        self.setFocusPolicy(Qt.StrongFocus)

    # ---------------------------------------------------------------- layout
    def _content_end_frame(self):
        """The last frame that needs to be visible: the animation's own
        length, or the end of whichever clip runs past it, whichever is
        further -- so an over-long clip is never clipped off-screen."""
        end = self.total_frames
        for track in self.tracks:
            for clip in track.clips:
                end = max(end, clip.end_frame)
        return end

    def sizeHint(self):
        width = int(self._content_end_frame() * self.px_per_frame) + 40
        height = max(1, len(self.tracks)) * TRACK_HEIGHT
        return QSize(width, height)

    def _apply_size_hint(self):
        # With setWidgetResizable(False), QScrollArea doesn't auto-grow the
        # widget from sizeHint() on its own -- updateGeometry() alone leaves
        # the widget's actual size unchanged, so new tracks/wider zoom just
        # get clipped outside it. Resize explicitly whenever content grows.
        self.resize(self.sizeHint())
        self.layoutChanged.emit()

    def set_fps(self, fps):
        self.fps = max(1, fps)
        self.updateGeometry()
        self._apply_size_hint()
        self.update()

    def set_total_frames(self, total_frames):
        self.total_frames = max(1, total_frames)
        self.updateGeometry()
        self._apply_size_hint()
        self.update()

    def set_current_frame(self, frame, emit=False):
        self.current_frame = max(0, frame)
        self.update()
        self.frameChanged.emit()
        if emit:
            self.scrubbed.emit(self.current_frame)

    def set_active_track_index(self, index):
        self.active_track_index = index
        self.update()
        self.activeTrackChanged.emit()

    def add_track(self, track):
        self.insert_track(len(self.tracks), track)

    def insert_track(self, index, track):
        self.tracks.insert(index, track)
        self.updateGeometry()
        self._apply_size_hint()
        self.update()

    def remove_track(self, track):
        if track in self.tracks:
            self.tracks.remove(track)
        if self.active_track_index >= len(self.tracks):
            self.active_track_index = max(0, len(self.tracks) - 1)
        self.updateGeometry()
        self._apply_size_hint()
        self.update()

    def refresh_layout(self):
        """Recomputes size/geometry and repaints after tracks were swapped
        out wholesale from outside the normal add/remove-track calls above
        (the docker restoring a different document's cached track list on
        switch)."""
        self.updateGeometry()
        self._apply_size_hint()
        self.update()

    # ------------------------------------------------------------- geometry
    def frame_to_x(self, frame):
        return int(frame * self.px_per_frame)

    def x_to_frame(self, x):
        frame = x / self.px_per_frame
        return max(0, int(round(frame)))

    def track_y(self, index):
        return index * TRACK_HEIGHT

    def visible_x_range(self):
        """The horizontal span of *this* widget's content actually exposed
        by its enclosing QScrollArea's viewport, in this widget's own
        (unscrolled, frame_to_x) coordinate space.

        This is deliberately not event.rect() from paintEvent: Qt scrolls a
        widget's existing backing-store pixels and only re-invokes
        paintEvent for the newly-uncovered sliver rather than the whole
        viewport, so using the dirty rect to decide where a sticky label
        should be drawn leaves stale copies behind at the old position
        (ghosting) as soon as more than one scroll step happens between
        full repaints.
        """
        viewport = self.parentWidget()
        if viewport is None:
            return 0, self.width()
        left = -self.x()
        return left, left + viewport.width()

    def track_index_at_y(self, y):
        if y < 0:
            return -1
        idx = y // TRACK_HEIGHT
        if 0 <= idx < len(self.tracks):
            return idx
        return -1

    def _clamped_track_index_at_y(self, y):
        """Like track_index_at_y(), but clamps into range instead of
        returning -1 -- used while dragging a clip so it stays assigned to
        the nearest track even if the mouse strays above the top or below
        the last lane."""
        if not self.tracks:
            return -1
        idx = y // TRACK_HEIGHT
        return max(0, min(idx, len(self.tracks) - 1))

    def mute_rect_for(self, index):
        y = self.track_y(index)
        x = TRACK_HEADER_WIDTH - BUTTON_SIZE - BUTTON_GAP
        return QRect(x, y + (TRACK_HEIGHT - BUTTON_SIZE) // 2, BUTTON_SIZE, BUTTON_SIZE)

    def delete_rect_for(self, index):
        mute = self.mute_rect_for(index)
        x = mute.left() - BUTTON_SIZE - BUTTON_GAP
        return QRect(x, mute.top(), BUTTON_SIZE, BUTTON_SIZE)

    # --------------------------------------------------------------- theme
    def _theme_colors(self):
        """Derive every paint color from the widget's live QPalette.

        Krita repaints its own palette to match the active theme (dark,
        light, or a custom one), and that palette propagates to this widget
        automatically -- so basing colors on it (rather than fixed RGB
        triples) keeps the timeline consistent with whatever theme Krita is
        currently using instead of assuming a specific dark scheme.
        """
        pal = self.palette()
        canvas_bg = pal.color(QPalette.Window)
        text = pal.color(QPalette.WindowText)
        accent = pal.color(QPalette.Highlight)
        accent_text = pal.color(QPalette.HighlightedText)

        is_dark = canvas_bg.lightness() < 128
        black, white = QColor(0, 0, 0), QColor(255, 255, 255)
        # Direction that reads as "raised"/foreground relative to the
        # background, whichever theme we're in.
        toward_fg = white if is_dark else black
        toward_bg = black if is_dark else white

        def elevate(c, t):
            return _mix(c, toward_fg, t)

        def recede(c, t):
            return _mix(c, toward_bg, t)

        header_bg = elevate(canvas_bg, 0.10)
        # Selected track: an unambiguously darker shade of the header
        # (not a hardcoded blue), regardless of light/dark theme.
        header_active_bg = _mix(header_bg, black, 0.35)

        return {
            'canvas_bg': canvas_bg,
            'header_bg': header_bg,
            'header_active_bg': header_active_bg,
            'header_text': text,
            'lane_bg': recede(canvas_bg, 0.08),
            'lane_muted_bg': recede(canvas_bg, 0.20),
            'border': recede(canvas_bg, 0.45),
            'ruler_bg': recede(canvas_bg, 0.35),
            'ruler_text': _mix(text, canvas_bg, 0.3),
            'button_bg': elevate(canvas_bg, 0.18),
            'button_muted_bg': _mix(QColor(190, 70, 70), canvas_bg, 0.15),
            'clip_fill': _mix(accent, canvas_bg, 0.25),
            'clip_border': _mix(accent, canvas_bg, 0.55),
            'clip_border_selected': elevate(accent, 0.25),
            'clip_text': accent_text,
            'waveform': _mix(accent_text, accent, 0.25),
        }

    # --------------------------------------------------------------- paint
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        theme = self._theme_colors()
        painter.fillRect(self.rect(), theme['canvas_bg'])

        # The widget is sized to its full (possibly huge, at high zoom or
        # with long clips) content width, but sits inside a QScrollArea
        # narrower than that -- event.rect() is only the sliver of it
        # actually exposed by the current scroll position, so clips (and
        # buckets within a clip) outside it can skip all their drawing work
        # below rather than paying for content that's off-screen anyway.
        visible_left = event.rect().left()
        visible_right = event.rect().right()
        visible_top = event.rect().top()
        visible_bottom = event.rect().bottom()

        for i, track in enumerate(self.tracks):
            y = self.track_y(i)
            if y + TRACK_HEIGHT < visible_top or y > visible_bottom:
                continue  # scrolled entirely out of view vertically
            self._paint_track(painter, i, track, theme, visible_left, visible_right)
        _paint_out_of_range_overlay(painter, self.frame_to_x(self.total_frames), self.width(), self.height())
        self._paint_playhead(painter)
        painter.end()

    def _paint_track(self, painter, index, track, theme, visible_left, visible_right):
        y = self.track_y(index)
        lane_rect = QRect(0, y, self.width(), TRACK_HEIGHT)
        painter.fillRect(lane_rect, theme['lane_muted_bg'] if track.muted else theme['lane_bg'])
        painter.setPen(QPen(theme['border']))
        # drawRect() fills with the painter's *current* brush, not just its
        # pen -- without resetting to NoBrush here, a leftover brush from a
        # previous track's clip (set in _paint_clip) bleeds into this
        # outline-only rect and paints the whole next lane solid.
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(lane_rect.adjusted(0, 0, -1, -1))

        for clip in track.clips:
            self._paint_clip(painter, y, clip, theme, visible_left, visible_right)

    def _paint_clip(self, painter, track_y, clip, theme, visible_left, visible_right):
        x0 = self.frame_to_x(clip.start_frame)
        x1 = self.frame_to_x(clip.end_frame)
        clip_rect = QRect(x0, track_y + 4, max(2, x1 - x0), TRACK_HEIGHT - 8)

        if clip_rect.right() < visible_left or clip_rect.left() > visible_right:
            return  # scrolled entirely out of view -- nothing to paint

        is_selected = clip is self.selected_clip
        painter.setBrush(QBrush(theme['clip_fill']))
        painter.setPen(QPen(theme['clip_border_selected'] if is_selected else theme['clip_border'],
                             3 if is_selected else 1))
        painter.drawRoundedRect(clip_rect, 4, 4)

        if clip.peaks:
            self._paint_waveform(painter, clip_rect, clip.peaks, theme, visible_left, visible_right)

        painter.setPen(theme['clip_text'])
        # Keep the label pinned to the left edge of the *visible* viewport
        # while the clip is on screen, rather than the clip's own (possibly
        # scrolled-off) left edge -- but never let it slide past the clip's
        # right edge, and clip drawing to clip_rect so a long name can't
        # bleed into the next clip. Uses the actual scroll viewport, not
        # the paintEvent dirty rect (visible_left/right) -- see
        # visible_x_range()'s docstring for why that would ghost.
        sticky_left, _sticky_right = self.visible_x_range()
        text_left = max(clip_rect.left(), sticky_left) + 4
        text_left = min(text_left, clip_rect.right() - 4)
        text_rect = QRect(text_left, clip_rect.top() + 2,
                           max(0, clip_rect.right() - 4 - text_left), clip_rect.height() - 2)
        painter.save()
        painter.setClipRect(clip_rect)
        painter.drawText(text_rect, Qt.AlignTop | Qt.AlignLeft, clip.name)
        painter.restore()

    def _paint_waveform(self, painter, clip_rect, peaks, theme, visible_left, visible_right):
        n = len(peaks)
        if n == 0:
            return
        step_px = clip_rect.width() / float(n)
        mid_y = clip_rect.center().y()
        half_h = clip_rect.height() / 2 - 4

        # Only walk buckets that fall within the visible horizontal range,
        # not the clip's whole stored peak array -- a long, mostly
        # scrolled-off clip can have tens of thousands of buckets (see
        # waveform_utils.MAX_BUCKETS), and re-walking all of them on every
        # repaint (drag, scroll, zoom, playhead move) is wasted work for
        # whatever's currently off-screen.
        draw_left = max(clip_rect.left(), visible_left)
        draw_right = min(clip_rect.right(), visible_right)
        if draw_right < draw_left:
            return
        start_idx = max(0, int((draw_left - clip_rect.left()) / step_px) - 1)
        end_idx = min(n, int((draw_right - clip_rect.left()) / step_px) + 2)

        painter.setPen(QPen(theme['waveform'], 1))

        if step_px >= 1.0:
            # Zoomed in enough that each bucket gets its own pixel column
            # (or more) -- draw them one-for-one, same as before.
            for i in range(start_idx, end_idx):
                lo, hi = peaks[i]
                x = clip_rect.left() + i * step_px
                painter.drawLine(int(x), int(mid_y - hi * half_h), int(x), int(mid_y - lo * half_h))
            return

        # Zoomed out far enough that multiple buckets land on the same
        # pixel column -- drawing one line per bucket would mean many
        # overlapping drawLine calls for a single visible pixel. Collapse
        # each column's buckets into one min/max pair first instead, so
        # draw calls scale with on-screen pixels rather than stored buckets.
        buckets_per_px = max(1, int(round(1.0 / step_px)))
        i = start_idx
        while i < end_idx:
            group = peaks[i:i + buckets_per_px]
            if not group:
                break
            lo = min(p[0] for p in group)
            hi = max(p[1] for p in group)
            x = clip_rect.left() + i * step_px
            painter.drawLine(int(x), int(mid_y - hi * half_h), int(x), int(mid_y - lo * half_h))
            i += buckets_per_px

    def _paint_playhead(self, painter):
        x = self.frame_to_x(self.current_frame)
        painter.setPen(QPen(QColor(255, 80, 80), 2))
        painter.drawLine(x, 0, x, self.height())

    # --------------------------------------------------------------- input
    def mousePressEvent(self, event):
        self.setFocus()
        pos = event.pos()
        if event.modifiers() & Qt.ShiftModifier:
            self._drag_mode = 'scrub'
            self.set_current_frame(self.x_to_frame(pos.x()), emit=True)
            return

        track_idx = self.track_index_at_y(pos.y())
        if track_idx >= 0:
            track = self.tracks[track_idx]
            self.set_active_track_index(track_idx)
            frame = self.x_to_frame(pos.x())
            clip = track.clip_at_frame(frame)
            if clip is not None:
                self.selected_clip = clip
                self._drag_mode = 'clip'
                self._drag_clip = clip
                self._drag_track = track
                self._drag_origin_track = track
                self._drag_offset_frames = frame - clip.start_frame
                self._drag_start_frame = clip.start_frame
                self.update()
                return
            self.selected_clip = None

        # fall back: clicking empty lane space also scrubs
        self._drag_mode = 'scrub'
        self.set_current_frame(self.x_to_frame(pos.x()), emit=True)
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.pos()
        if self._drag_mode == 'scrub':
            self.set_current_frame(self.x_to_frame(pos.x()), emit=True)
        elif self._drag_mode == 'clip' and self._drag_clip is not None:
            new_start = self.x_to_frame(pos.x()) - self._drag_offset_frames
            new_start = max(0, new_start)

            target_idx = self._clamped_track_index_at_y(pos.y())
            if target_idx >= 0:
                target_track = self.tracks[target_idx]
                if target_track is not self._drag_track:
                    # Hand the clip over to the track under the cursor right
                    # away so painting (which iterates each track's own
                    # clip list) and the overlap clamp below both see it on
                    # its new lane for the rest of the drag -- same live-
                    # mutate-then-commit pattern the horizontal-only case
                    # already used for start_frame.
                    self._drag_track.remove_clip(self._drag_clip.id)
                    target_track.add_clip(self._drag_clip)
                    self._drag_track = target_track
                    self.set_active_track_index(target_idx)

            # clamp_move_start() excludes the clip itself by id, so this
            # applies the exact same overlap-avoidance/cascading rules used
            # for in-track moves, whether or not the track just changed.
            self._drag_clip.start_frame = self._drag_track.clamp_move_start(self._drag_clip, new_start)
            self.updateGeometry()
            self._apply_size_hint()
            self.update()

    def mouseReleaseEvent(self, event):
        if self._drag_mode == 'clip' and self._drag_clip is not None:
            moved = (self._drag_clip.start_frame != self._drag_start_frame
                     or self._drag_track is not self._drag_origin_track)
            if moved:
                self.undo_stack.push(commands.MoveClipCommand(
                    self, self._drag_clip,
                    self._drag_origin_track, self._drag_start_frame,
                    self._drag_track, self._drag_clip.start_frame,
                ))
        self._drag_mode = None
        self._drag_clip = None
        self._drag_track = None
        self._drag_origin_track = None

    def event(self, event):
        # Krita binds Delete (and, on the canvas, Ctrl+Z/Ctrl+Y) to its own
        # actions, and Qt resolves those global shortcuts before ever
        # calling keyPressEvent -- so without claiming these here via
        # ShortcutOverride, Krita's actions eat the key and this widget
        # never sees it.
        if event.type() == QEvent.ShortcutOverride:
            if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.selected_clip is not None:
                event.accept()
                return True
            if self._is_undo_shortcut(event) or self._is_redo_shortcut(event):
                event.accept()
                return True
        return super().event(event)

    @staticmethod
    def _is_undo_shortcut(event):
        mods = event.modifiers()
        return bool(mods & Qt.ControlModifier) and not (mods & Qt.ShiftModifier) and event.key() == Qt.Key_Z

    @staticmethod
    def _is_redo_shortcut(event):
        mods = event.modifiers()
        if not (mods & Qt.ControlModifier):
            return False
        # Accept both common Redo conventions -- plain Ctrl+Y, and
        # Ctrl+Shift+Z -- since Krita/the desktop environment may already
        # bind one of these to its own Redo action as an alternate
        # shortcut, in which case only the other one reliably reaches us.
        if event.key() == Qt.Key_Y:
            return True
        return bool(mods & Qt.ShiftModifier) and event.key() == Qt.Key_Z

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.selected_clip is not None:
            self._delete_clip(self.selected_clip)
            return
        if self._is_undo_shortcut(event):
            self.undo_stack.undo()
            return
        if self._is_redo_shortcut(event):
            self.undo_stack.redo()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        pos = event.pos()
        idx = self.track_index_at_y(pos.y())
        if idx < 0:
            return
        track = self.tracks[idx]
        frame = self.x_to_frame(pos.x())
        clip = track.clip_at_frame(frame)
        if clip is None:
            return
        menu = QMenu(self)
        delete_action = menu.addAction("Delete Clip")
        chosen = menu.exec_(event.globalPos())
        if chosen == delete_action:
            self._delete_clip(clip)

    def _delete_clip(self, clip):
        for track in self.tracks:
            if clip in track.clips:
                self.undo_stack.push(commands.DeleteClipCommand(self, track, clip))
                break

    def wheelEvent(self, event):
        # Ctrl+wheel to zoom the timeline horizontally
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            factor = 1.15 if delta > 0 else (1 / 1.15)
            self.px_per_frame = max(0.5, min(40.0, self.px_per_frame * factor))
            self.updateGeometry()
            self._apply_size_hint()
            self.update()
        else:
            super().wheelEvent(event)


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
            for divisor in _fps_divisors(fps):
                if divisor >= min_frames:
                    return divisor
            return fps

        needed_seconds = math.ceil(min_frames / fps)
        for seconds in _NICE_SECOND_STEPS:
            if seconds >= needed_seconds:
                return seconds * fps
        return _NICE_SECOND_STEPS[-1] * fps

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

        _paint_out_of_range_overlay(
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
            mute_icon = _krita_icon("audio-none" if track.muted else "audio-volume-high")
            mute_icon.paint(painter, mute_rect.adjusted(2, 2, -2, -2))

            delete_rect = timeline.delete_rect_for(index)
            painter.setBrush(QBrush(theme['button_bg']))
            painter.setPen(QPen(theme['border']))
            painter.drawRoundedRect(delete_rect, 3, 3)
            delete_icon = _krita_icon("deletelayer")
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
