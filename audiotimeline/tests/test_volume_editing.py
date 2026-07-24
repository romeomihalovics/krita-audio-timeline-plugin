"""VolumeEditingMixin: entering/exiting per-clip volume-editing mode, the
flat (<=2 point) gain-line drag, and the multi-point envelope (adding a
bend point via double-click, removing one via Delete/double-click/right-
click, and the exact-percentage dialog) -- both the simple flat-line case
and the curved multi-point case the user asked to have covered.
"""

from unittest.mock import patch

from PyQt5.QtCore import QPoint

from ..audio.audio_track import AudioTrack
from ..ui.timeline_constants import TRACK_HEIGHT
from .test_timeline_interaction import _press, _move, _release, _key
from PyQt5.QtCore import Qt


def _enter_editing(timeline, track_idx, clip):
    clip_rect = timeline._clip_rect_for(track_idx, clip)
    sticky_left, _ = timeline.visible_x_range()
    indicator_rect = timeline._sticky_volume_indicator_rect(clip, clip_rect, sticky_left)
    _press(timeline, indicator_rect.center())
    assert timeline.volume_editing_clip is clip


def _line_pos(timeline, track_idx, clip):
    clip_rect = timeline._clip_rect_for(track_idx, clip)
    x = clip_rect.center().x()
    y = int(timeline._volume_line_y_for(clip, clip_rect))
    return QPoint(x, y)


def test_enter_and_apply_volume_editing(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)

    _enter_editing(timeline, 0, clip)
    assert timeline._volume_edit_entry_points == [(0.0, 1.0), (1.0, 1.0)]

    clip_rect = timeline._clip_rect_for(0, clip)
    sticky_left, _ = timeline.visible_x_range()
    apply_rect, _cancel_rect = timeline._sticky_volume_action_rects(clip_rect, sticky_left)
    _press(timeline, apply_rect.center())
    assert timeline.volume_editing_clip is None


def test_cancel_volume_editing_reverts_changes(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)

    _enter_editing(timeline, 0, clip)
    pos = _line_pos(timeline, 0, clip)
    _press(timeline, pos)
    # Drag down (not up) -- the cancel icon sits near the clip's top edge,
    # so dragging the gain line up here would land the next press (on the
    # cancel icon) back on the line itself instead.
    _move(timeline, QPoint(pos.x(), pos.y() + 15))
    _release(timeline, QPoint(pos.x(), pos.y() + 15))
    assert clip.volume_points != [(0.0, 1.0), (1.0, 1.0)]

    clip_rect = timeline._clip_rect_for(0, clip)
    sticky_left, _ = timeline.visible_x_range()
    _apply_rect, cancel_rect = timeline._sticky_volume_action_rects(clip_rect, sticky_left)
    _press(timeline, cancel_rect.center())
    assert clip.volume_points == [(0.0, 1.0), (1.0, 1.0)]


def test_drag_flat_line_changes_both_endpoints_together(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)

    _enter_editing(timeline, 0, clip)
    pos = _line_pos(timeline, 0, clip)
    _press(timeline, pos)
    _move(timeline, QPoint(pos.x(), pos.y() + 20))  # drag down -- gain should drop
    _release(timeline, QPoint(pos.x(), pos.y() + 20))

    assert clip.volume_points[0][1] < 1.0
    assert clip.volume_points[0][1] == clip.volume_points[1][1]
    assert timeline.undo_stack.count() == 1

    timeline.undo_stack.undo()
    assert clip.volume_points == [(0.0, 1.0), (1.0, 1.0)]


def test_double_click_on_curve_adds_bend_point(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)
    _enter_editing(timeline, 0, clip)

    clip_rect = timeline._clip_rect_for(0, clip)
    # Somewhere in the middle of the flat line, away from either endpoint.
    mid_x = clip_rect.left() + clip_rect.width() // 2
    line_y = int(timeline._volume_line_y_for(clip, clip_rect))
    from PyQt5.QtCore import QEvent, QPointF
    from PyQt5.QtGui import QMouseEvent

    event = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(mid_x, line_y),
                         Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    timeline.mouseDoubleClickEvent(event)

    assert len(clip.volume_points) == 3
    assert timeline.selected_volume_point_index is not None


