"""Every QUndoCommand in commands.py, pushed through a real
AudioTimelineWidget's undo_stack and exercised through undo()/redo() (not
just direct .redo()/.undo() calls) so behavior matches what Ctrl+Z/Ctrl+Y
actually drive in the app.
"""

from .. import commands
from ..audio.audio_track import AudioTrack


def test_add_track_command_undo_redo(timeline):
    track = AudioTrack(name="New Track")
    timeline.undo_stack.push(commands.AddTrackCommand(timeline, track))
    assert track in timeline.tracks
    assert timeline.active_track_index == timeline.tracks.index(track)

    timeline.undo_stack.undo()
    assert track not in timeline.tracks

    timeline.undo_stack.redo()
    assert track in timeline.tracks


def test_delete_track_command_undo_redo(timeline):
    track = AudioTrack(name="T1")
    timeline.undo_stack.push(commands.AddTrackCommand(timeline, track))
    timeline.undo_stack.push(commands.DeleteTrackCommand(timeline, track))
    assert track not in timeline.tracks

    timeline.undo_stack.undo()
    assert track in timeline.tracks
    # Restored at its original index.
    assert timeline.tracks.index(track) == 0

    timeline.undo_stack.redo()
    assert track not in timeline.tracks


def test_add_clip_command_undo_redo(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0)
    timeline.undo_stack.push(commands.AddClipCommand(timeline, track, clip))
    assert clip in track.clips
    assert timeline.selected_clip is clip

    timeline.undo_stack.undo()
    assert clip not in track.clips
    assert timeline.selected_clip is None

    timeline.undo_stack.redo()
    assert clip in track.clips
    assert timeline.selected_clip is clip


def test_delete_clip_command_undo_redo(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0)
    track.add_clip(clip)

    timeline.undo_stack.push(commands.DeleteClipCommand(timeline, track, clip))
    assert clip not in track.clips

    timeline.undo_stack.undo()
    assert clip in track.clips
    assert track.clips.index(clip) == 0

    timeline.undo_stack.redo()
    assert clip not in track.clips


def test_move_clip_command_same_track_undo_redo(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0)
    track.add_clip(clip)

    timeline.undo_stack.push(commands.MoveClipCommand(
        timeline, clip, track, 0, track, 100,
    ))
    assert clip.start_frame == 100

    timeline.undo_stack.undo()
    assert clip.start_frame == 0

    timeline.undo_stack.redo()
    assert clip.start_frame == 100


def test_move_clip_command_across_tracks_undo_redo(timeline, make_clip):
    track_a = AudioTrack(name="A")
    track_b = AudioTrack(name="B")
    timeline.add_track(track_a)
    timeline.add_track(track_b)
    clip = make_clip(start_frame=0, duration_sec=1.0)
    track_a.add_clip(clip)

    timeline.undo_stack.push(commands.MoveClipCommand(
        timeline, clip, track_a, 0, track_b, 50,
    ))
    assert clip not in track_a.clips
    assert clip in track_b.clips
    assert clip.start_frame == 50

    timeline.undo_stack.undo()
    assert clip in track_a.clips
    assert clip not in track_b.clips
    assert clip.start_frame == 0

    timeline.undo_stack.redo()
    assert clip in track_b.clips
    assert clip not in track_a.clips


def test_trim_clip_command_undo_redo(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=2.0, fps=24)
    track.add_clip(clip)

    timeline.undo_stack.push(commands.TrimClipCommand(
        timeline, clip,
        0.0, 0.0, 0,
        0.5, 0.25, 12,
    ))
    assert clip.trim_in_sec == 0.5
    assert clip.trim_out_sec == 0.25
    assert clip.start_frame == 12

    timeline.undo_stack.undo()
    assert clip.trim_in_sec == 0.0
    assert clip.trim_out_sec == 0.0
    assert clip.start_frame == 0

    timeline.undo_stack.redo()
    assert clip.trim_in_sec == 0.5


