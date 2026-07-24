"""AudioTimelineRulerWidget: the "nice" tick/label spacing math (coarser
round-second steps zoomed out, finer fps-divisor sub-second steps zoomed
in, ticks always an even subdivision of the label spacing) across a range
of fps/zoom combinations, plus dragging the ruler to scrub the playhead.
"""

import pytest
from PyQt5.QtCore import QPoint

from ..ui.ruler_widget import AudioTimelineRulerWidget
from ..ui.timeline_constants import RULER_MIN_TICK_SPACING_PX, RULER_MIN_LABEL_SPACING_PX
from .test_timeline_interaction import _press, _move, _release


@pytest.fixture
def ruler(timeline, qtbot):
    r = AudioTimelineRulerWidget(timeline)
    qtbot.addWidget(r)
    r.resize(2000, 24)
    return r


# ------------------------------------------------------- _tick_step_frames
@pytest.mark.parametrize("fps", [24, 25, 30, 60, 1])
@pytest.mark.parametrize("px_per_frame", [0.5, 1.0, 4.0, 20.0, 40.0])
def test_tick_step_frames_is_at_least_one_and_gives_enough_spacing(timeline, ruler, fps, px_per_frame):
    timeline.fps = fps
    timeline.px_per_frame = px_per_frame
    step = ruler._tick_step_frames(RULER_MIN_TICK_SPACING_PX)
    assert step >= 1
    # The chosen step must actually satisfy (or, for the coarsest possible
    # step near NICE_SECOND_STEPS' ceiling, get as close as a "nice" step
    # can) the requested minimum on-screen spacing.
    assert step * px_per_frame >= RULER_MIN_TICK_SPACING_PX - 1e-6 or step == fps * 3600


def test_tick_step_frames_prefers_fps_divisor_when_zoomed_in(timeline, ruler):
    timeline.fps = 24
    timeline.px_per_frame = 20.0  # zoomed in a lot -- 1 frame alone exceeds min spacing
    step = ruler._tick_step_frames(RULER_MIN_TICK_SPACING_PX)
    assert step in (1, 2, 3, 4, 6, 8, 12, 24)  # a divisor of 24, not an arbitrary frame count


def test_tick_step_frames_falls_back_to_nice_seconds_when_zoomed_out(timeline, ruler):
    timeline.fps = 24
    timeline.px_per_frame = 0.05  # zoomed out far -- needs multi-second steps
    step = ruler._tick_step_frames(RULER_MIN_LABEL_SPACING_PX)
    assert step % (24) == 0  # a whole number of seconds
    seconds = step // 24
    from ..ui.timeline_constants import NICE_SECOND_STEPS
    assert seconds in NICE_SECOND_STEPS


def test_tick_step_frames_fps_of_one_never_divides_by_zero(timeline, ruler):
    timeline.fps = 1
    timeline.px_per_frame = 4.0
    step = ruler._tick_step_frames(RULER_MIN_TICK_SPACING_PX)
    assert step >= 1


# ------------------------------------------------- _label_and_tick_step_frames
@pytest.mark.parametrize("fps", [24, 30, 60])
@pytest.mark.parametrize("px_per_frame", [0.2, 1.0, 4.0, 15.0])
def test_tick_step_evenly_divides_label_step(timeline, ruler, fps, px_per_frame):
    timeline.fps = fps
    timeline.px_per_frame = px_per_frame
    label_step, tick_step = ruler._label_and_tick_step_frames()
    assert tick_step >= 1
    assert label_step % tick_step == 0


def test_label_step_is_at_least_as_coarse_as_tick_step(timeline, ruler):
    timeline.fps = 24
    timeline.px_per_frame = 4.0
    label_step, tick_step = ruler._label_and_tick_step_frames()
    assert label_step >= tick_step


# --------------------------------------------------------------------- paint
def test_paint_event_runs_without_error(timeline, ruler):
    from PyQt5.QtCore import QRect
    from PyQt5.QtGui import QPaintEvent
    timeline.add_track(__import__(
        "audiotimeline.audio.audio_track", fromlist=["AudioTrack"]
    ).AudioTrack(name="T1"))
    ruler.paintEvent(QPaintEvent(QRect(0, 0, ruler.width(), ruler.height())))


# ------------------------------------------------------------------- scrubbing
def test_dragging_ruler_scrubs_playhead(timeline, ruler):
    scrubbed = []
    timeline.scrubbed.connect(lambda frame: scrubbed.append(frame))

    _press(ruler, QPoint(40, 10))
    assert ruler._dragging is True
    assert timeline.current_frame == timeline.x_to_frame(40)
    assert scrubbed[-1] == timeline.current_frame

    _move(ruler, QPoint(80, 10))
    assert timeline.current_frame == timeline.x_to_frame(80)

    _release(ruler, QPoint(80, 10))
    assert ruler._dragging is False


def test_moving_without_press_does_not_scrub(timeline, ruler):
    before = timeline.current_frame
    _move(ruler, QPoint(200, 10))
    assert timeline.current_frame == before
