"""InteractionMixin: mouse-driven clip move/trim/split/delete and their
overlap constraints, plus the keyboard shortcuts (Delete, S-to-split,
Ctrl+Z/Ctrl+Y) and the right-click "Delete Clip" context menu. Mouse/key
events are constructed directly (QMouseEvent/QKeyEvent) and dispatched via
the widget's own event handlers rather than the real platform event loop,
so these run identically under the offscreen QPA platform.
"""

from unittest.mock import patch

from PyQt5.QtCore import QPoint, QPointF
from PyQt5.QtGui import QKeyEvent, QMouseEvent

from .. import qtcompat
from ..audio.audio_track import AudioTrack
from ..ui.timeline_constants import TRACK_HEIGHT


def _press(widget, pos, modifiers=qtcompat.NO_MODIFIER, button=qtcompat.LEFT_BUTTON):
    event = QMouseEvent(qtcompat.EVENT_MOUSE_BUTTON_PRESS, QPointF(pos), button, button, modifiers)
    widget.mousePressEvent(event)


def _move(widget, pos, modifiers=qtcompat.NO_MODIFIER):
    event = QMouseEvent(qtcompat.EVENT_MOUSE_MOVE, QPointF(pos), qtcompat.NO_BUTTON, qtcompat.LEFT_BUTTON, modifiers)
    widget.mouseMoveEvent(event)


def _release(widget, pos, button=qtcompat.LEFT_BUTTON):
    event = QMouseEvent(qtcompat.EVENT_MOUSE_BUTTON_RELEASE, QPointF(pos), button, qtcompat.NO_BUTTON, qtcompat.NO_MODIFIER)
    widget.mouseReleaseEvent(event)


def _key(widget, key, modifiers=qtcompat.NO_MODIFIER):
    event = QKeyEvent(qtcompat.EVENT_KEY_PRESS, key, modifiers)
    widget.keyPressEvent(event)


def _clip_center_pos(timeline, track_idx, clip):
    x = (timeline.frame_to_x(clip.start_frame) + timeline.frame_to_x(clip.end_frame)) // 2
    y = timeline.track_y(track_idx) + TRACK_HEIGHT // 2
    return QPoint(x, y)


# ------------------------------------------------------------------- move
def test_drag_moves_clip_within_track_and_pushes_undoable_command(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)

    start_pos = _clip_center_pos(timeline, 0, clip)
    _press(timeline, start_pos)
    assert timeline.selected_clip is clip

    target_x = timeline.frame_to_x(100) + (start_pos.x() - timeline.frame_to_x(clip.start_frame))
    _move(timeline, QPoint(target_x, start_pos.y()))
    assert clip.start_frame > 0

    _release(timeline, QPoint(target_x, start_pos.y()))
    assert timeline.undo_stack.count() == 1

    timeline.undo_stack.undo()
    assert clip.start_frame == 0


