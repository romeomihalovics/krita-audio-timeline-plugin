from PyQt5.QtWidgets import QUndoCommand

from . import volume_envelope


class AddTrackCommand(QUndoCommand):
    """Adds a new (empty) track and makes it the active one."""

    def __init__(self, timeline, track, index=None):
        super().__init__(f'Add track "{track.name}"')
        self.timeline = timeline
        self.track = track
        self.index = index if index is not None else len(timeline.tracks)
        self.previous_active_index = timeline.active_track_index
        # A freshly added track is always empty, so it can never change
        # what the mixdown sounds like.
        self.affects_audio = False

    def redo(self):
        self.timeline.insert_track(self.index, self.track)
        self.timeline.active_track_index = self.index
        self.timeline.contentChanged.emit(self.affects_audio)

    def undo(self):
        self.timeline.remove_track(self.track)
        self.timeline.active_track_index = max(
            0, min(self.previous_active_index, len(self.timeline.tracks) - 1)
        )
        self.timeline.contentChanged.emit(self.affects_audio)


class DeleteTrackCommand(QUndoCommand):
    def __init__(self, timeline, track):
        super().__init__(f'Delete track "{track.name}"')
        self.timeline = timeline
        self.track = track
        self.index = timeline.tracks.index(track)
        self.previous_active_index = timeline.active_track_index
        # Only matters for the mixdown if the track actually had clips.
        self.affects_audio = bool(track.clips)

    def redo(self):
        self.timeline.remove_track(self.track)
        self.timeline.contentChanged.emit(self.affects_audio)

    def undo(self):
        self.timeline.insert_track(self.index, self.track)
        self.timeline.active_track_index = max(
            0, min(self.previous_active_index, len(self.timeline.tracks) - 1)
        )
        self.timeline.contentChanged.emit(self.affects_audio)


class AddClipCommand(QUndoCommand):
    def __init__(self, timeline, track, clip):
        super().__init__(f'Import clip "{clip.name}"')
        self.timeline = timeline
        self.track = track
        self.clip = clip
        self.affects_audio = True

    def redo(self):
        self.track.add_clip(self.clip)
        self.timeline.selected_clip = self.clip
        self.timeline.refresh_layout()
        self.timeline.contentChanged.emit(self.affects_audio)

    def undo(self):
        self.track.remove_clip(self.clip.id)
        if self.timeline.selected_clip is self.clip:
            self.timeline.selected_clip = None
        self.timeline.refresh_layout()
        self.timeline.contentChanged.emit(self.affects_audio)


class DeleteClipCommand(QUndoCommand):
    def __init__(self, timeline, track, clip):
        super().__init__(f'Delete clip "{clip.name}"')
        self.timeline = timeline
        self.track = track
        self.clip = clip
        self.index = track.clips.index(clip)
        self.affects_audio = True

    def redo(self):
        self.track.remove_clip(self.clip.id)
        if self.timeline.selected_clip is self.clip:
            self.timeline.selected_clip = None
        self.timeline.refresh_layout()
        self.timeline.contentChanged.emit(self.affects_audio)

    def undo(self):
        self.track.clips.insert(self.index, self.clip)
        self.timeline.selected_clip = self.clip
        self.timeline.refresh_layout()
        self.timeline.contentChanged.emit(self.affects_audio)


class MoveClipCommand(QUndoCommand):
    """Pushed once, on mouse release, at the end of a clip drag -- not per
    mouse-move step -- so the mixdown only re-renders once the move is
    actually finished. Covers both a same-track move and one that also
    dropped the clip onto a different track -- old_track/new_track are the
    same object for a plain horizontal move.

    redo() is also called once, synchronously, by QUndoStack.push() itself,
    right after the drag has already live-mutated the clip's track/position
    on-screen -- so both branches guard against redoing work that's already
    done (matching the pattern the plain-drag case already relied on for
    start_frame)."""

    def __init__(self, timeline, clip, old_track, old_start_frame, new_track, new_start_frame):
        super().__init__(f'Move clip "{clip.name}"')
        self.timeline = timeline
        self.clip = clip
        self.old_track = old_track
        self.old_start_frame = old_start_frame
        self.new_track = new_track
        self.new_start_frame = new_start_frame
        self.affects_audio = True

    def redo(self):
        if self.new_track is not self.old_track:
            if self.clip in self.old_track.clips:
                self.old_track.remove_clip(self.clip.id)
            if self.clip not in self.new_track.clips:
                self.new_track.add_clip(self.clip)
        self.clip.start_frame = self.new_start_frame
        self.timeline.refresh_layout()
        self.timeline.contentChanged.emit(self.affects_audio)

    def undo(self):
        if self.new_track is not self.old_track:
            if self.clip in self.new_track.clips:
                self.new_track.remove_clip(self.clip.id)
            if self.clip not in self.old_track.clips:
                self.old_track.add_clip(self.clip)
        self.clip.start_frame = self.old_start_frame
        self.timeline.refresh_layout()
        self.timeline.contentChanged.emit(self.affects_audio)


