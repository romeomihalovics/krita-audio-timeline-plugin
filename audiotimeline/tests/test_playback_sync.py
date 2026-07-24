"""PlaybackSync: keeps the timeline's current frame/fps/total_frames in
sync with whichever (fake) Krita document is active -- both on document/
view switches (canvas_changed) and continuously during polling (poll,
since Krita has no signal for animation-range edits or per-frame
playback), plus the auto-scroll-to-playhead and scrub-back-to-Krita
behavior. `active_document()` is overridden per test rather than routed
through the fake `krita` module, since PlaybackSync re-imports `krita`
fresh on every call.
"""

from unittest.mock import MagicMock

from PyQt5.QtCore import QObject
from PyQt5.QtWidgets import QScrollArea, QUndoGroup

from ..ui.playback_sync import PlaybackSync
from ..ui.state_persistence import DocStateStore
from ..ui.timeline_widget import AudioTimelineWidget
from ..audio.audio_track import AudioTrack
from .helpers import FakeAnnotatedDocument


class FakeDocker(QObject):
    def __init__(self, timeline):
        super().__init__()
        self.timeline = timeline
        self.state_store = DocStateStore(self)
        self.undo_group = QUndoGroup()
        self.mixdown = MagicMock()
        self.mixdown.mixdown_already_attached.return_value = True  # skip real render in tests
        self.add_track = lambda: self.timeline.add_track(AudioTrack(name="Track 1"))


def make_playback(qtbot):
    timeline = AudioTimelineWidget()
    qtbot.addWidget(timeline)
    docker = FakeDocker(timeline)
    playback = PlaybackSync(docker)
    return docker, playback


# ------------------------------------------------------------- canvas_changed
def test_canvas_changed_with_no_active_document_is_noop(qtbot):
    docker, playback = make_playback(qtbot)
    playback.active_document = lambda: None
    playback.canvas_changed(None)
    assert docker.timeline.tracks == []


def test_canvas_changed_syncs_fps_and_total_frames(qtbot):
    docker, playback = make_playback(qtbot)
    doc = FakeAnnotatedDocument(fps=30, frame_count=500)
    playback.active_document = lambda: doc
    playback.canvas_changed(None)
    assert docker.timeline.fps == 30
    assert docker.timeline.total_frames == 500


def test_canvas_changed_loads_state_for_new_document(qtbot):
    docker, playback = make_playback(qtbot)
    doc = FakeAnnotatedDocument()
    playback.active_document = lambda: doc
    playback.canvas_changed(None)
    # Fresh document with no saved annotation -- load_state_fresh's own
    # empty-doc fallback gives it one track.
    assert len(docker.timeline.tracks) == 1
    assert docker.state_store._loaded_doc_id == docker.state_store.doc_id(doc)


def test_canvas_changed_does_not_reload_same_document_twice(qtbot):
    docker, playback = make_playback(qtbot)
    doc = FakeAnnotatedDocument()
    playback.active_document = lambda: doc
    playback.canvas_changed(None)
    track = docker.timeline.tracks[0]

    playback.canvas_changed(None)  # same doc again
    assert docker.timeline.tracks == [track]  # untouched, not re-parsed


def test_canvas_changed_switches_between_two_documents(qtbot):
    docker, playback = make_playback(qtbot)
    doc_a = FakeAnnotatedDocument(fps=24)
    doc_b = FakeAnnotatedDocument(fps=60)
    playback.active_document = lambda: doc_a
    playback.canvas_changed(None)
    assert docker.timeline.fps == 24

    playback.active_document = lambda: doc_b
    playback.canvas_changed(None)
    assert docker.timeline.fps == 60


# ------------------------------------------------------------------------ poll
def test_poll_with_no_active_document_is_noop(qtbot):
    docker, playback = make_playback(qtbot)
    playback.active_document = lambda: None
    playback.poll()  # must not raise


