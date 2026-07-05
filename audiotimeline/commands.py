from PyQt5.QtWidgets import QUndoCommand


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