class TrimClipCommand(QUndoCommand):
    """Pushed once, on mouse release, at the end of an edge-drag -- the drag
    itself has already live-mutated trim_in_sec/trim_out_sec/start_frame, so
    redo()/undo() just need to (re)apply the recorded before/after values,
    matching MoveClipCommand's guard pattern.

    Never touches volume_points: a clip's bend points are stored relative
    to its permanent extent (trim_in_floor_sec .. source_duration_sec, see
    AudioClip.played_fraction_to_extent_fraction), not its current played
    window, so an ordinary trim-edge drag changing only trim_in_sec/
    trim_out_sec never needs to rewrite them -- a point trimmed out of
    view reappears, unmodified, once the clip is trimmed back out."""

    def __init__(self, timeline, clip, old_trim_in, old_trim_out, old_start_frame,
                 new_trim_in, new_trim_out, new_start_frame):
        super().__init__(f'Trim clip "{clip.name}"')
        self.timeline = timeline
        self.clip = clip
        self.old = (old_trim_in, old_trim_out, old_start_frame)
        self.new = (new_trim_in, new_trim_out, new_start_frame)
        self.affects_audio = True

    def redo(self):
        self.clip.trim_in_sec, self.clip.trim_out_sec, self.clip.start_frame = self.new
        self.timeline.refresh_layout()
        self.timeline.contentChanged.emit(self.affects_audio)

    def undo(self):
        self.clip.trim_in_sec, self.clip.trim_out_sec, self.clip.start_frame = self.old
        self.timeline.refresh_layout()
        self.timeline.contentChanged.emit(self.affects_audio)


