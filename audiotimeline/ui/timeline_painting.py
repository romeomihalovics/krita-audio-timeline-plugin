"""PaintingMixin: everything AudioTimelineWidget draws for itself (tracks,
clips, waveforms, the volume-editing overlay, the playhead) -- mixed into
AudioTimelineWidget alongside VolumeEditingMixin/InteractionMixin/
ClipboardMixin (see ui/timeline_widget.py). Reads geometry/hit-test helpers
off `self` (defined on the core widget or the other mixins) the same way
they were reached as sibling methods before this file was split out."""

from PyQt5.QtCore import Qt, QRect, QPoint
from PyQt5.QtGui import QPainter, QColor, QPen, QBrush, QFontMetrics, QPixmap
from PyQt5.QtWidgets import QStyle

from ..audio import volume_envelope
from .timeline_constants import TRACK_HEIGHT, VOLUME_ICON_SIZE, VOLUME_GAIN_UNITY, VOLUME_POINT_RADIUS
from .timeline_icons import krita_icon, tinted_icon_pixmap
from .timeline_theme import paint_out_of_range_overlay
from .timeline_volume_editing import gain_to_pct_text


class PaintingMixin:
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
        paint_out_of_range_overlay(painter, self.frame_to_x(self.total_frames), self.width(), self.height())
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
            self._paint_waveform_cached(painter, clip_rect, clip, theme, visible_left, visible_right)
        elif clip.peaks is None:
            self._paint_pending_waveform(painter, clip_rect, theme, visible_left, visible_right)

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
            apply_icon = krita_icon("dialog-ok-apply", QStyle.SP_DialogApplyButton)
            cancel_icon = krita_icon("dialog-cancel", QStyle.SP_DialogCancelButton)
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
            volume_icon = krita_icon("audio-volume-high", QStyle.SP_MediaVolume)
            icon_pixmap = tinted_icon_pixmap(volume_icon, VOLUME_ICON_SIZE, theme['clip_text'])
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
                curve_icon = krita_icon("curve-preset-s", QStyle.SP_FileDialogDetailedView)
                curve_pixmap = tinted_icon_pixmap(curve_icon, VOLUME_ICON_SIZE, theme['clip_text'])
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
        # Sorted once for the whole curve sweep below instead of per-step
        # inside evaluate() -- same reasoning as _paint_waveform's
        # sorted_points.
        sorted_points = sorted(points, key=lambda p: p[0])

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
            gain = volume_envelope.evaluate_sorted(sorted_points, extent_frac)
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
        text = gain_to_pct_text(gain)

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
        text = gain_to_pct_text(point[1])

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
            text = gain_to_pct_text(gain)
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

    # Qt's raster paint engine loses precision (and can silently stop
    # drawing altogether) on coordinates much past roughly +/-32,767px --
    # a long clip zoomed in far enough can easily exceed that on its own
    # (e.g. a 3-minute clip at even a modest zoom is well past it), so a
    # single QPixmap spanning the clip's whole on-screen width isn't safe
    # at any zoom level. Kept comfortably under that limit.
    _MAX_WAVEFORM_TILE_PX = 8000

    def _paint_waveform_cached(self, painter, clip_rect, clip, theme, visible_left, visible_right):
        """Renders _paint_waveform() into offscreen QPixmap *tiles*
        (see _MAX_WAVEFORM_TILE_PX) and reuses them across repaints,
        instead of re-walking peak buckets and re-evaluating the gain
        envelope per pixel column on every single paintEvent (drag,
        scroll, playhead move, another clip's own drag).

        Tiles are keyed in clip-local coordinates (a tile index into the
        clip's own full on-screen width), not by clip_rect's actual
        on-screen position -- moving a clip horizontally (the common case
        while dragging) only changes where already-cached tiles get
        blitted, so it stays cheap mid-drag. Only tiles overlapping the
        currently-visible slice are ever rendered, so a long clip mostly
        scrolled off-screen doesn't pay to render (or even allocate) the
        rest of it. A trim, zoom, or volume-envelope edit changes the key
        and forces the affected tile(s) to re-render, same cost as before
        tiling.

        Keyed per clip.id (not by clip identity) so a clone (split/copy)
        starts with its own cache entries rather than colliding with its
        sibling's."""
        peaks = self._trimmed_peaks(clip)
        if not peaks:
            return
        full_width = max(1, clip_rect.width())
        height = max(1, clip_rect.height())

        draw_left = max(clip_rect.left(), visible_left) - clip_rect.left()
        draw_right = min(clip_rect.right(), visible_right) - clip_rect.left()
        if draw_right < draw_left:
            return

        tile_w = min(full_width, self._MAX_WAVEFORM_TILE_PX)
        first_tile = max(0, draw_left // tile_w)
        last_tile = min((full_width - 1) // tile_w, draw_right // tile_w)

        tiles = self._waveform_pixmap_cache.setdefault(clip.id, {})
        base_key = (
            id(clip.peaks), clip.trim_in_sec, clip.trim_out_sec,
            full_width, height, tuple(clip.volume_points),
            theme['waveform'].name(), theme['waveform_clipped'].name(),
        )
        for tile_idx in range(first_tile, last_tile + 1):
            tile_start = tile_idx * tile_w
            tile_width = min(tile_w, full_width - tile_start)
            key = base_key + (tile_idx,)
            cached = tiles.get(tile_idx)
            if cached is None or cached[0] != key:
                pixmap = QPixmap(tile_width, height)
                pixmap.fill(Qt.transparent)
                pm_painter = QPainter(pixmap)
                pm_painter.setRenderHint(QPainter.Antialiasing)
                # Shifted left by tile_start (not 0) so bucket->x math
                # inside _paint_waveform -- computed against the clip's
                # full (unshifted) width, to keep step_px/zoom correct --
                # lands each bucket at its position *within this tile*
                # rather than its absolute position in the whole clip.
                local_rect = QRect(-tile_start, 0, full_width, height)
                self._paint_waveform(pm_painter, local_rect, peaks, theme, 0, tile_width, clip)
                pm_painter.end()
                cached = (key, pixmap)
                tiles[tile_idx] = cached
            painter.drawPixmap(clip_rect.left() + tile_start, clip_rect.top(), cached[1])

    def _paint_pending_waveform(self, painter, clip_rect, theme, visible_left, visible_right):
        """Drawn instead of _paint_waveform for a clip whose peaks haven't
        finished decoding yet (clip.peaks is None -- see AudioClip's
        defer_analysis and AudioTimelineWidget.request_waveform): a flat
        centerline across the clip, like a silent/muted waveform, so the
        clip is visibly present and editable at its full length while the
        real waveform loads in the background."""
        draw_left = max(clip_rect.left(), visible_left)
        draw_right = min(clip_rect.right(), visible_right)
        if draw_right < draw_left:
            return
        painter.setPen(QPen(theme['waveform'], 1))
        mid_y = clip_rect.center().y()
        painter.drawLine(int(draw_left), int(mid_y), int(draw_right), int(mid_y))

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
        # Sorted once here rather than inside evaluate() -- gain_at_index
        # below is called once per drawn bucket, and re-sorting the same
        # small list on every one of those calls added up across a long
        # clip's worth of buckets.
        sorted_points = sorted(points, key=lambda p: p[0])
        step_px = clip_rect.width() / float(n)
        mid_y = clip_rect.center().y()
        half_h = clip_rect.height() / 2 - 4

        def gain_at_index(i):
            played_frac = i / float(n - 1) if n > 1 else 0.0
            extent_frac = clip.played_fraction_to_extent_fraction(played_frac) if clip is not None else played_frac
            return volume_envelope.evaluate_sorted(sorted_points, extent_frac)

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
