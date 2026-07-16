"""VolumeEditingMixin: the per-clip volume-envelope subsystem -- geometry/
hit-testing for the volume icon, apply/cancel icons, the gain line/curve and
its bend points, plus the editing-mode enter/exit flow and the exact-
percentage dialog. Mixed into AudioTimelineWidget alongside PaintingMixin/
InteractionMixin/ClipboardMixin (see ui/timeline_widget.py)."""

from PyQt5.QtCore import QRect
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtWidgets import QInputDialog, QMessageBox

from .. import commands
from ..audio import volume_envelope
from .timeline_constants import HANDLE_PX, VOLUME_ICON_SIZE, VOLUME_GAIN_UNITY, VOLUME_GAIN_MAX


def gain_to_pct_text(gain):
    """"NN%" for a gain value -- shared formatting used by every volume
    readout/badge/dialog (the indicator badge, the bottom-pinned and
    per-point readouts, and the percentage-entry dialog's current value)
    so they can't drift out of sync with each other."""
    return f"{int(round(gain * 100))}%"


class VolumeEditingMixin:
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
        return gain_to_pct_text(gain)

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