class SplitClipCommand(QUndoCommand):
    """Splits `clip` into two siblings at `split_frame` (a frame strictly
    inside the clip). Only ever touches the one clip it's given -- the
    track's other clips are untouched. redo() replaces the original clip
    object with the two new sibling objects; undo() removes both and
    restores the original single clip, so no other state (selection aside)
    needs to change hands."""

    def __init__(self, timeline, track, clip, split_frame):
        super().__init__(f'Split clip "{clip.name}"')
        self.timeline = timeline
        self.track = track
        self.clip = clip
        self.index = track.clips.index(clip)

        # split_offset_sec is how far into the clip's *current* (already-
        # trimmed) audio the split falls; split_point_sec is that same
        # position but measured from the absolute start of the source file
        # (i.e. in the same units as trim_in_sec/source_duration_sec).
        split_offset_sec = (split_frame - clip.start_frame) / float(clip.fps)
        split_point_sec = clip.trim_in_sec + split_offset_sec

        # Each sibling's ceiling/floor is walled off at the cut so neither
        # can be re-trimmed back out into the other's territory (which would
        # duplicate audio) -- see clone_for_split's docstring.
        self.left = clip.clone_for_split(
            trim_in_sec=clip.trim_in_sec,
            trim_out_sec=0.0,
            start_frame=clip.start_frame,
            source_duration_sec=split_point_sec,
            trim_in_floor_sec=clip.trim_in_floor_sec,
        )
        self.right = clip.clone_for_split(
            trim_in_sec=split_point_sec,
            trim_out_sec=clip.trim_out_sec,
            start_frame=split_frame,
            source_duration_sec=clip.source_duration_sec,
            trim_in_floor_sec=split_point_sec,
        )

        # clone_for_split() just copies volume_points verbatim onto both
        # siblings -- fine when there are only the two (always-equal) flat
        # endpoints, but a multi-point envelope's fractions are relative to
        # the *original* clip's permanent extent (trim_in_floor_sec ..
        # source_duration_sec, not its played window -- see
        # AudioClip.played_fraction_to_extent_fraction), so left/right need
        # to be remapped onto each sibling's own (now permanently walled-
        # off, per clone_for_split above) extent instead, or an interior
        # bend point would land at the wrong relative position -- or
        # outside the sibling's span entirely -- on each half. Unlike an
        # ordinary trim, a split permanently narrows each sibling's extent
        # (its own trim_in_floor_sec/source_duration_sec), so points
        # outside the kept half are genuinely gone for that sibling (only
        # undo, not re-trimming, brings them back).
        orig_extent_duration = clip.source_duration_sec - clip.trim_in_floor_sec
        split_extent_frac = (
            (split_point_sec - clip.trim_in_floor_sec) / orig_extent_duration
            if orig_extent_duration > 0 else 0.5
        )
        self.left.volume_points = volume_envelope.split(clip.volume_points, split_extent_frac, keep_left=True)
        self.right.volume_points = volume_envelope.split(clip.volume_points, split_extent_frac, keep_left=False)
        self.affects_audio = True

    def redo(self):
        if self.clip in self.track.clips:
            idx = self.track.clips.index(self.clip)
            self.track.clips[idx:idx + 1] = [self.left, self.right]
        self.timeline.selected_clip = self.left
        if self.timeline.volume_editing_clip is self.clip:
            # Same reasoning as _delete_clip: the original clip object is
            # gone from track.clips, so there's nothing to revert to --
            # just drop the editing-mode state directly.
            self.timeline.volume_editing_clip = None
            self.timeline._volume_edit_entry_points = None
        self.timeline.refresh_layout()
        self.timeline.contentChanged.emit(self.affects_audio)

    def undo(self):
        if self.left in self.track.clips:
            idx = self.track.clips.index(self.left)
            self.track.clips[idx:idx + 2] = [self.clip]
        self.timeline.selected_clip = self.clip
        if self.timeline.volume_editing_clip in (self.left, self.right):
            self.timeline.volume_editing_clip = None
            self.timeline._volume_edit_entry_points = None
        self.timeline.refresh_layout()
        self.timeline.contentChanged.emit(self.affects_audio)


class SetClipVolumeCommand(QUndoCommand):
    """Pushed once, on mouse release, at the end of a volume-line drag (or
    a phase-2 point add/move/remove) -- the drag has already live-mutated
    clip.volume_points, so redo()/undo() just (re)apply the recorded
    before/after list, matching MoveClipCommand's guard pattern."""

    def __init__(self, timeline, clip, old_points, new_points):
        super().__init__(f'Adjust volume of "{clip.name}"')
        self.timeline = timeline
        self.clip = clip
        self.old_points = old_points
        self.new_points = new_points
        self.affects_audio = True

    def redo(self):
        self.clip.volume_points = list(self.new_points)
        self.timeline.update()
        self.timeline.contentChanged.emit(self.affects_audio)

    def undo(self):
        self.clip.volume_points = list(self.old_points)
        self.timeline.update()
        self.timeline.contentChanged.emit(self.affects_audio)


class MuteTrackCommand(QUndoCommand):
    def __init__(self, timeline, track):
        super().__init__(f'Toggle mute "{track.name}"')
        self.timeline = timeline
        self.track = track
        self.old_muted = track.muted
        # Muting a track with no clips can't change anything audible.
        self.affects_audio = bool(track.clips)

    def redo(self):
        self.track.muted = not self.old_muted
        self.timeline.update()
        self.timeline.contentChanged.emit(self.affects_audio)

    def undo(self):
        self.track.muted = self.old_muted
        self.timeline.update()
        self.timeline.contentChanged.emit(self.affects_audio)


class RenameTrackCommand(QUndoCommand):
    def __init__(self, timeline, track, new_name):
        super().__init__(f'Rename track to "{new_name}"')
        self.timeline = timeline
        self.track = track
        self.old_name = track.name
        self.new_name = new_name
        # The track name never factors into the rendered audio.
        self.affects_audio = False

    def redo(self):
        self.track.name = self.new_name
        self.timeline.update()
        self.timeline.contentChanged.emit(self.affects_audio)

    def undo(self):
        self.track.name = self.old_name
        self.timeline.update()
        self.timeline.contentChanged.emit(self.affects_audio)