def test_drag_cannot_overlap_another_clip_on_same_track(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    blocker = make_clip(start_frame=100, duration_sec=1.0, fps=24)  # [100, 124)
    moving = make_clip(start_frame=0, duration_sec=1.0, fps=24)     # [0, 24)
    track.add_clip(blocker)
    track.add_clip(moving)

    start_pos = _clip_center_pos(timeline, 0, moving)
    _press(timeline, start_pos)
    # Drag far enough right that, unclamped, it would land on top of `blocker`.
    target_x = timeline.frame_to_x(105)
    _move(timeline, QPoint(target_x, start_pos.y()))
    _release(timeline, QPoint(target_x, start_pos.y()))

    assert not (moving.start_frame < blocker.end_frame and moving.end_frame > blocker.start_frame)


def test_drag_no_op_release_pushes_no_command(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)

    pos = _clip_center_pos(timeline, 0, clip)
    _press(timeline, pos)
    _release(timeline, pos)  # released at the exact same spot -- no real move
    assert timeline.undo_stack.count() == 0


def test_drag_moves_clip_to_a_different_track(timeline, make_clip):
    track_a = AudioTrack(name="A")
    track_b = AudioTrack(name="B")
    timeline.add_track(track_a)
    timeline.add_track(track_b)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track_a.add_clip(clip)

    pos = _clip_center_pos(timeline, 0, clip)
    _press(timeline, pos)
    drop_pos = QPoint(pos.x(), timeline.track_y(1) + TRACK_HEIGHT // 2)
    _move(timeline, drop_pos)
    _release(timeline, drop_pos)

    assert clip in track_b.clips
    assert clip not in track_a.clips
    timeline.undo_stack.undo()
    assert clip in track_a.clips


# ------------------------------------------------------------------- trim
def test_trim_left_edge_shortens_from_start(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=2.0, fps=24)  # 48 frames
    track.add_clip(clip)

    left_x = timeline.frame_to_x(clip.start_frame)
    y = TRACK_HEIGHT // 2
    _press(timeline, QPoint(left_x, y))
    assert timeline._drag_mode == "trim_left"

    new_x = timeline.frame_to_x(12)
    _move(timeline, QPoint(new_x, y))
    _release(timeline, QPoint(new_x, y))

    assert clip.trim_in_sec > 0
    assert clip.end_frame == 48  # right edge stays fixed on a left-edge trim
    timeline.undo_stack.undo()
    assert clip.trim_in_sec == 0.0


def test_trim_right_edge_cannot_cross_next_clip(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=2.0, fps=24)  # [0, 48)
    neighbor = make_clip(start_frame=60, duration_sec=1.0, fps=24)  # [60, 84)
    track.add_clip(clip)
    track.add_clip(neighbor)

    right_x = timeline.frame_to_x(clip.end_frame)
    y = TRACK_HEIGHT // 2
    _press(timeline, QPoint(right_x, y))
    assert timeline._drag_mode == "trim_right"

    # Drag far to the right, well past the neighbor's start.
    far_x = timeline.frame_to_x(90)
    _move(timeline, QPoint(far_x, y))
    _release(timeline, QPoint(far_x, y))

    assert clip.end_frame <= neighbor.start_frame


def test_trim_cannot_shrink_clip_to_less_than_one_frame(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)

    right_x = timeline.frame_to_x(clip.end_frame)
    y = TRACK_HEIGHT // 2
    _press(timeline, QPoint(right_x, y))
    # Drag all the way back to (and past) the clip's start.
    _move(timeline, QPoint(timeline.frame_to_x(0) - 200, y))
    _release(timeline, QPoint(timeline.frame_to_x(0) - 200, y))
    assert clip.length_frames >= 1


# ------------------------------------------------------------------- split
def test_split_selected_clip_at_playhead(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=2.0, fps=24)  # 48 frames
    track.add_clip(clip)
    timeline.selected_clip = clip
    timeline.current_frame = 24

    assert timeline.can_split_selected_clip() is True
    timeline.split_selected_clip()
    assert len(track.clips) == 2


def test_cannot_split_outside_clip_bounds(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=2.0, fps=24)
    track.add_clip(clip)
    timeline.selected_clip = clip
    timeline.current_frame = 1000  # well outside the clip

    assert timeline.can_split_selected_clip() is False
    timeline.split_selected_clip()
    assert len(track.clips) == 1


def test_s_key_splits_selected_clip(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=2.0, fps=24)
    track.add_clip(clip)
    timeline.selected_clip = clip
    timeline.current_frame = 24

    _key(timeline, qtcompat.KEY_S)
    assert len(track.clips) == 2


# ------------------------------------------------------------------ delete
def test_delete_key_removes_selected_clip(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)
    timeline.selected_clip = clip

    _key(timeline, qtcompat.KEY_DELETE)
    assert clip not in track.clips

    timeline.undo_stack.undo()
    assert clip in track.clips


def test_right_click_delete_clip_via_context_menu(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)

    def fake_exec(self, pos):
        for action in self.actions():
            if action.text() == "Delete Clip":
                return action
        return None

    pos = _clip_center_pos(timeline, 0, clip)
    event = type("Evt", (), {"pos": lambda self: pos, "globalPos": lambda self: pos})()
    with patch("PyQt5.QtWidgets.QMenu.exec", new=fake_exec):
        timeline.contextMenuEvent(event)

    assert clip not in track.clips


# --------------------------------------------------------------- undo/redo
def test_ctrl_z_and_ctrl_y_shortcuts_undo_redo(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)
    timeline.selected_clip = clip

    _key(timeline, qtcompat.KEY_DELETE)
    assert clip not in track.clips

    _key(timeline, qtcompat.KEY_Z, qtcompat.CONTROL_MODIFIER)
    assert clip in track.clips

    _key(timeline, qtcompat.KEY_Y, qtcompat.CONTROL_MODIFIER)
    assert clip not in track.clips