def test_poll_updates_current_frame_when_krita_frame_changes(qtbot):
    docker, playback = make_playback(qtbot)
    doc = FakeAnnotatedDocument()
    doc.setCurrentTime(42)
    playback.active_document = lambda: doc
    playback.poll()
    assert docker.timeline.current_frame == 42


def test_poll_is_a_noop_when_nothing_changed(qtbot):
    docker, playback = make_playback(qtbot)
    doc = FakeAnnotatedDocument()
    playback.active_document = lambda: doc
    playback.poll()
    docker.timeline.current_frame = 999  # simulate no further doc-side change
    playback.poll()
    # doc.currentTime() is still 0, unchanged from the first poll -- a
    # second poll must not stomp on any user-driven change in between
    # (there isn't one here, but the point is it only writes on an actual
    # detected change).
    assert docker.timeline.current_frame == 999


def test_poll_detects_total_frames_change_and_triggers_rerender(qtbot):
    docker, playback = make_playback(qtbot)
    doc = FakeAnnotatedDocument(frame_count=240)
    playback.active_document = lambda: doc
    playback._last_total_frames = 240  # simulate already in sync (skip the first-poll baseline trigger)
    playback.poll()
    docker.mixdown.render_and_apply.assert_not_called()

    doc._frame_count = 480  # simulate the user dragging the animation end frame
    playback.poll()
    assert docker.timeline.total_frames == 480
    docker.mixdown.render_and_apply.assert_called_once_with(doc)


def test_poll_detects_fps_change(qtbot):
    docker, playback = make_playback(qtbot)
    doc = FakeAnnotatedDocument(fps=24)
    playback.active_document = lambda: doc
    playback.poll()
    doc._fps = 12
    playback.poll()
    assert docker.timeline.fps == 12


# --------------------------------------------------------- ensure_playhead_visible
def test_ensure_playhead_visible_without_scroll_area_is_noop(qtbot):
    docker, playback = make_playback(qtbot)
    playback.ensure_playhead_visible(moving_forward=True)  # no docker.scroll_area at all


def test_ensure_playhead_visible_scrolls_forward_to_reveal_playhead(qtbot):
    docker, playback = make_playback(qtbot)
    docker.timeline.set_total_frames(100000)  # plenty of scrollable width
    scroll_area = QScrollArea()
    scroll_area.setWidget(docker.timeline)
    scroll_area.setFixedWidth(300)
    qtbot.addWidget(scroll_area)
    docker.scroll_area = scroll_area

    docker.timeline.current_frame = 50000  # far off to the right of the viewport
    playback.ensure_playhead_visible(moving_forward=True)

    hbar = scroll_area.horizontalScrollBar()
    x = docker.timeline.frame_to_x(docker.timeline.current_frame)
    assert hbar.value() == x


def test_ensure_playhead_visible_noop_when_already_in_view(qtbot):
    docker, playback = make_playback(qtbot)
    docker.timeline.set_total_frames(1000)
    scroll_area = QScrollArea()
    scroll_area.setWidget(docker.timeline)
    scroll_area.setFixedWidth(300)
    qtbot.addWidget(scroll_area)
    docker.scroll_area = scroll_area

    docker.timeline.current_frame = 0
    before = scroll_area.horizontalScrollBar().value()
    playback.ensure_playhead_visible(moving_forward=True)
    assert scroll_area.horizontalScrollBar().value() == before


# --------------------------------------------------------------- scrub-to-krita
def test_on_timeline_scrubbed_sets_krita_current_time(qtbot):
    docker, playback = make_playback(qtbot)
    doc = FakeAnnotatedDocument()
    playback.active_document = lambda: doc
    playback.on_timeline_scrubbed(77)
    assert doc.currentTime() == 77
    assert playback._last_doc_frame == 77


def test_on_timeline_scrubbed_with_no_active_document_is_noop(qtbot):
    docker, playback = make_playback(qtbot)
    playback.active_document = lambda: None
    playback.on_timeline_scrubbed(77)  # must not raise
