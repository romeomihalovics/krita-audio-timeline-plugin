"""AudioTimelineWidget core + AudioTimelineHeaderWidget: adding/removing/
renaming/muting tracks through the actual UI (header widget clicks/double-
clicks, not just the underlying commands), zoom clamping, and the
relayout/resize machinery that reacts to total_frames changes (the
animation end-frame) and to zooming -- plus paint-time viewport culling
(only visible tracks/clips get drawn).
"""

from unittest.mock import MagicMock, patch

from PyQt5.QtCore import QPoint, QPointF, QRect, Qt
from PyQt5.QtGui import QPaintEvent, QWheelEvent

from ..ui.header_widget import AudioTimelineHeaderWidget
from ..ui.timeline_constants import TRACK_HEIGHT
from ..audio.audio_track import AudioTrack


def _wheel_event(delta, ctrl=True):
    mods = Qt.ControlModifier if ctrl else Qt.NoModifier
    return QWheelEvent(
        QPointF(0, 0), QPointF(0, 0), QPoint(0, 0), QPoint(0, delta),
        Qt.NoButton, mods, Qt.NoScrollPhase, False,
    )


# --------------------------------------------------------------- add/remove
def test_add_track_appends_and_relayouts(timeline):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    assert timeline.tracks == [track]
    assert timeline.sizeHint().height() == TRACK_HEIGHT


def test_remove_track_clamps_active_index(timeline):
    a, b = AudioTrack(name="A"), AudioTrack(name="B")
    timeline.add_track(a)
    timeline.add_track(b)
    timeline.active_track_index = 1
    timeline.remove_track(b)
    assert timeline.active_track_index == 0


def test_header_mute_button_toggles_via_click(timeline, qtbot):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    header = AudioTimelineHeaderWidget(timeline)
    qtbot.addWidget(header)
    header.resize(200, 200)

    mute_rect = timeline.mute_rect_for(0)
    qtbot.mouseClick(header, Qt.LeftButton, pos=mute_rect.center())
    assert track.muted is True

    qtbot.mouseClick(header, Qt.LeftButton, pos=mute_rect.center())
    assert track.muted is False


def test_header_delete_button_removes_track_via_click(timeline, qtbot):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    header = AudioTimelineHeaderWidget(timeline)
    qtbot.addWidget(header)
    header.resize(200, 200)

    delete_rect = timeline.delete_rect_for(0)
    qtbot.mouseClick(header, Qt.LeftButton, pos=delete_rect.center())
    assert track not in timeline.tracks

    timeline.undo_stack.undo()
    assert track in timeline.tracks


