import math

from PyQt5.QtCore import Qt, QRect, QPoint, QSize, QEvent, pyqtSignal
from PyQt5.QtGui import QPainter, QColor, QPen, QBrush, QIcon, QPalette, QFontMetrics, QPixmap
from PyQt5.QtWidgets import (
    QWidget, QInputDialog, QMenu, QUndoStack, QStyle, QApplication, QMessageBox,
)

from . import commands
from . import volume_envelope
from .audio_track import AudioClip, AudioTrack

# Extensions accepted by drag-and-drop from outside Krita and by pasting a
# file path copied from the OS -- kept in sync with import_audio's own
# QFileDialog filter (AudioTimelineDocker.import_audio).
EXTERNAL_AUDIO_EXTENSIONS = ('.wav', '.mp3', '.ogg', '.flac')

TRACK_HEIGHT = 64
RULER_HEIGHT = 24
TRACK_HEADER_WIDTH = 110
BUTTON_SIZE = 18
BUTTON_GAP = 4
# How close (in px) the mouse needs to be to a clip's left/right edge for a
# press/hover there to be treated as a trim-edge drag rather than a
# whole-clip move.
HANDLE_PX = 6
# Size (px) of the small per-clip volume-editing toggle icon drawn at each
# clip's top-left corner.
VOLUME_ICON_SIZE = 13
# Gain (1.0 == 100%) at which the volume line sits at clip_rect's vertical
# middle -- the mapping's fixed point -- and the ceiling it can be dragged
# up to (mapped to clip_rect's top). 0.0 always maps to the bottom.
VOLUME_GAIN_UNITY = 1.0
VOLUME_GAIN_MAX = 2.0
# Radius (px) of the filled circle drawn at each bend point, and the hit-test
# tolerance (combined with HANDLE_PX) for grabbing/double-clicking one.
VOLUME_POINT_RADIUS = 4

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


def _krita_icon(name, fallback_standard_icon=None):
    """Look up one of Krita's own themed icons by name, cached per
    (name, fallback) pair.

    Falls back to a Qt-bundled standard icon (e.g. QStyle.SP_DialogApplyButton)
    when given one and Krita's Python API isn't available (e.g. this module
    imported outside Krita) or doesn't recognize the name -- otherwise
    returns a null QIcon, and callers without a fallback need to tolerate
    that themselves.
    """
    cache_key = (name, fallback_standard_icon)
    if cache_key not in _icon_cache:
        icon = QIcon()
        try:
            from krita import Krita
            icon = Krita.instance().icon(name)
        except Exception:
            pass
        if icon.isNull() and fallback_standard_icon is not None:
            style = QApplication.style()
            if style is not None:
                icon = style.standardIcon(fallback_standard_icon)
        _icon_cache[cache_key] = icon
    return _icon_cache[cache_key]


_tinted_pixmap_cache = {}


def _tinted_icon_pixmap(icon, size, color):
    """`icon` rendered at `size`x`size` and recolored solid `color`,
    keeping only its original alpha shape -- so the passive volume badge's
    icon can match theme['clip_text'] exactly (the color the badge's own
    percentage text is drawn in) rather than whatever fixed tint the
    Krita/Qt icon theme happens to bake in. Cached per (icon, size, color)
    since it's repainted every frame a badge is on screen."""
    cache_key = (icon.cacheKey(), size, color.rgba())
    if cache_key not in _tinted_pixmap_cache:
        pixmap = icon.pixmap(size, size)
        if not pixmap.isNull():
            tinted = QPixmap(pixmap.size())
            tinted.fill(Qt.transparent)
            painter = QPainter(tinted)
            painter.drawPixmap(0, 0, pixmap)
            painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
            painter.fillRect(tinted.rect(), color)
            painter.end()
            pixmap = tinted
        _tinted_pixmap_cache[cache_key] = pixmap
    return _tinted_pixmap_cache[cache_key]


def _mix(c1, c2, t):
    """Linear-interpolate between two QColors (t=0 -> c1, t=1 -> c2)."""
    t = max(0.0, min(1.0, t))
    return QColor(
        int(c1.red() + (c2.red() - c1.red()) * t),
        int(c1.green() + (c2.green() - c1.green()) * t),
        int(c1.blue() + (c2.blue() - c1.blue()) * t),
    )


def _complementary(c):
    """The hue-opposite of `c` (same saturation/value/alpha, hue rotated
    180 degrees) -- used to derive a color that reads as unmistakably
    distinct from the accent-derived clip/text colors while still being
    computed from (so themed consistently with) the same palette."""
    h, s, v, a = c.getHsv()
    if h < 0:  # fully desaturated (grayscale) -- no hue to rotate
        return QColor(c)
    return QColor.fromHsv((h + 180) % 360, s, v, a)


