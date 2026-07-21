from PyQt5.QtCore import Qt, QRect, QSize, pyqtSignal
from PyQt5.QtWidgets import QWidget, QUndoStack

from .timeline_constants import (
    EXTERNAL_AUDIO_EXTENSIONS, TRACK_HEIGHT, RULER_HEIGHT, TRACK_HEADER_WIDTH,
    BUTTON_SIZE, BUTTON_GAP,
)
from .timeline_theme import ThemeMixin
from .timeline_painting import PaintingMixin
from .timeline_volume_editing import VolumeEditingMixin
from .timeline_interaction import InteractionMixin
from .timeline_clipboard import ClipboardMixin

# Re-exported for callers that only need the satellite widgets alongside
# this one (the docker imports all of them from this module).
from .ruler_widget import AudioTimelineRulerWidget
from .header_widget import AudioTimelineHeaderWidget
from .chrome_widgets import AudioTimelineCornerWidget, AudioTimelineHeaderWrapperWidget

__all__ = [
    'AudioTimelineWidget',
    'AudioTimelineRulerWidget', 'AudioTimelineHeaderWidget',
    'AudioTimelineCornerWidget', 'AudioTimelineHeaderWrapperWidget',
    'RULER_HEIGHT', 'TRACK_HEADER_WIDTH',
]


class AudioTimelineWidget(ThemeMixin, PaintingMixin, VolumeEditingMixin, InteractionMixin, ClipboardMixin, QWidget):
    """
    Renders N audio tracks as horizontal lanes with waveforms, plus a
    shared playhead. Emits `scrubbed(frame)` when the user drags the
    playhead or ruler. Every track/clip mutation (add, delete, move, mute,
    rename) goes through `undo_stack` so it's independently undoable/
    redoable from within the docker, and emits `contentChanged(affects_audio)`
    so the docker knows whether that mutation needs a mixdown re-render
    (e.g. an empty track add/remove/rename doesn't, a clip add/move/delete
    or a mute toggle on a track with clips does).

    Split across several mixins by responsibility -- see ThemeMixin
    (timeline_theme.py), PaintingMixin (timeline_painting.py),
    VolumeEditingMixin (timeline_volume_editing.py), InteractionMixin
    (timeline_interaction.py) and ClipboardMixin (timeline_clipboard.py) --
    this class itself owns the core state (tracks/fps/undo_stack/drag
    state) and the layout/geometry methods every mixin builds on.
    """

    scrubbed = pyqtSignal(int)
    contentChanged = pyqtSignal(bool)
    # Emitted the instant a clip/trim/volume drag *starts* -- before any
    # actual mutation, and well before contentChanged fires on release.
    # Dragging doesn't push an undo command (and so doesn't fire
    # contentChanged) until mouseReleaseEvent, but a mixdown render
    # already in flight when the drag starts is *known* stale from this
    # point on (whatever the drag ends up doing, it changes the audio) --
    # so the docker can cancel it right away instead of leaving it to
    # finish uselessly while the user is still mid-drag. See
    # InteractionMixin.mousePressEvent and AudioTimelineDocker's
    # connection to MixdownController.cancel_inflight().
    mixdownInvalidated = pyqtSignal()
    # Emitted at the end of every clip/trim/volume drag (mouseReleaseEvent),
    # whether or not it actually changed anything -- a real edit already
    # triggers its own re-render via contentChanged, but a drag that
    # started with mixdownInvalidated (cancelling an in-flight render) and
    # then turns out to be a no-op (released back at its starting position)
    # pushes no undo command and so never fires contentChanged -- nothing
    # else would ever re-render the edit that got cancelled out from under
    # it otherwise. See MixdownController.cancel_inflight()/drag_settled().
    mixdownDragSettled = pyqtSignal()
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

        # Background waveform-decode queue for clips imported with
        # defer_analysis=True (see AudioClip) -- one file decoded at a time,
        # same "coalesce, don't overlap" shape as the docker's mixdown
        # worker. See request_waveform/_start_next_waveform_job/
        # _on_waveform_ready (ClipboardMixin, timeline_clipboard.py).
        self._waveform_queue = []
        self._waveform_thread = None
        self._waveform_inflight_path = None

        # Cache of rendered waveform QPixmaps, keyed by clip.id -- see
        # PaintingMixin._paint_waveform_cached(). Lives here (not on the
        # clip) since it's purely a paint-time optimization, not part of
        # the clip's actual state.
        self._waveform_pixmap_cache = {}

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

        # sizeHint() last actually applied via resize()/layoutChanged -- see
        # _apply_size_hint(). None so the first call always applies.
        self._last_applied_size_hint = None

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
        #
        # sizeHint() itself is an O(all clips across all tracks) scan (see
        # _content_end_frame()), and resize()/layoutChanged fan out to the
        # ruler/header/scrollbar-gutter widgets' own resize+repaint (see
        # docker_ui.py's layoutChanged connections) -- skipped entirely when
        # the hint hasn't actually changed, which is true for most in-track
        # moves/trims (only a clip crossing the current content edge, a
        # zoom change, or a track count change actually grows/shrinks it).
        hint = self.sizeHint()
        if hint == self._last_applied_size_hint:
            return
        self._last_applied_size_hint = hint
        self.resize(hint)
        self.layoutChanged.emit()

    def _relayout(self):
        """updateGeometry() + _apply_size_hint() + update() -- the same
        "content/geometry just changed" triple every mutation that can
        affect layout (zoom, track add/remove, clip drag/resize, trim)
        needs to run, collapsed into one call so those call sites can't
        drift out of sync by forgetting one of the three."""
        self.updateGeometry()
        self._apply_size_hint()
        self.update()

    def _track_containing(self, clip):
        """The track `clip` currently lives on, or None -- shared by every
        call site that needs to find a clip's track by membership rather
        than already holding a reference to it (e.g. _delete_clip,
        _split_clip)."""
        for track in self.tracks:
            if clip in track.clips:
                return track
        return None

    def set_fps(self, fps):
        self.fps = max(1, fps)
        self._relayout()

    def set_total_frames(self, total_frames):
        self.total_frames = max(1, total_frames)
        self._relayout()

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
        self._relayout()

    def remove_track(self, track):
        if track in self.tracks:
            self.tracks.remove(track)
        if self.active_track_index >= len(self.tracks):
            self.active_track_index = max(0, len(self.tracks) - 1)
        self._relayout()

    def refresh_layout(self):
        """Recomputes size/geometry and repaints after tracks were swapped
        out wholesale from outside the normal add/remove-track calls above
        (the docker restoring a different document's cached track list on
        switch)."""
        self._relayout()

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

    def _clip_rect_for(self, track_idx, clip):
        """The on-screen rect _paint_clip() draws `clip` into -- shared
        with mouse hit-testing so icon/line hit-tests always agree with
        what's actually drawn."""
        x0 = self.frame_to_x(clip.start_frame)
        x1 = self.frame_to_x(clip.end_frame)
        return QRect(x0, self.track_y(track_idx) + 4, max(2, x1 - x0), TRACK_HEIGHT - 8)
