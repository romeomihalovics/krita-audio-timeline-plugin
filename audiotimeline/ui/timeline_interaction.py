"""InteractionMixin: the mouse/keyboard drag state machine (scrub, move,
trim, volume drag), clip selection, split/delete, and the context menu.
Mixed into AudioTimelineWidget alongside PaintingMixin/VolumeEditingMixin/
ClipboardMixin (see ui/timeline_widget.py)."""

from PyQt5.QtCore import Qt, QEvent
from PyQt5.QtWidgets import QMenu, QApplication

from .. import commands
from ..audio import volume_envelope
from .timeline_constants import HANDLE_PX, VOLUME_GAIN_MAX


class InteractionMixin:
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
            self._relayout()
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
            self._relayout()
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
            self._relayout()

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
        track = self._track_containing(clip)
        if track is not None:
            self.undo_stack.push(commands.DeleteClipCommand(self, track, clip))

    def _split_clip(self, clip):
        track = self._track_containing(clip)
        if track is not None:
            self.undo_stack.push(commands.SplitClipCommand(self, track, clip, self.current_frame))

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
            self._relayout()
        else:
            super().wheelEvent(event)