def _intensify(c):
    """A more saturated, brighter version of `c` at the same hue -- used
    for the selected-bend-point highlight so it reads as "this same color,
    turned up" rather than an unrelated accent color competing with it."""
    h, s, v, a = c.getHsv()
    if h < 0:  # fully desaturated -- no hue to push, just brighten
        h, s = 0, 0
    s = min(255, int(s * 1.35) + 40)
    v = min(255, int(v * 1.2) + 40)
    return QColor.fromHsv(h, s, v, a)


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
    # Emitted whenever selected_clip changes (including to/from None), with
    # whether a clip is now selected -- lets other widgets (e.g. the split
    # toolbar button) mirror the selection state without polling.
    selectionChanged = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tracks = []
        self.fps = 24
        self.total_frames = 240
        self.px_per_frame = 4.0
        self.current_frame = 0
        self.active_track_index = 0
        self._selected_clip = None
        # The clip last copied with Ctrl+C (see copy_selected_clip), pasted
        # via Ctrl+V (paste_clip) -- a live reference to the original clip
        # object, not a frozen snapshot, so pasting after further edits to
        # it (trim, volume, etc.) copies its current state, same as a
        # normal clipboard.
        self._clipboard_clip = None
        # The clip currently showing the volume-line overlay (toggled by
        # clicking its top-left volume icon), or None. Only one clip can be
        # in volume-editing mode at a time.
        self.volume_editing_clip = None
        # Snapshot of volume_editing_clip's volume_points taken when it
        # entered editing mode, so the Cancel (X) icon / Escape can revert
        # to it -- see _enter_volume_editing()/_exit_volume_editing().
        self._volume_edit_entry_points = None
        # Index into volume_editing_clip.volume_points of whichever bend
        # point was last clicked/grabbed, once the envelope has more than
        # the two default endpoints -- drawn with a highlighted border and
        # the target of the Delete key/right-click-to-remove (see
        # _remove_volume_point). None while flat (no individual points to
        # select) or when nothing's been clicked yet.
        self.selected_volume_point_index = None
        self.undo_stack = QUndoStack(self)

        self._drag_mode = None  # None | 'scrub' | 'clip' | 'trim_left' | 'trim_right' | 'volume'
        self._drag_clip = None
        self._drag_track = None       # track the clip currently lives on mid-drag
        self._drag_origin_track = None  # track it started the drag on
        self._drag_offset_frames = 0
        self._drag_start_frame = 0
        # Recorded original trim/position values for 'trim_left'/'trim_right'
        # drags -- trim deltas are always applied against these originals
        # (not accumulated per-move-event) to avoid drift, and they're what
        # mouseReleaseEvent compares the final values against to decide
        # whether a TrimClipCommand is actually needed.
        self._drag_orig_trim_in = 0.0
        self._drag_orig_trim_out = 0.0
        self._drag_orig_end_frame = 0
        # Recorded original volume_points for a 'volume' drag, compared
        # against the live-mutated value on release to decide whether a
        # SetClipVolumeCommand is actually needed.
        self._drag_orig_volume_points = None
        # Which of the current volume-editing clip's points (index into
        # volume_points) a 'volume' drag is moving -- None means the phase-1
        # flat-line case where the two endpoints move together instead of a
        # specific point.
        self._drag_point_index = None

        self.setMouseTracking(True)
        self.setMinimumHeight(TRACK_HEIGHT)
        # Needed to actually receive keyPressEvent (Delete key on a
        # selected clip) -- widgets default to no keyboard focus.
        self.setFocusPolicy(Qt.StrongFocus)
        # Lets files dragged in from the OS file manager land on the
        # timeline -- see dragEnterEvent/dropEvent.
        self.setAcceptDrops(True)

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

    @property
    def selected_clip(self):
        return self._selected_clip

    @selected_clip.setter
    def selected_clip(self, clip):
        if clip is self._selected_clip:
            return
        self._selected_clip = clip
        self.selectionChanged.emit(clip is not None)

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

    def _volume_indicator_text(self, clip):
        """The passive percentage-badge text for `clip`, shown next to the
        volume icon for a flat (single-gain) clip so its level is
        comparable at a glance without singling out the default case (see
        volume_indicator_rect_for) -- empty for a clip with a multi-point
        envelope, where no single percentage represents the whole curve
        and showing one (e.g. just the gain at fraction 0) would be
        misleading rather than informative."""
        if not self._volume_is_flat(clip):
            return ""
        gain = clip.volume_points[0][1] if clip.volume_points else VOLUME_GAIN_UNITY
        return f"{int(round(gain * 100))}%"

    def volume_indicator_rect_for(self, clip, clip_rect):
        """A small square pinned to `clip_rect`'s top-left (with a small
        margin) that toggles volume-editing mode for that clip, widened to
        fit a trailing percentage badge for a flat clip (the passive
        indicator; see _volume_indicator_text), or a second "has a curve"
        icon once the clip has a multi-point envelope, where there's no
        single percentage to show instead (see _paint_clip). Callers pass
        the *sticky* x-adjusted rect (see _paint_clip) so it stays visible
        even when the clip's real left edge is scrolled off-screen -- same
        reasoning as the sticky name label."""
        size = VOLUME_ICON_SIZE
        margin = 3
        gap = 2
        icon_rect = QRect(clip_rect.left() + margin, clip_rect.top() + margin, size, size)
        text = self._volume_indicator_text(clip)
        fm = QFontMetrics(self.font())
        # Tall enough for the text (icon square alone may be shorter than
        # the font's line height), centered on the icon's own vertical
        # middle so widening the badge doesn't shift the icon up/down --
        # kept consistent whether or not there's text, so icon-only and
        # icon+text badges still align across different clips.
        height = max(size, fm.height())
        width = size
        if not self._volume_is_flat(clip):
            width += gap + size
        elif text:
            text_w = fm.boundingRect(text).width()
            pad = 4
            width += gap + text_w + pad
        top = icon_rect.top() - (height - size) // 2
        return QRect(icon_rect.left(), top, width, height)

    def _sticky_volume_indicator_rect(self, clip, clip_rect, sticky_left):
        """volume_indicator_rect_for(), but positioned using the sticky x
        the clip name label already uses (see _paint_clip) rather than
        clip_rect.left() -- so it stays visible when the clip's actual
        left edge is scrolled off-screen -- clamped so it never runs past
        the clip's own right edge. Shared by painting and hit-testing so
        they can never disagree."""
        sticky_rect = QRect(max(clip_rect.left(), sticky_left), clip_rect.top(),
                             clip_rect.width(), clip_rect.height())
        indicator_rect = self.volume_indicator_rect_for(clip, sticky_rect)
        clamped_left = min(indicator_rect.left(), clip_rect.right() - indicator_rect.width())
        return QRect(clamped_left, indicator_rect.top(), indicator_rect.width(), indicator_rect.height())

    def volume_apply_rect_for(self, clip_rect):
        """Where the "apply" (checkmark) icon sits, replacing the plain
        volume icon while the clip is in volume-editing mode."""
        size = VOLUME_ICON_SIZE
        margin = 3
        return QRect(clip_rect.left() + margin, clip_rect.top() + margin, size, size)

    def volume_cancel_rect_for(self, clip_rect):
        """Where the "cancel" (X) icon sits, immediately to the right of
        the apply icon."""
        apply_rect = self.volume_apply_rect_for(clip_rect)
        return QRect(apply_rect.right() + 2, apply_rect.top(), apply_rect.width(), apply_rect.height())

    def _sticky_volume_action_rects(self, clip_rect, sticky_left):
        """(apply_rect, cancel_rect), positioned using the sticky x the
        name label uses (see _paint_clip) and clamped as a pair -- rather
        than independently -- so a narrow clip can't make them overlap
        each other while still never running past the clip's right edge.
        Shared by painting and hit-testing so they can never disagree."""
        sticky_rect = QRect(max(clip_rect.left(), sticky_left), clip_rect.top(),
                             clip_rect.width(), clip_rect.height())
        apply_rect = self.volume_apply_rect_for(sticky_rect)
        cancel_rect = self.volume_cancel_rect_for(sticky_rect)
        overflow = cancel_rect.right() - clip_rect.right()
        if overflow > 0:
            apply_rect = apply_rect.translated(-overflow, 0)
            cancel_rect = cancel_rect.translated(-overflow, 0)
        return apply_rect, cancel_rect

    def _clip_rect_for(self, track_idx, clip):
        """The on-screen rect _paint_clip() draws `clip` into -- shared
        with mouse hit-testing so icon/line hit-tests always agree with
        what's actually drawn."""
        x0 = self.frame_to_x(clip.start_frame)
        x1 = self.frame_to_x(clip.end_frame)
        return QRect(x0, self.track_y(track_idx) + 4, max(2, x1 - x0), TRACK_HEIGHT - 8)

    def _volume_gain_to_y(self, clip_rect, gain):
        """Maps a gain value to a y coordinate within `clip_rect`: 100%
        (unity) sits at the vertical middle, 0% at the bottom, and
        VOLUME_GAIN_MAX at the top -- piecewise-linear across those two
        segments (not one linear function over the whole range) so 100%
        always lands exactly at the middle regardless of where the max is
        set. The inverse of _volume_y_to_gain()."""
        top = clip_rect.top()
        bottom = clip_rect.bottom()
        mid = (top + bottom) / 2.0
        if gain >= VOLUME_GAIN_UNITY:
            t = (gain - VOLUME_GAIN_UNITY) / (VOLUME_GAIN_MAX - VOLUME_GAIN_UNITY)
            t = max(0.0, min(1.0, t))
            return mid - t * (mid - top)
        t = gain / VOLUME_GAIN_UNITY
        t = max(0.0, min(1.0, t))
        return bottom - t * (bottom - mid)

    def _volume_y_to_gain(self, clip_rect, y):
        """Inverse of _volume_gain_to_y()."""
        top = clip_rect.top()
        bottom = clip_rect.bottom()
        mid = (top + bottom) / 2.0
        if y <= mid:
            if mid == top:
                return VOLUME_GAIN_MAX
            t = (mid - y) / (mid - top)
            return VOLUME_GAIN_UNITY + t * (VOLUME_GAIN_MAX - VOLUME_GAIN_UNITY)
        if bottom == mid:
            return 0.0
        t = (y - mid) / (bottom - mid)
        return VOLUME_GAIN_UNITY - t * VOLUME_GAIN_UNITY

    def _volume_line_y_for(self, clip, clip_rect):
        """The clip's current gain line, as a y coordinate -- only correct
        for the flat (<=2-point) case, where every point shares the same
        gain; used for the legacy "drag both endpoints together" hit-test
        and hover. For an actual multi-point envelope, use
        _volume_curve_y_at_x/_volume_point_hit_test instead."""
        gain = clip.volume_points[0][1] if clip.volume_points else VOLUME_GAIN_UNITY
        return self._volume_gain_to_y(clip_rect, gain)

    def _volume_is_flat(self, clip):
        """True while `clip` is still in the legacy flat-line case (only
        the two default endpoint points, no interior bend points added
        yet) -- the one concept behind every '<=2 points' check across
        hit-testing, hover, painting and the percentage dialog, so they
        can't drift out of sync with each other."""
        return len(clip.volume_points) <= 2

    def _x_to_clip_fraction(self, clip_rect, x):
        """Maps an x coordinate to a fraction of the clip's currently
        *played* (on-screen) window, in [0, 1] -- convert through
        clip.played_fraction_to_extent_fraction() before storing into or
        evaluating clip.volume_points, which are relative to the clip's
        permanent extent instead (see AudioClip.volume_points)."""
        width = clip_rect.width()
        if width <= 0:
            return 0.0
        return max(0.0, min(1.0, (x - clip_rect.left()) / float(width)))

    def _volume_point_screen_pos(self, clip, clip_rect, point):
        """Maps a (extent_fraction, gain) volume_points entry to its
        on-screen (x, y), shared by painting and hit-testing so they can't
        disagree. Returns None if the point currently falls outside the
        clip's played (trimmed) window -- it still exists in
        clip.volume_points, just isn't visible/interactable until the clip
        is trimmed back out to reveal it (see
        AudioClip.extent_fraction_to_played_fraction)."""
        extent_frac, gain = point
        played_frac = clip.extent_fraction_to_played_fraction(extent_frac)
        if played_frac is None:
            return None
        x = clip_rect.left() + played_frac * clip_rect.width()
        y = self._volume_gain_to_y(clip_rect, gain)
        return x, y

    def _volume_curve_y_at_x(self, clip, clip_rect, x):
        """The drawn curve's y coordinate at screen x -- evaluates the
        shared envelope function at that x's fraction (converted from a
        played-window fraction to the envelope's own extent-relative
        fraction), same as the paint code and mixdown.py."""
        played_frac = self._x_to_clip_fraction(clip_rect, x)
        extent_frac = clip.played_fraction_to_extent_fraction(played_frac)
        gain = volume_envelope.evaluate(clip.volume_points, extent_frac)
        return self._volume_gain_to_y(clip_rect, gain)

    def _volume_point_hit_test(self, clip, clip_rect, pos):
        """Index into clip.volume_points whose screen position is within
        hit-test tolerance of `pos`, or None. Points currently trimmed out
        of view (see _volume_point_screen_pos) are never matched -- they
        aren't drawn, so they shouldn't be grabbable either."""
        for i, point in enumerate(clip.volume_points):
            screen_pos = self._volume_point_screen_pos(clip, clip_rect, point)
            if screen_pos is None:
                continue
            px, py = screen_pos
            if abs(pos.x() - px) <= HANDLE_PX and abs(pos.y() - py) <= HANDLE_PX:
                return i
        return None

    def _volume_curve_hit_test(self, clip, clip_rect, pos):
        """True if `pos` sits within HANDLE_PX (vertically) of the drawn
        curve at that x, and within the clip's horizontal span -- used for
        double-click-to-insert-a-point."""
        if not (clip_rect.left() <= pos.x() <= clip_rect.right()):
            return False
        y = self._volume_curve_y_at_x(clip, clip_rect, pos.x())
        return abs(pos.y() - y) <= HANDLE_PX

    def _volume_line_hover(self, pos):
        """True if `pos` sits over something draggable on the currently
        volume-editing clip: an individual bend point once the envelope has
        more than the two default endpoints, or (in the still-flat case)
        anywhere along the flat line -- used for the hover cursor so it
        matches mousePressEvent's own drag hit-test."""
        clip = self.volume_editing_clip
        if clip is None:
            return False
        for track_idx, track in enumerate(self.tracks):
            if clip in track.clips:
                clip_rect = self._clip_rect_for(track_idx, clip)
                if not (clip_rect.left() <= pos.x() <= clip_rect.right()):
                    return False
                if not self._volume_is_flat(clip):
                    return self._volume_point_hit_test(clip, clip_rect, pos) is not None
                line_y = self._volume_line_y_for(clip, clip_rect)
                return abs(pos.y() - line_y) <= HANDLE_PX
        return False

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
            # Hue-complement of the accent, elevated toward the foreground --
            # deliberately not built from clip_fill/clip_border/clip_text (all
            # accent-derived) so the volume-editing line reads as clearly
            # distinct from everything else drawn on a clip, in either theme.
            'volume_line': elevate(_complementary(accent), 0.2),
            # Same hue as volume_line, just turned up (more saturated/
            # brighter) -- the selected-bend-point ring, so it reads as
            # "this point, emphasized" rather than an unrelated highlight
            # color competing with the curve/points it's drawn around.
            'volume_point_selected': _intensify(elevate(_complementary(accent), 0.2)),
            # Segment of a clip's waveform that's been driven past +/-100%
            # by its own gain (i.e. would itself clip on mixdown) -- reuses
            # the same warning-red base as button_muted_bg (just mixed less
            # toward canvas_bg, so it reads as more saturated/alarming than
            # the muted-button tint) rather than a fresh hardcoded color.
            'waveform_clipped': _mix(QColor(190, 70, 70), canvas_bg, 0.1),
            # Background behind the volume readout/badge text -- opposite
            # brightness from clip_text (the color actually drawn on top of
            # it, not just canvas_bg/is_dark) so the text stays legible
            # whichever theme makes clip_text light or dark.
            'readout_bg': QColor(0, 0, 0, 170) if accent_text.lightness() >= 128 else QColor(255, 255, 255, 170),
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
            self._paint_waveform(painter, clip_rect, self._trimmed_peaks(clip), theme,
                                  visible_left, visible_right, clip)

        # The gain line (while editing) is drawn now (right above the
        # waveform, below everything else) rather than after the icons/
        # title/readout below, so it never covers them -- the percentage
        # readout is drawn last of all, see the end of this method, so it's
        # always on top even of the icons/title.
        if clip is self.volume_editing_clip:
            self._paint_volume_shading(painter, clip_rect, clip, theme)

        # Keep the label (and volume icon) pinned to the left edge of the
        # *visible* viewport while the clip is on screen, rather than the
        # clip's own (possibly scrolled-off) left edge -- but never let it
        # slide past the clip's right edge. Uses the actual scroll
        # viewport, not the paintEvent dirty rect (visible_left/right) --
        # see visible_x_range()'s docstring for why that would ghost.
        sticky_left, sticky_right = self.visible_x_range()
        if clip is self.volume_editing_clip:
            # Editing this clip's volume -- the single volume icon is
            # replaced by explicit apply (check)/cancel (X) icons so the
            # user has an unambiguous way to commit or discard the edit
            # (Escape/click-elsewhere still apply/cancel too, see
            # keyPressEvent/mousePressEvent, but the icons make both
            # actions discoverable).
            apply_rect, cancel_rect = self._sticky_volume_action_rects(clip_rect, sticky_left)
            apply_icon = _krita_icon("dialog-ok-apply", QStyle.SP_DialogApplyButton)
            cancel_icon = _krita_icon("dialog-cancel", QStyle.SP_DialogCancelButton)
            painter.save()
            painter.setClipRect(clip_rect)
            apply_icon.paint(painter, apply_rect)
            cancel_icon.paint(painter, cancel_rect)
            painter.restore()
            icons_right_edge = cancel_rect.right()
        else:
            indicator_rect = self._sticky_volume_indicator_rect(clip, clip_rect, sticky_left)
            icon_top = indicator_rect.top() + (indicator_rect.height() - VOLUME_ICON_SIZE) // 2
            icon_rect = QRect(indicator_rect.left(), icon_top, VOLUME_ICON_SIZE, VOLUME_ICON_SIZE)
            # The passive badge always shows the same glyph (not the
            # high/low/muted-bucket swap the apply/cancel-icon slot doesn't
            # need to make either) -- the percentage text beside it already
            # carries the exact level, so the icon here is just a fixed
            # "this is the volume control" marker, tinted to match that
            # text (theme['clip_text']) rather than the icon theme's own
            # fixed color.
            volume_icon = _krita_icon("audio-volume-high", QStyle.SP_MediaVolume)
            icon_pixmap = _tinted_icon_pixmap(volume_icon, VOLUME_ICON_SIZE, theme['clip_text'])
            multi_point = not self._volume_is_flat(clip)
            text = self._volume_indicator_text(clip)
            painter.save()
            painter.setClipRect(clip_rect)
            # Passive (not-in-edit-mode) gain indicator: same small
            # background-behind-text treatment as the in-edit-mode readout,
            # but beside the icon and shown unconditionally (including at
            # 100%) -- so a clip's level is comparable against others' at a
            # glance without entering edit mode.
            painter.fillRect(indicator_rect, theme['readout_bg'])
            painter.drawPixmap(icon_rect, icon_pixmap)
            if multi_point:
                # A flat clip shows its exact gain as a percentage instead
                # (see below) -- once bend points exist, no single number
                # represents the whole curve, so this "has a curve" glyph
                # stands in for it at a glance.
                curve_icon = _krita_icon("curve-preset-s", QStyle.SP_FileDialogDetailedView)
                curve_pixmap = _tinted_icon_pixmap(curve_icon, VOLUME_ICON_SIZE, theme['clip_text'])
                curve_rect = QRect(icon_rect.right() + 2, icon_rect.top(), VOLUME_ICON_SIZE, VOLUME_ICON_SIZE)
                painter.drawPixmap(curve_rect, curve_pixmap)
            elif text:
                text_rect = QRect(icon_rect.right() + 2, indicator_rect.top(),
                                   indicator_rect.right() - icon_rect.right() - 2, indicator_rect.height())
                painter.setPen(theme['clip_text'])
                painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, text)
            painter.restore()
            icons_right_edge = indicator_rect.right()

        painter.setPen(theme['clip_text'])
        # The name label must shift right of the volume icon(s) (plus a
        # small gap) so they never overlap, but clip drawing to clip_rect so
        # a long name can't bleed into the next clip.
        text_left = icons_right_edge + 4
        text_left = min(text_left, clip_rect.right() - 4)
        text_rect = QRect(text_left, clip_rect.top() + 2,
                           max(0, clip_rect.right() - 4 - text_left), clip_rect.height() - 2)
        painter.save()
        painter.setClipRect(clip_rect)
        painter.drawText(text_rect, Qt.AlignTop | Qt.AlignLeft, clip.name)
        painter.restore()

        if clip is self.volume_editing_clip:
            self._paint_volume_readout(painter, clip_rect, clip, theme, sticky_left, sticky_right)

    def _paint_volume_shading(self, painter, clip_rect, clip, theme):
        """Draws the volume-editing gain curve -- only called while this
        clip is the one currently in volume-editing mode (see
        self.volume_editing_clip). Called early in _paint_clip (right
        after the waveform, before the icons/title/percentage readout) so
        none of those ever end up underneath it.

        Samples volume_envelope.evaluate() every few px across clip_rect's
        width to draw a smooth curve (a straight flat/sloped line falls out
        of that automatically for the <=2-point case), then draws a small
        filled circle at each actual bend point on top so they're visible
        and individually targetable (see _volume_point_hit_test).

        No longer paints a dark overlay over the attenuated region -- the
        waveform itself is now drawn pre-scaled by the clip's (live, while
        dragging) gain in _paint_waveform, so the shrunk/grown amplitude
        *is* the indication, continuously, and the curve stays only as the
        drag handle / precise position reference on top of it."""
        points = clip.volume_points or [(0.0, VOLUME_GAIN_UNITY), (1.0, VOLUME_GAIN_UNITY)]

        painter.save()
        painter.setClipRect(clip_rect)
        # A distinct (accent-complementary) color, not clip_text, so the
        # curve reads as clearly separate from the name/percentage text and
        # the clip's own border/fill.
        painter.setPen(QPen(theme['volume_line'], 2))
        step = 3
        left = clip_rect.left()
        right = clip_rect.right()
        width = clip_rect.width()
        prev = None
        x = left
        while True:
            played_frac = (x - left) / float(width) if width else 0.0
            extent_frac = clip.played_fraction_to_extent_fraction(max(0.0, min(1.0, played_frac)))
            gain = volume_envelope.evaluate(points, extent_frac)
            cur = QPoint(x, int(round(self._volume_gain_to_y(clip_rect, gain))))
            if prev is not None:
                painter.drawLine(prev, cur)
            prev = cur
            if x >= right:
                break
            x = min(x + step, right)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(theme['volume_line']))
        for point in points:
            screen_pos = self._volume_point_screen_pos(clip, clip_rect, point)
            if screen_pos is None:
                # Currently trimmed out of view -- still in clip.volume_points,
                # just not drawn/targetable until the clip is trimmed back
                # out to reveal it (see _volume_point_screen_pos).
                continue
            px, py = screen_pos
            painter.drawEllipse(QPoint(int(round(px)), int(round(py))),
                                 VOLUME_POINT_RADIUS, VOLUME_POINT_RADIUS)

        # The selected point (see selected_volume_point_index, set by
        # clicking/grabbing a point -- only meaningful once there's more
        # than one point to select between) gets an extra ring in an
        # intensified version of the points' own color (not the generic
        # accent-based selection color), on top of its fill, so it reads
        # as "this point, turned up" rather than a color unrelated to the
        # curve -- and is clearly distinguishable from every other point
        # for Delete-key/right-click removal.
        if (not self._volume_is_flat(clip)
                and self.selected_volume_point_index is not None
                and self.selected_volume_point_index < len(points)):
            sel_screen_pos = self._volume_point_screen_pos(clip, clip_rect, points[self.selected_volume_point_index])
            if sel_screen_pos is not None:
                sel_px, sel_py = sel_screen_pos
                painter.setPen(QPen(theme['volume_point_selected'], 2))
                painter.setBrush(Qt.NoBrush)
                ring_radius = VOLUME_POINT_RADIUS + 2
                painter.drawEllipse(QPoint(int(round(sel_px)), int(round(sel_py))), ring_radius, ring_radius)
        painter.restore()

    def _volume_readout_rect_for(self, clip, clip_rect, sticky_left, sticky_right):
        """Where the single, bottom-pinned percentage-gain readout's
        background sits -- used only for the flat (<=2-point) case,
        regardless of where the gain line itself sits, centered within
        whatever portion of the clip is actually on screen. Shared by
        painting and double-click hit-testing so they can never disagree.
        Uses the widget's own font, not a live QPainter's (none is
        available outside paintEvent), which matches it closely enough for
        hit-testing purposes."""
        gain = clip.volume_points[0][1] if clip.volume_points else 1.0
        text = f"{int(round(gain * 100))}%"

        visible_left = max(clip_rect.left(), sticky_left)
        visible_right = min(clip_rect.right(), sticky_right)
        center_x = (visible_left + visible_right) / 2.0

        fm = QFontMetrics(self.font())
        text_size = fm.boundingRect(text)
        pad_x, pad_y = 4, 2
        bg_rect = QRect(0, 0, text_size.width() + pad_x * 2, fm.height() + pad_y * 2)
        bg_rect.moveCenter(QPoint(int(round(center_x)), clip_rect.bottom() - bg_rect.height() // 2 - 2))
        if bg_rect.left() < visible_left:
            bg_rect.moveLeft(int(visible_left))
        if bg_rect.right() > visible_right:
            bg_rect.moveRight(int(visible_right))
        return bg_rect

    def _volume_point_readout_rect_for(self, clip, clip_rect, point_index):
        """Where an individual bend point's own percentage-gain readout
        sits -- just below that point's (x, y) on the curve, rather than
        pinned to the clip's bottom edge, so the shape of a multi-point
        envelope is readable at a glance. Used once the clip has more than
        the two default endpoint points (see _volume_readout_rects).
        Returns None if the point is currently trimmed out of view (see
        _volume_point_screen_pos) -- nothing to show a readout for.

        Unlike the flat-case bottom-pinned readout, this one is never
        clamped to stay within the visible *viewport* -- once there's more
        than one readout on screen at a time, pinning each independently
        would make them drift relative to their own points as the
        timeline scrolls; it's clearer for a readout to simply scroll off
        with its point like any other on-canvas label. It's still clamped
        to stay within the *clip's own* rect horizontally, though -- left
        unclamped, a point sitting exactly at the clip's left/right edge
        (e.g. the default endpoints) would have its readout's other half
        sliced off by _paint_clip's clip_rect clipping, reading as a
        stray, barely-legible sliver instead of a full label."""
        point = clip.volume_points[point_index]
        screen_pos = self._volume_point_screen_pos(clip, clip_rect, point)
        if screen_pos is None:
            return None
        px, py = screen_pos
        text = f"{int(round(point[1] * 100))}%"

        fm = QFontMetrics(self.font())
        text_size = fm.boundingRect(text)
        pad_x, pad_y = 4, 2
        bg_rect = QRect(0, 0, text_size.width() + pad_x * 2, fm.height() + pad_y * 2)
        below_y = py + VOLUME_POINT_RADIUS + 2 + bg_rect.height() / 2.0
        bg_rect.moveCenter(QPoint(int(round(px)), int(round(below_y))))
        if bg_rect.left() < clip_rect.left():
            bg_rect.moveLeft(clip_rect.left())
        if bg_rect.right() > clip_rect.right():
            bg_rect.moveRight(clip_rect.right())
        if bg_rect.bottom() > clip_rect.bottom():
            bg_rect.moveBottom(clip_rect.bottom())
        return bg_rect

    def _volume_readout_rects(self, clip, clip_rect, sticky_left, sticky_right):
        """The set of (point_index, bg_rect) percentage readouts to show
        for this clip while volume-editing: a single bottom-pinned one
        representing both endpoints together in the flat (<=2-point) case
        (matching phase 1), or one per actual *visible* point (not sticky
        to the viewport at all -- see _volume_point_readout_rect_for; a
        point currently trimmed out of view gets no readout) once any
        interior bend point exists. Shared by painting and double-click
        hit-testing."""
        points = clip.volume_points
        if self._volume_is_flat(clip):
            return [(0, self._volume_readout_rect_for(clip, clip_rect, sticky_left, sticky_right))]
        result = []
        for i in range(len(points)):
            rect = self._volume_point_readout_rect_for(clip, clip_rect, i)
            if rect is not None:
                result.append((i, rect))
        return result

    def _paint_volume_readout(self, painter, clip_rect, clip, theme, sticky_left, sticky_right):
        """Draws the percentage-gain readout(s) -- a small background
        behind just the text (not the full clip width) so it stays legible
        over the waveform/either theme. Called last of all in _paint_clip
        so it's always on top, even of the icons/title."""
        points = clip.volume_points
        painter.save()
        painter.setClipRect(clip_rect)
        painter.setPen(theme['clip_text'])
        for idx, bg_rect in self._volume_readout_rects(clip, clip_rect, sticky_left, sticky_right):
            gain = points[idx][1] if points else 1.0
            text = f"{int(round(gain * 100))}%"
            painter.fillRect(bg_rect, theme['readout_bg'])
            painter.drawText(bg_rect, Qt.AlignCenter, text)
        painter.restore()

    def _trimmed_peaks(self, clip):
        """`clip.peaks` is analyzed once over the *full source file* at
        import time and never re-analyzed on trim (trimming must not
        re-decode audio) -- so slice out just the sub-range of it that
        corresponds to the clip's current trim_in_sec/trim_out_sec window
        before handing it to _paint_waveform.

        Fractions must be taken against `full_source_duration_sec` (the true,
        never-overridden duration `peaks` spans) rather than
        `source_duration_sec`, which post-split is instead a trim-ceiling
        capped at the cut point and would otherwise make the ceiling itself
        cancel out of the fraction and yield the whole `peaks` array.
        `source_duration_sec` is still used here as the *absolute* end-of-clip position (in
        original-source seconds) that trim_out_sec is measured back from."""
        peaks = clip.peaks
        full = clip.full_source_duration_sec
        if not peaks or full <= 0:
            return peaks
        frac_start = clip.trim_in_sec / full
        frac_end = (clip.source_duration_sec - clip.trim_out_sec) / full
        n = len(peaks)
        start_idx = max(0, min(n, int(frac_start * n)))
        end_idx = max(start_idx, min(n, int(frac_end * n)))
        return peaks[start_idx:end_idx]

    def _paint_waveform(self, painter, clip_rect, peaks, theme, visible_left, visible_right, clip=None):
        """Draws `peaks`, each value pre-multiplied by the gain envelope
        (volume_envelope.evaluate() at that bucket's fractional position,
        converted from a played-window fraction to `clip`'s envelope-
        extent-relative fraction -- see AudioClip.played_fraction_to_extent_fraction)
        -- the same function mixdown.py applies to the actual audio -- so a
        clip's waveform visibly flattens/grows/bends with its own volume
        envelope, both passively (resting gain) and, since callers just
        read the clip's current volume_points on every repaint, live while
        a volume-editing drag is in progress (see mouseMoveEvent's 'volume'
        drag mode). Any bucket driven past +/-100% by the gain is clamped
        for drawing but rendered in a distinct color, so amplification past
        what the display (and, on mixdown, the actual output) can represent
        without clipping stays visible rather than silently flattening at
        the clip_rect edge."""
        n = len(peaks)
        if n == 0:
            return
        points = clip.volume_points if clip is not None and clip.volume_points else [(0.0, VOLUME_GAIN_UNITY), (1.0, VOLUME_GAIN_UNITY)]
        step_px = clip_rect.width() / float(n)
        mid_y = clip_rect.center().y()
        half_h = clip_rect.height() / 2 - 4

        def gain_at_index(i):
            played_frac = i / float(n - 1) if n > 1 else 0.0
            extent_frac = clip.played_fraction_to_extent_fraction(played_frac) if clip is not None else played_frac
            return volume_envelope.evaluate(points, extent_frac)

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

        normal_pen = QPen(theme['waveform'], 1)
        clipped_pen = QPen(theme['waveform_clipped'], 1)

        def draw_segment(x, lo, hi, gain):
            lo_g, hi_g = lo * gain, hi * gain
            clipped = hi_g > 1.0 or lo_g < -1.0
            painter.setPen(clipped_pen if clipped else normal_pen)
            lo_c = max(-1.0, min(1.0, lo_g))
            hi_c = max(-1.0, min(1.0, hi_g))
            painter.drawLine(int(x), int(mid_y - hi_c * half_h), int(x), int(mid_y - lo_c * half_h))

        if step_px >= 1.0:
            # Zoomed in enough that each bucket gets its own pixel column
            # (or more) -- draw them one-for-one, same as before.
            for i in range(start_idx, end_idx):
                lo, hi = peaks[i]
                draw_segment(clip_rect.left() + i * step_px, lo, hi, gain_at_index(i))
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
            draw_segment(clip_rect.left() + i * step_px, lo, hi, gain_at_index(i + len(group) // 2))
            i += buckets_per_px

    def _paint_playhead(self, painter):
        x = self.frame_to_x(self.current_frame)
        painter.setPen(QPen(QColor(255, 80, 80), 2))
        painter.drawLine(x, 0, x, self.height())

    @staticmethod
    def _prev_clip_end(track, clip, orig_start_frame):
        """The end_frame of whichever other clip on `track` sits immediately
        before `clip`'s original (pre-drag) start -- trimming the left edge
        can't pull `clip`'s start_frame back past this. 0 if there is none."""
        end = 0
        for c in track.clips:
            if c is clip:
                continue
            if c.end_frame <= orig_start_frame and c.end_frame > end:
                end = c.end_frame
        return end

    @staticmethod
    def _next_clip_start(track, clip, orig_end_frame):
        """The start_frame of whichever other clip on `track` sits
        immediately after `clip`'s original (pre-drag) end -- trimming the
        right edge can't push `clip`'s end_frame past this. None if there
        is none."""
        start = None
        for c in track.clips:
            if c is clip:
                continue
            if c.start_frame >= orig_end_frame and (start is None or c.start_frame < start):
                start = c.start_frame
        return start

    @staticmethod
    def _closer_trim_edge(pos_x, x0, x1):
        """Which edge ('trim_left'/'trim_right'/None) `pos_x` is within
        HANDLE_PX of -- picking whichever of the two is *closer* rather
        than always preferring the left edge, so a clip narrower than
        2*HANDLE_PX (both edges' hit-zones overlapping, e.g. a short
        sliver left over from a split) doesn't make the right edge
        permanently unreachable just because the left check used to run
        first. Shared by mousePressEvent's hit-test and _trim_handle_at's
        hover-cursor check so they can never disagree."""
        d0 = abs(pos_x - x0)
        d1 = abs(pos_x - x1)
        if d0 <= HANDLE_PX and d0 <= d1:
            return 'trim_left'
        if d1 <= HANDLE_PX:
            return 'trim_right'
        return None

    def _trim_handle_at(self, pos):
        """Returns 'trim_left'/'trim_right' if `pos` sits within HANDLE_PX
        of the selected clip's left/right on-screen edge, else None -- used
        both for mousePressEvent hit-testing and for the hover cursor.
        Always None while the selected clip is the one being volume-edited
        (trimming is disabled for it -- see mousePressEvent), so the resize
        cursor never hints an edge-drag that wouldn't actually happen."""
        clip = self.selected_clip
        if clip is None or clip is self.volume_editing_clip:
            return None
        track_idx = self.track_index_at_y(pos.y())
        if track_idx < 0 or clip not in self.tracks[track_idx].clips:
            return None
        x0 = self.frame_to_x(clip.start_frame)
        x1 = self.frame_to_x(clip.end_frame)
        return self._closer_trim_edge(pos.x(), x0, x1)

    def _clip_edge_at(self, track, pos_x):
        """Returns (clip, edge) for whichever clip on `track` has a
        start/end on-screen edge within HANDLE_PX of `pos_x`, picking the
        globally closest edge if more than one clip's tolerance zone
        covers `pos_x`. Unlike `track.clip_at_frame`, this has the same
        pixel tolerance as the hover cursor (`_trim_handle_at`), so a click
        just outside a clip's strict frame bounds -- but still within the
        cursor's hover zone -- can grab that edge instead of missing the
        clip entirely. Returns None if no clip's edge is within range."""
        best = None
        best_dist = None
        for c in track.clips:
            x0 = self.frame_to_x(c.start_frame)
            x1 = self.frame_to_x(c.end_frame)
            edge = self._closer_trim_edge(pos_x, x0, x1)
            if edge is None:
                continue
            dist = abs(pos_x - (x0 if edge == 'trim_left' else x1))
            if best_dist is None or dist < best_dist:
                best = (c, edge)
                best_dist = dist
        return best

    # --------------------------------------------------------------- input
    def mousePressEvent(self, event):
        self.setFocus()
        if event.button() == Qt.RightButton:
            # Right-click never starts a drag -- contextMenuEvent (fired
            # separately, on release) is the sole handler for right-click
            # actions (deleting a clip, or removing a bend point while
            # volume-editing), so nothing here should pre-empt it by
            # selecting/dragging whatever's under the cursor first.
            return
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
            edge_hit = None
            if clip is None:
                # Strict frame containment missed -- but the hover cursor
                # uses a pixel tolerance around clip edges (HANDLE_PX), so
                # a click just outside a clip's bounds can still be a valid
                # edge grab; check for that before falling through to the
                # deselect/scrub fallback below.
                # Deliberately still resolves to the clip currently being
                # volume-edited, if that's the nearest edge -- excluding it
                # here used to mean a click near its right endpoint (which
                # sits exactly at the strict frame boundary, just like a
                # trim handle) fell through to "clip is None" and
                # deselected/scrubbed instead of reaching the point hit-
                # test below. Trimming/moving that clip is still fully
                # disabled (see the "no drag while editing" guard further
                # down); this only affects which clip a near-edge click
                # resolves to, not what dragging it can do.
                edge_hit = self._clip_edge_at(track, pos.x())
                if edge_hit is not None:
                    clip, _ = edge_hit
            if clip is not None:
                clip_rect = self._clip_rect_for(track_idx, clip)

                # If this clip is currently in volume-editing mode, a click
                # near its curve starts a volume drag -- checked before the
                # icon and normal clip hit-tests. Once the envelope has more
                # than the two default endpoints, only an actual bend point
                # (hit-tested individually) can be grabbed, and only that
                # point's gain moves; in the still-flat (<=2-point) case,
                # any point along the line grabs both endpoints together
                # (phase 1's behavior).
                if self.volume_editing_clip is clip:
                    if not self._volume_is_flat(clip):
                        point_idx = self._volume_point_hit_test(clip, clip_rect, pos)
                        if point_idx is not None:
                            self.selected_clip = clip
                            self.selected_volume_point_index = point_idx
                            self._drag_mode = 'volume'
                            self._drag_clip = clip
                            self._drag_track = track
                            self._drag_point_index = point_idx
                            self._drag_orig_volume_points = list(clip.volume_points)
                            self.update()
                            return
                        # Missed every point -- deselect rather than leave
                        # a stale selection border on a point the user
                        # isn't interacting with anymore.
                        self.selected_volume_point_index = None
                    else:
                        line_y = self._volume_line_y_for(clip, clip_rect)
                        if abs(pos.y() - line_y) <= HANDLE_PX:
                            self.selected_clip = clip
                            self._drag_mode = 'volume'
                            self._drag_clip = clip
                            self._drag_track = track
                            self._drag_point_index = None
                            self._drag_orig_volume_points = list(clip.volume_points)
                            self.update()
                            return

                sticky_left, _sticky_right = self.visible_x_range()
                if self.volume_editing_clip is clip:
                    apply_rect, cancel_rect = self._sticky_volume_action_rects(clip_rect, sticky_left)
                    if apply_rect.contains(pos):
                        self.selected_clip = clip
                        self._exit_volume_editing(revert=False)
                        self.update()
                        return
                    if cancel_rect.contains(pos):
                        self.selected_clip = clip
                        self._exit_volume_editing(revert=True)
                        self.update()
                        return
                else:
                    indicator_rect = self._sticky_volume_indicator_rect(clip, clip_rect, sticky_left)
                    if indicator_rect.contains(pos):
                        self.selected_clip = clip
                        self._enter_volume_editing(clip)
                        self.update()
                        return

                if self.volume_editing_clip is not None and self.volume_editing_clip is not clip:
                    self._exit_volume_editing_with_prompt()
                self.selected_clip = clip
                if self.volume_editing_clip is clip:
                    # Every other click/drag while this clip is being
                    # volume-edited is handled above (bend points, apply/
                    # cancel icons) -- a click that misses all of those
                    # just keeps the clip selected, without starting a
                    # move/trim/split drag. Repositioning, resizing or
                    # splitting the clip out from under an in-progress
                    # envelope edit would be confusing at best.
                    self.update()
                    return
                x0 = self.frame_to_x(clip.start_frame)
                x1 = self.frame_to_x(clip.end_frame)
                edge = self._closer_trim_edge(pos.x(), x0, x1)
                self._drag_mode = edge if edge is not None else 'clip'
                self._drag_clip = clip
                self._drag_track = track
                self._drag_origin_track = track
                self._drag_offset_frames = frame - clip.start_frame
                self._drag_start_frame = clip.start_frame
                self._drag_orig_trim_in = clip.trim_in_sec
                self._drag_orig_trim_out = clip.trim_out_sec
                self._drag_orig_end_frame = clip.end_frame
                self.update()
                return
            self.selected_clip = None
            if self.volume_editing_clip is not None:
                self._exit_volume_editing_with_prompt()

        # fall back: clicking empty lane space also scrubs
        self._drag_mode = 'scrub'
        self.set_current_frame(self.x_to_frame(pos.x()), emit=True)
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.pos()
        if self._drag_mode is None:
            # Not currently dragging anything -- just update the hover
            # cursor: a horizontal resize cursor hints the selected clip's
            # draggable trim-edge zones, a vertical one hints the
            # volume-editing clip's draggable gain line (the "other axis"
            # of the same resize-cursor idea).
            handle = self._trim_handle_at(pos)
            if handle:
                self.setCursor(Qt.SizeHorCursor)
            elif self._volume_line_hover(pos):
                self.setCursor(Qt.SizeVerCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
        if self._drag_mode == 'scrub':
            self.set_current_frame(self.x_to_frame(pos.x()), emit=True)
        elif self._drag_mode == 'trim_left' and self._drag_clip is not None:
            clip = self._drag_clip
            track = self._drag_track
            fps = float(self.fps)
            min_dur = 1.0 / fps
            max_trim_in = clip.source_duration_sec - self._drag_orig_trim_out - min_dur

            trim_in_floor = clip.trim_in_floor_sec

            candidate_start = self.x_to_frame(pos.x())
            delta_frames = candidate_start - self._drag_start_frame
            raw_trim_in = self._drag_orig_trim_in + delta_frames / fps
            trim_in = max(trim_in_floor, min(raw_trim_in, max_trim_in))
            new_start = self._drag_start_frame + int(round((trim_in - self._drag_orig_trim_in) * fps))

            prev_end = self._prev_clip_end(track, clip, self._drag_start_frame)
            new_start = max(0, prev_end, new_start)
            # end_frame must stay fixed for a left-edge trim -- cap
            # new_start so at least one frame of clip remains.
            new_start = min(new_start, self._drag_orig_end_frame - 1)

            # Re-derive trim_in from the (possibly re-clamped) start_frame
            # so the two stay exactly consistent with each other.
            trim_in = self._drag_orig_trim_in + (new_start - self._drag_start_frame) / fps
            trim_in = max(trim_in_floor, min(trim_in, max_trim_in))

            clip.trim_in_sec = trim_in
            clip.start_frame = new_start
            self.updateGeometry()
            self._apply_size_hint()
            self.update()
        elif self._drag_mode == 'trim_right' and self._drag_clip is not None:
            clip = self._drag_clip
            track = self._drag_track
            fps = float(self.fps)
            min_dur = 1.0 / fps
            max_trim_out = clip.source_duration_sec - self._drag_orig_trim_in - min_dur

            candidate_end = self.x_to_frame(pos.x())
            delta_frames = candidate_end - self._drag_orig_end_frame
            raw_trim_out = self._drag_orig_trim_out - delta_frames / fps
            trim_out = max(0.0, min(raw_trim_out, max_trim_out))
            new_length_sec = clip.source_duration_sec - self._drag_orig_trim_in - trim_out
            new_end = clip.start_frame + int(round(new_length_sec * fps))

            next_start = self._next_clip_start(track, clip, self._drag_orig_end_frame)
            if next_start is not None:
                new_end = min(new_end, next_start)
            # start_frame stays fixed for a right-edge trim -- keep at
            # least one frame of clip remaining.
            new_end = max(new_end, clip.start_frame + 1)

            # Re-derive trim_out from the (possibly re-clamped) end_frame
            # so the two stay exactly consistent with each other.
            new_length_frames = new_end - clip.start_frame
            trim_out = clip.source_duration_sec - self._drag_orig_trim_in - new_length_frames / fps
            trim_out = max(0.0, min(trim_out, max_trim_out))

            clip.trim_out_sec = trim_out
            self.updateGeometry()
            self._apply_size_hint()
            self.update()
        elif self._drag_mode == 'volume' and self._drag_clip is not None:
            clip = self._drag_clip
            track_idx = self.tracks.index(self._drag_track) if self._drag_track in self.tracks else -1
            if track_idx >= 0:
                clip_rect = self._clip_rect_for(track_idx, clip)
                gain = self._volume_y_to_gain(clip_rect, pos.y())
                gain = max(0.0, min(VOLUME_GAIN_MAX, gain))
                if self._drag_point_index is None:
                    clip.volume_points = [(0.0, gain), (1.0, gain)]
                else:
                    points = list(clip.volume_points)
                    idx = self._drag_point_index
                    old_frac, _old_gain = points[idx]
                    if old_frac in (0.0, 1.0):
                        # Endpoints stay anchored to the clip's own start/
                        # end -- only their gain moves.
                        new_frac = old_frac
                    else:
                        # Interior points are draggable left/right too, but
                        # clamped strictly between their immediate
                        # neighbors so dragging can never reorder points
                        # (which would need re-indexing mid-drag).
                        played_frac = self._x_to_clip_fraction(clip_rect, pos.x())
                        new_frac = clip.played_fraction_to_extent_fraction(played_frac)
                        prev_frac = points[idx - 1][0]
                        next_frac = points[idx + 1][0]
                        epsilon = min(1e-4, (next_frac - prev_frac) / 4.0)
                        new_frac = max(prev_frac + epsilon, min(next_frac - epsilon, new_frac))
                    points[idx] = (new_frac, gain)
                    clip.volume_points = points
                self.update()
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
        elif self._drag_mode in ('trim_left', 'trim_right') and self._drag_clip is not None:
            clip = self._drag_clip
            trimmed = (clip.trim_in_sec != self._drag_orig_trim_in
                       or clip.trim_out_sec != self._drag_orig_trim_out
                       or clip.start_frame != self._drag_start_frame)
            if trimmed:
                self.undo_stack.push(commands.TrimClipCommand(
                    self, clip,
                    self._drag_orig_trim_in, self._drag_orig_trim_out, self._drag_start_frame,
                    clip.trim_in_sec, clip.trim_out_sec, clip.start_frame,
                ))
        elif self._drag_mode == 'volume' and self._drag_clip is not None:
            clip = self._drag_clip
            if list(clip.volume_points) != self._drag_orig_volume_points:
                self.undo_stack.push(commands.SetClipVolumeCommand(
                    self, clip, self._drag_orig_volume_points, list(clip.volume_points),
                ))
        self._drag_mode = None
        self._drag_clip = None
        self._drag_track = None
        self._drag_origin_track = None
        self._drag_orig_volume_points = None
        self._drag_point_index = None

    def mouseDoubleClickEvent(self, event):
        pos = event.pos()
        clip = self.volume_editing_clip
        if clip is not None:
            for track_idx, track in enumerate(self.tracks):
                if clip in track.clips:
                    clip_rect = self._clip_rect_for(track_idx, clip)
                    sticky_left, sticky_right = self.visible_x_range()

                    # Checked before the readout rects: with closely-spaced
                    # bend points, one point's own readout can visually
                    # overlap a neighboring point's marker, and a direct hit
                    # on an actual marker should always win over a
                    # readout that merely happens to extend over it.
                    #
                    # Double-clicking an existing interior bend point
                    # removes it (endpoints, at fraction 0.0/1.0, are
                    # permanent so the envelope stays fully defined).
                    point_idx = self._volume_point_hit_test(clip, clip_rect, pos)
                    if point_idx is not None:
                        self._remove_volume_point(clip, point_idx)
                        return

                    for idx, readout_rect in self._volume_readout_rects(clip, clip_rect, sticky_left, sticky_right):
                        if readout_rect.contains(pos):
                            self._prompt_volume_percentage(clip, idx)
                            return

                    # Double-clicking elsewhere on the curve inserts a new
                    # bend point there, at whatever gain the curve already
                    # evaluates to at that fraction -- so it never causes a
                    # visible/audible jump.
                    if self._volume_curve_hit_test(clip, clip_rect, pos):
                        played_frac = self._x_to_clip_fraction(clip_rect, pos.x())
                        frac = clip.played_fraction_to_extent_fraction(played_frac)
                        old_points = list(clip.volume_points)
                        gain = volume_envelope.evaluate(old_points, frac)
                        new_points = sorted(old_points + [(frac, gain)], key=lambda p: p[0])
                        self.undo_stack.push(commands.SetClipVolumeCommand(self, clip, old_points, new_points))
                        # Not new_points.index((frac, gain)) -- if the new
                        # gain happens to equal an existing point's exact
                        # gain at a very close fraction, .index() would
                        # find that other point first. Python's sort is
                        # stable and the new tuple was appended last (after
                        # every old_points entry), so its final position is
                        # simply how many old points sort at or before it.
                        self.selected_volume_point_index = sum(1 for p in old_points if p[0] <= frac)
                        self.update()
                        return
                    break
        super().mouseDoubleClickEvent(event)

    def _remove_volume_point(self, clip, point_idx):
        """Removes clip.volume_points[point_idx] via an undoable
        SetClipVolumeCommand -- shared by double-click, right-click, and
        the Delete key (see mouseDoubleClickEvent, contextMenuEvent,
        keyPressEvent) so all three ways of removing a bend point behave
        identically. No-op for either endpoint (fraction 0.0/1.0), which
        are permanent so the envelope stays fully defined across the
        clip."""
        points = clip.volume_points
        if point_idx < 0 or point_idx >= len(points):
            return
        if points[point_idx][0] in (0.0, 1.0):
            return
        old_points = list(points)
        new_points = old_points[:point_idx] + old_points[point_idx + 1:]
        self.undo_stack.push(commands.SetClipVolumeCommand(self, clip, old_points, new_points))
        if self.selected_volume_point_index == point_idx:
            self.selected_volume_point_index = None
        elif self.selected_volume_point_index is not None and self.selected_volume_point_index > point_idx:
            self.selected_volume_point_index -= 1
        self.update()

    def _prompt_volume_percentage(self, clip, point_index=0):
        """Double-clicking a percentage readout opens a number-entry dialog
        for setting an exact gain rather than needing a precise drag --
        capped at VOLUME_GAIN_MAX (200%), the same ceiling dragging itself
        is clamped to, not just 100%. In the flat (<=2-point) case, the one
        readout represents both endpoints together, so this sets both at
        once; once interior bend points exist, `point_index` (from
        _volume_readout_rects) picks which single point's gain is set."""
        max_pct = int(round(VOLUME_GAIN_MAX * 100))
        points = clip.volume_points
        flat = self._volume_is_flat(clip)
        current_gain = points[point_index][1] if points else 1.0
        current_pct = max(0, min(max_pct, int(round(current_gain * 100))))
        value, ok = QInputDialog.getInt(self, "Set Volume", "Volume (%):", current_pct, 0, max_pct, 1)
        if not ok:
            return
        old_points = list(points)
        if flat:
            new_points = [(0.0, value / 100.0), (1.0, value / 100.0)]
        else:
            new_points = list(points)
            frac, _old_gain = new_points[point_index]
            new_points[point_index] = (frac, value / 100.0)
        if new_points != old_points:
            self.undo_stack.push(commands.SetClipVolumeCommand(self, clip, old_points, new_points))
        self.update()

    def _enter_volume_editing(self, clip):
        """Turns on volume-editing mode for `clip`, snapshotting its
        current volume_points so a later Cancel (see _exit_volume_editing)
        can revert to exactly this state."""
        self.volume_editing_clip = clip
        self._volume_edit_entry_points = list(clip.volume_points)
        self.selected_volume_point_index = None

    def _exit_volume_editing(self, revert):
        """Turns off volume-editing mode. `revert=True` (Cancel/Escape)
        pushes a SetClipVolumeCommand back to the volume_points captured
        when editing started, if they've changed -- so canceling is itself
        undoable/redoable like every other edit, regardless of how many
        intermediate drags happened while editing. `revert=False`
        (Apply/clicking elsewhere) just leaves whatever's already live,
        since each drag already pushed its own command on release."""
        clip = self.volume_editing_clip
        if clip is None:
            return
        if revert and self._volume_edit_entry_points is not None:
            current = list(clip.volume_points)
            entry = list(self._volume_edit_entry_points)
            if current != entry:
                self.undo_stack.push(commands.SetClipVolumeCommand(self, clip, current, entry))
        self.volume_editing_clip = None
        self._volume_edit_entry_points = None
        self.selected_volume_point_index = None
        # Abort any in-flight point/line drag on this clip (e.g. Escape
        # pressed with the mouse button still held down mid-drag) -- left
        # alone, mouseMoveEvent's 'volume' branch would keep running against
        # a volume_points list that just got reverted/replaced out from
        # under it, and _drag_point_index (valid for the pre-revert point
        # count) could point past the end of a shorter reverted list.
        if self._drag_mode == 'volume' and self._drag_clip is clip:
            self._drag_mode = None
            self._drag_clip = None
            self._drag_track = None
            self._drag_orig_volume_points = None
            self._drag_point_index = None

    def _exit_volume_editing_with_prompt(self):
        """Like _exit_volume_editing(), but for the "selection moved away
        from the clip being volume-edited" case (clicking a different clip
        or empty space) -- rather than silently keeping the live-mutated
        volume_points (as the old implicit-apply behavior did), ask
        whether to keep them or discard back to how they were when editing
        started. Skipped entirely (straight to a no-op exit) if nothing
        was actually changed, so it never interrupts unless there's
        something to lose."""
        clip = self.volume_editing_clip
        if clip is None:
            return
        if self._volume_edit_entry_points is None or list(clip.volume_points) == list(self._volume_edit_entry_points):
            self._exit_volume_editing(revert=False)
            return
        choice = QMessageBox.question(
            self, "Unsaved Volume Changes",
            f'Save the volume changes made to "{clip.name}"?',
            QMessageBox.Save | QMessageBox.Discard, QMessageBox.Discard,
        )
        self._exit_volume_editing(revert=(choice != QMessageBox.Save))

    def event(self, event):
        # Krita binds Delete (and, on the canvas, Ctrl+Z/Ctrl+Y) to its own
        # actions, and Qt resolves those global shortcuts before ever
        # calling keyPressEvent -- so without claiming these here via
        # ShortcutOverride, Krita's actions eat the key and this widget
        # never sees it.
        if event.type() == QEvent.ShortcutOverride:
            if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and (
                self.selected_clip is not None or self.volume_editing_clip is not None
            ):
                event.accept()
                return True
            if event.key() == Qt.Key_S and self.selected_clip is not None:
                event.accept()
                return True
            if self._is_undo_shortcut(event) or self._is_redo_shortcut(event):
                event.accept()
                return True
            if self._is_copy_shortcut(event) and self.selected_clip is not None:
                event.accept()
                return True
            if self._is_paste_shortcut(event) and (
                self._clipboard_clip is not None
                or self._external_audio_paths(QApplication.clipboard().mimeData())
            ):
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

    @staticmethod
    def _is_copy_shortcut(event):
        mods = event.modifiers()
        return bool(mods & Qt.ControlModifier) and not (mods & Qt.ShiftModifier) and event.key() == Qt.Key_C

    @staticmethod
    def _is_paste_shortcut(event):
        mods = event.modifiers()
        return bool(mods & Qt.ControlModifier) and not (mods & Qt.ShiftModifier) and event.key() == Qt.Key_V

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self.volume_editing_clip is not None:
            # Escape cancels the edit (reverts to the state volume-editing
            # mode was entered with), same as clicking the X icon.
            self._exit_volume_editing(revert=True)
            self.update()
            return
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            if self.volume_editing_clip is not None:
                # Deleting the clip currently being volume-edited is
                # disabled (see _delete_clip) -- Delete instead removes
                # whichever bend point is selected, if any, so it's easy
                # to shape an envelope with the keyboard without risking
                # the whole clip.
                if self.selected_volume_point_index is not None:
                    self._remove_volume_point(self.volume_editing_clip, self.selected_volume_point_index)
                return
            if self.selected_clip is not None:
                self._delete_clip(self.selected_clip)
                return
        if event.key() == Qt.Key_S and not event.modifiers():
            if self.selected_clip is not None:
                self.split_selected_clip()
                return
        if self._is_undo_shortcut(event):
            self.undo_stack.undo()
            return
        if self._is_redo_shortcut(event):
            self.undo_stack.redo()
            return
        if self._is_copy_shortcut(event):
            self.copy_selected_clip()
            return
        if self._is_paste_shortcut(event):
            self.paste_clip()
            return
        super().keyPressEvent(event)

    def copy_selected_clip(self):
        """Ctrl+C: remembers the selected clip for a later Ctrl+V. Stores a
        live reference to the clip itself (not a frozen snapshot), so
        pasting after further edits (trim, volume, etc.) copies its
        then-current state -- same as most clipboards."""
        if self.selected_clip is not None:
            self._copy_clip(self.selected_clip)

    def _copy_clip(self, clip):
        """Shared by the Ctrl+C shortcut and the clip context menu's "Copy
        Clip" action. Clears the system clipboard so a subsequent paste
        picks up *this* clip rather than some stale external file path --
        see paste_clip/_paste_at, which check the system clipboard first."""
        self._clipboard_clip = clip
        QApplication.clipboard().clear()

    def paste_clip(self):
        """Ctrl+V: pastes onto the active track at the playhead -- see
        _paste_at for the actual clipboard-source priority."""
        idx = self.active_track_index if self.tracks else 0
        self._paste_at(idx, self.current_frame)

    def _paste_at(self, track_idx, frame):
        """Pastes at `frame` on track `track_idx` -- shared by the Ctrl+V
        shortcut (paste_clip, always at the playhead/active track) and the
        empty-space context menu's "Paste" action (at the clicked
        position). A file copied from outside Krita (e.g. the OS file
        manager) takes priority over a clip copied inside the timeline --
        see _copy_clip, which clears the system clipboard on an internal
        copy specifically so this ordering resolves unambiguously rather
        than on which happened more recently."""
        paths = self._external_audio_paths(QApplication.clipboard().mimeData())
        if paths:
            self._import_external_audio_files(paths, track_idx, frame)
            return
        if self._clipboard_clip is None:
            return
        if not (0 <= track_idx < len(self.tracks)):
            return
        track = self.tracks[track_idx]
        start_frame = track.find_insert_start(frame, self._clipboard_clip.length_frames)
        new_clip = self._clipboard_clip.clone(start_frame)
        self.undo_stack.push(commands.AddClipCommand(self, track, new_clip))

    def _clipboard_has_content(self):
        """Whether paste_clip/_paste_at currently has anything to paste --
        either an internally-copied clip or an external file path -- used
        to decide whether the empty-space context menu offers "Paste"."""
        if self._clipboard_clip is not None:
            return True
        return bool(self._external_audio_paths(QApplication.clipboard().mimeData()))

    # --------------------------------------------------------- external i/o
    @staticmethod
    def _external_audio_paths(mime_data):
        """Local file paths in `mime_data` with a supported audio
        extension -- shared by drag-and-drop from the OS file manager
        (dropEvent) and pasting a file copied there (paste_clip)."""
        if mime_data is None or not mime_data.hasUrls():
            return []
        paths = []
        for url in mime_data.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            if path.lower().endswith(EXTERNAL_AUDIO_EXTENSIONS):
                paths.append(path)
        return paths

    def _import_external_audio_files(self, paths, track_idx, start_frame):
        """Imports each of `paths` onto track `track_idx` (creating a first
        track if there isn't one yet, same fallback as
        AudioTimelineDocker.import_audio), each nudged clear of whatever it
        would otherwise overlap -- shared by dropEvent and paste_clip's
        external-clipboard fallback."""
        if not paths:
            return
        if not self.tracks:
            track = AudioTrack(name="Track 1")
            self.undo_stack.push(commands.AddTrackCommand(self, track))
            track_idx = 0
        if not (0 <= track_idx < len(self.tracks)):
            track_idx = max(0, len(self.tracks) - 1)
        track = self.tracks[track_idx]
        for path in paths:
            try:
                clip = AudioClip(path, start_frame, self.fps)
            except Exception as exc:  # e.g. missing pydub for non-wav files
                QMessageBox.critical(self, "Audio Timeline", str(exc))
                continue
            clip.start_frame = track.find_insert_start(clip.start_frame, clip.length_frames)
            self.undo_stack.push(commands.AddClipCommand(self, track, clip))
        self.active_track_index = track_idx

    def dragEnterEvent(self, event):
        if self._external_audio_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._external_audio_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = self._external_audio_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        pos = event.pos()
        idx = self.track_index_at_y(pos.y())
        if idx < 0:
            idx = self.active_track_index if self.tracks else 0
        frame = self.x_to_frame(pos.x())
        self._import_external_audio_files(paths, idx, frame)
        event.acceptProposedAction()
        self.update()

    def contextMenuEvent(self, event):
        pos = event.pos()
        idx = self.track_index_at_y(pos.y())
        if idx < 0:
            return
        track = self.tracks[idx]
        frame = self.x_to_frame(pos.x())
        clip = track.clip_at_frame(frame)
        if clip is None:
            if not self._clipboard_has_content():
                return
            menu = QMenu(self)
            paste_action = menu.addAction("Paste")
            chosen = menu.exec_(event.globalPos())
            if chosen == paste_action:
                self._paste_at(idx, frame)
            return
        if self.volume_editing_clip is clip:
            # While this clip is being volume-edited, right-click only
            # operates on bend points (clip-level actions -- delete/split/
            # trim -- are disabled for it until it exits edit mode; see
            # mousePressEvent/keyPressEvent/split_selected_clip/
            # _delete_clip's own guards), so there's no "Delete Clip" menu
            # here at all while editing. Unlike the Delete key (which
            # removes the already-selected point instantly), right-click
            # shows a menu -- same "click to confirm" pattern as the
            # normal clip's "Delete Clip" menu below -- rather than
            # removing the point the instant it's right-clicked.
            if not self._volume_is_flat(clip):
                clip_rect = self._clip_rect_for(idx, clip)
                point_idx = self._volume_point_hit_test(clip, clip_rect, pos)
                if point_idx is not None and clip.volume_points[point_idx][0] not in (0.0, 1.0):
                    self.selected_volume_point_index = point_idx
                    self.update()
                    point_menu = QMenu(self)
                    remove_action = point_menu.addAction("Remove Point")
                    chosen = point_menu.exec_(event.globalPos())
                    if chosen == remove_action:
                        self._remove_volume_point(clip, point_idx)
            return
        menu = QMenu(self)
        delete_action = menu.addAction("Delete Clip")
        copy_action = menu.addAction("Copy Clip")
        chosen = menu.exec_(event.globalPos())
        if chosen == delete_action:
            self._delete_clip(clip)
        elif chosen == copy_action:
            self._copy_clip(clip)

    def _delete_clip(self, clip):
        if clip is self.volume_editing_clip:
            # Deleting the clip currently being volume-edited is disabled
            # -- Delete/right-click remove a selected bend point instead
            # while editing is active (see keyPressEvent/contextMenuEvent).
            return
        for track in self.tracks:
            if clip in track.clips:
                self.undo_stack.push(commands.DeleteClipCommand(self, track, clip))
                break

    def _split_clip(self, clip):
        for track in self.tracks:
            if clip in track.clips:
                self.undo_stack.push(commands.SplitClipCommand(self, track, clip, self.current_frame))
                break

    def can_split_selected_clip(self):
        """Whether split_selected_clip would actually do anything right
        now -- shared with the toolbar split button's enabled state so it
        can't be clicked when the playhead is outside the selected clip."""
        clip = self.selected_clip
        if clip is None or clip is self.volume_editing_clip:
            return False
        return clip.start_frame < self.current_frame < clip.end_frame

    def split_selected_clip(self):
        """Splits the selected clip at the current playhead, if the
        playhead sits strictly inside it -- shared by the S-key shortcut
        (keyPressEvent) and the toolbar split button so both follow the
        exact same guard. Disabled entirely while the clip is being
        volume-edited (see mousePressEvent's trim-disabling guard and
        _delete_clip's matching one)."""
        if self.can_split_selected_clip():
            self._split_clip(self.selected_clip)

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