def test_split_clip_command_undo_redo(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=2.0, fps=24)  # 48 frames
    track.add_clip(clip)
    timeline.current_frame = 24

    timeline.undo_stack.push(commands.SplitClipCommand(timeline, track, clip, 24))
    assert len(track.clips) == 2
    left, right = track.clips
    assert left.end_frame == 24
    assert right.start_frame == 24
    assert clip not in track.clips

    timeline.undo_stack.undo()
    assert track.clips == [clip]

    timeline.undo_stack.redo()
    assert len(track.clips) == 2
    assert track.clips[0] is not clip


def test_split_clip_command_remaps_multipoint_volume(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=2.0, fps=24)
    clip.volume_points = [(0.0, 1.0), (0.25, 0.2), (0.5, 0.9), (0.75, 0.3), (1.0, 1.0)]
    track.add_clip(clip)

    timeline.undo_stack.push(commands.SplitClipCommand(timeline, track, clip, 24))
    left, right = track.clips
    # Each sibling's envelope must still start/end at unity-adjacent values
    # consistent with the original curve (no audible jump at the seam).
    assert left.volume_points[-1][1] == right.volume_points[0][1]


def test_set_clip_volume_command_undo_redo(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0)
    track.add_clip(clip)
    old_points = list(clip.volume_points)
    new_points = [(0.0, 0.5), (1.0, 0.5)]

    timeline.undo_stack.push(commands.SetClipVolumeCommand(timeline, clip, old_points, new_points))
    assert clip.volume_points == new_points

    timeline.undo_stack.undo()
    assert clip.volume_points == old_points

    timeline.undo_stack.redo()
    assert clip.volume_points == new_points


def test_mute_track_command_undo_redo(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    track.add_clip(make_clip(start_frame=0))

    timeline.undo_stack.push(commands.MuteTrackCommand(timeline, track))
    assert track.muted is True

    timeline.undo_stack.undo()
    assert track.muted is False

    timeline.undo_stack.redo()
    assert track.muted is True


def test_mute_track_command_affects_audio_only_with_clips(timeline):
    empty_track = AudioTrack(name="Empty")
    timeline.add_track(empty_track)
    cmd = commands.MuteTrackCommand(timeline, empty_track)
    assert cmd.affects_audio is False


def test_rename_track_command_undo_redo(timeline):
    track = AudioTrack(name="Old Name")
    timeline.add_track(track)

    timeline.undo_stack.push(commands.RenameTrackCommand(timeline, track, "New Name"))
    assert track.name == "New Name"

    timeline.undo_stack.undo()
    assert track.name == "Old Name"

    timeline.undo_stack.redo()
    assert track.name == "New Name"


def test_content_changed_signal_reflects_affects_audio(timeline, make_clip, qtbot):
    track = AudioTrack(name="T1")
    seen = []
    timeline.contentChanged.connect(lambda affects: seen.append(affects))

    timeline.undo_stack.push(commands.AddTrackCommand(timeline, track))
    assert seen[-1] is False  # empty track add never affects audio

    clip = make_clip(start_frame=0)
    timeline.undo_stack.push(commands.AddClipCommand(timeline, track, clip))
    assert seen[-1] is True  # adding a clip does


def test_undo_stack_can_undo_redo_multiple_command_types_in_sequence(timeline, make_clip):
    """A realistic edit session -- add track, add clip, move it, trim it,
    mute the track -- must all unwind and replay cleanly in undo/redo
    order, matching what repeated Ctrl+Z/Ctrl+Y does in the app."""
    track = AudioTrack(name="T1")
    timeline.undo_stack.push(commands.AddTrackCommand(timeline, track))
    clip = make_clip(start_frame=0, duration_sec=2.0, fps=24)
    timeline.undo_stack.push(commands.AddClipCommand(timeline, track, clip))
    timeline.undo_stack.push(commands.MoveClipCommand(timeline, clip, track, 0, track, 50))
    timeline.undo_stack.push(commands.TrimClipCommand(timeline, clip, 0.0, 0.0, 50, 0.2, 0.0, 55))
    timeline.undo_stack.push(commands.MuteTrackCommand(timeline, track))

    assert track.muted is True
    assert clip.trim_in_sec == 0.2
    assert clip.start_frame == 55

    for _ in range(5):
        timeline.undo_stack.undo()
    assert timeline.tracks == []

    for _ in range(5):
        timeline.undo_stack.redo()
    assert track.muted is True
    assert clip.trim_in_sec == 0.2
    assert clip in track.clips