def test_header_click_on_name_sets_active_track(timeline, qtbot):
    a, b = AudioTrack(name="A"), AudioTrack(name="B")
    timeline.add_track(a)
    timeline.add_track(b)
    header = AudioTimelineHeaderWidget(timeline)
    qtbot.addWidget(header)
    header.resize(200, 200)

    name_pos = QPoint(10, timeline.track_y(1) + TRACK_HEIGHT // 2)
    qtbot.mouseClick(header, Qt.LeftButton, pos=name_pos)
    assert timeline.active_track_index == 1


def test_header_double_click_renames_track(timeline, qtbot):
    track = AudioTrack(name="Old Name")
    timeline.add_track(track)
    header = AudioTimelineHeaderWidget(timeline)
    qtbot.addWidget(header)
    header.resize(200, 200)

    name_pos = QPoint(10, timeline.track_y(0) + TRACK_HEIGHT // 2)
    with patch("audiotimeline.ui.header_widget.QInputDialog.getText", return_value=("Renamed", True)):
        qtbot.mouseDClick(header, Qt.LeftButton, pos=name_pos)
    assert track.name == "Renamed"

    timeline.undo_stack.undo()
    assert track.name == "Old Name"


def test_header_double_click_cancelled_does_not_rename(timeline, qtbot):
    track = AudioTrack(name="Old Name")
    timeline.add_track(track)
    header = AudioTimelineHeaderWidget(timeline)
    qtbot.addWidget(header)
    header.resize(200, 200)

    name_pos = QPoint(10, timeline.track_y(0) + TRACK_HEIGHT // 2)
    with patch("audiotimeline.ui.header_widget.QInputDialog.getText", return_value=("Renamed", False)):
        qtbot.mouseDClick(header, Qt.LeftButton, pos=name_pos)
    assert track.name == "Old Name"


def test_header_double_click_on_mute_button_does_not_rename(timeline, qtbot):
    track = AudioTrack(name="Old Name")
    timeline.add_track(track)
    header = AudioTimelineHeaderWidget(timeline)
    qtbot.addWidget(header)
    header.resize(200, 200)

    mute_rect = timeline.mute_rect_for(0)
    with patch("audiotimeline.ui.header_widget.QInputDialog.getText", return_value=("Renamed", True)) as mocked:
        qtbot.mouseDClick(header, Qt.LeftButton, pos=mute_rect.center())
    mocked.assert_not_called()
    assert track.name == "Old Name"


# --------------------------------------------------------------------- zoom
def test_wheel_ctrl_zooms_in_and_out(timeline):
    start = timeline.px_per_frame
    timeline.wheelEvent(_wheel_event(120, ctrl=True))
    assert timeline.px_per_frame > start

    zoomed_in = timeline.px_per_frame
    timeline.wheelEvent(_wheel_event(-120, ctrl=True))
    assert timeline.px_per_frame < zoomed_in


def test_wheel_zoom_clamped_to_bounds(timeline):
    timeline.px_per_frame = 40.0
    timeline.wheelEvent(_wheel_event(120, ctrl=True))
    assert timeline.px_per_frame <= 40.0

    timeline.px_per_frame = 0.5
    timeline.wheelEvent(_wheel_event(-120, ctrl=True))
    assert timeline.px_per_frame >= 0.5


def test_wheel_without_ctrl_does_not_zoom(timeline):
    start = timeline.px_per_frame
    timeline.wheelEvent(_wheel_event(120, ctrl=False))
    assert timeline.px_per_frame == start


# ------------------------------------------------------- resize/rerendering
def test_set_total_frames_grows_size_hint(timeline):
    timeline.set_total_frames(240)
    small_hint = timeline.sizeHint()
    timeline.set_total_frames(24000)
    big_hint = timeline.sizeHint()
    assert big_hint.width() > small_hint.width()


def test_set_total_frames_emits_layout_changed_only_when_hint_changes(timeline, qtbot):
    timeline.set_total_frames(240)  # settle at a baseline hint first
    seen = []
    timeline.layoutChanged.connect(lambda: seen.append(True))
    timeline.set_total_frames(241)  # tiny change, likely still changes width in px
    assert len(seen) >= 1
    seen.clear()
    timeline.set_total_frames(241)  # identical value again -- no-op, no signal
    assert seen == []


def test_content_end_frame_extends_past_animation_length_for_long_clip(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    timeline.set_total_frames(100)
    clip = make_clip(start_frame=90, duration_sec=2.0, fps=24)  # ends well past 100
    track.add_clip(clip)
    assert timeline._content_end_frame() == clip.end_frame
    assert timeline._content_end_frame() > 100


def test_set_fps_relayouts(timeline):
    timeline.set_fps(30)
    assert timeline.fps == 30
    timeline.set_fps(0)  # must clamp to at least 1, never divide by zero elsewhere
    assert timeline.fps == 1


# --------------------------------------------------------- viewport culling
def test_paint_clip_skips_entirely_when_scrolled_out_of_view(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)

    theme = timeline._theme_colors()
    painter = MagicMock()
    # Clip sits at x in [0, ~96); ask to paint a visible range entirely to
    # the right of it -- _paint_clip should bail out before touching the
    # painter at all.
    timeline._paint_clip(painter, 0, clip, theme, visible_left=10000, visible_right=20000)
    painter.setBrush.assert_not_called()
    painter.drawRoundedRect.assert_not_called()


def test_paint_clip_draws_when_in_view(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)

    theme = timeline._theme_colors()
    painter = MagicMock()
    timeline._paint_clip(painter, 0, clip, theme, visible_left=0, visible_right=2000)
    painter.drawRoundedRect.assert_called()


def test_paint_event_skips_tracks_scrolled_out_of_view_vertically(timeline):
    for i in range(10):
        timeline.add_track(AudioTrack(name=f"T{i}"))
    timeline.resize(timeline.sizeHint())

    calls = []
    original = timeline._paint_track

    def spy(painter, index, track, theme, vl, vr):
        calls.append(index)
        return original(painter, index, track, theme, vl, vr)

    with patch.object(timeline, "_paint_track", side_effect=spy):
        # Only expose rows for tracks 0 and 1 (TRACK_HEIGHT each).
        event = QPaintEvent(QRect(0, 0, timeline.width(), TRACK_HEIGHT * 2))
        timeline.paintEvent(event)

    assert 0 in calls and 1 in calls
    assert 9 not in calls