def test_remove_bend_point_via_delete_key(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    clip.volume_points = [(0.0, 1.0), (0.5, 0.3), (1.0, 1.0)]
    track.add_clip(clip)
    _enter_editing(timeline, 0, clip)
    timeline.selected_volume_point_index = 1

    _key(timeline, Qt.Key_Delete)
    assert clip.volume_points == [(0.0, 1.0), (1.0, 1.0)]


def test_cannot_remove_endpoint_via_delete_key(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    clip.volume_points = [(0.0, 1.0), (0.5, 0.3), (1.0, 1.0)]
    track.add_clip(clip)
    _enter_editing(timeline, 0, clip)
    timeline.selected_volume_point_index = 0  # the left endpoint

    _key(timeline, Qt.Key_Delete)
    assert len(clip.volume_points) == 3


def test_remove_bend_point_via_double_click(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    clip.volume_points = [(0.0, 1.0), (0.5, 0.3), (1.0, 1.0)]
    track.add_clip(clip)
    _enter_editing(timeline, 0, clip)

    clip_rect = timeline._clip_rect_for(0, clip)
    screen_pos = timeline._volume_point_screen_pos(clip, clip_rect, (0.5, 0.3))
    px, py = screen_pos

    from PyQt5.QtCore import QEvent, QPointF
    from PyQt5.QtGui import QMouseEvent
    event = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(px, py),
                         Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    timeline.mouseDoubleClickEvent(event)
    assert clip.volume_points == [(0.0, 1.0), (1.0, 1.0)]


def test_remove_bend_point_via_right_click_menu(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    clip.volume_points = [(0.0, 1.0), (0.5, 0.3), (1.0, 1.0)]
    track.add_clip(clip)
    _enter_editing(timeline, 0, clip)

    clip_rect = timeline._clip_rect_for(0, clip)
    px, py = timeline._volume_point_screen_pos(clip, clip_rect, (0.5, 0.3))
    pos = QPoint(int(px), int(py))

    def fake_exec(self, globalpos):
        for action in self.actions():
            if action.text() == "Remove Point":
                return action
        return None

    event = type("Evt", (), {"pos": lambda self: pos, "globalPos": lambda self: pos})()
    with patch("PyQt5.QtWidgets.QMenu.exec_", new=fake_exec):
        timeline.contextMenuEvent(event)
    assert clip.volume_points == [(0.0, 1.0), (1.0, 1.0)]


def test_set_exact_percentage_via_dialog(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)
    _enter_editing(timeline, 0, clip)

    with patch("audiotimeline.ui.timeline_volume_editing.QInputDialog.getInt", return_value=(55, True)):
        timeline._prompt_volume_percentage(clip, 0)
    assert clip.volume_points == [(0.0, 0.55), (1.0, 0.55)]


def test_exiting_with_unsaved_changes_prompts_and_respects_choice(timeline, make_clip):
    from PyQt5.QtWidgets import QMessageBox
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip_a = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    clip_b = make_clip(start_frame=100, duration_sec=1.0, fps=24)
    track.add_clip(clip_a)
    track.add_clip(clip_b)
    _enter_editing(timeline, 0, clip_a)

    pos = _line_pos(timeline, 0, clip_a)
    _press(timeline, pos)
    _move(timeline, QPoint(pos.x(), pos.y() + 20))
    _release(timeline, QPoint(pos.x(), pos.y() + 20))
    changed_points = list(clip_a.volume_points)
    assert changed_points != [(0.0, 1.0), (1.0, 1.0)]

    # Clicking a different clip while unsaved changes exist prompts;
    # choosing Discard should revert clip_a's envelope.
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Discard):
        b_pos = _line_pos(timeline, 0, clip_b) if False else QPoint(
            timeline.frame_to_x(clip_b.start_frame) + 5, TRACK_HEIGHT // 2
        )
        _press(timeline, b_pos)
    assert clip_a.volume_points == [(0.0, 1.0), (1.0, 1.0)]
