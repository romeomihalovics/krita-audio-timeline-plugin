"""MixdownController: the background-render coalescing (cancel whatever's
in flight and queue the newest edit instead of racing two renders),
skipping a redundant render when Krita already has audio attached, the
"Krita build predates setAudioTracks()" warning path, and export_mixdown.
Uses a real MixdownWorker QThread rendering real (generated) audio -- not
mocked -- so the coalescing behavior is exercised against actual timing.
"""

from unittest.mock import MagicMock, patch

from PyQt5.QtCore import QObject
from PyQt5.QtWidgets import QFileDialog, QMessageBox

from ..ui.mixdown_controller import MixdownController
from ..ui.state_persistence import DocStateStore
from ..ui.timeline_widget import AudioTimelineWidget
from ..audio.audio_track import AudioTrack
from .helpers import FakeAnnotatedDocument, NoAudioApiDocument


class FakeDocker(QObject):
    def __init__(self, timeline):
        super().__init__()
        self.timeline = timeline
        self.state_store = DocStateStore(self)
        self.playback = MagicMock()
        self.busy_history = []

    def _set_mixdown_busy(self, busy):
        self.busy_history.append(busy)


def make_controller(qtbot):
    timeline = AudioTimelineWidget()
    qtbot.addWidget(timeline)
    docker = FakeDocker(timeline)
    controller = MixdownController(docker)
    return docker, controller


def _wait_idle(qtbot, controller, timeout=3000):
    qtbot.waitUntil(lambda: controller._thread is None, timeout=timeout)


# ------------------------------------------------------------- audio API
def test_audio_api_available_true_for_conforming_document(qtbot):
    docker, controller = make_controller(qtbot)
    doc = FakeAnnotatedDocument()
    with patch.object(QMessageBox, "warning") as mock_warning:
        assert controller.audio_api_available(doc) is True
    mock_warning.assert_not_called()


def test_audio_api_available_false_warns_once_for_old_krita(qtbot):
    docker, controller = make_controller(qtbot)
    doc = NoAudioApiDocument()
    with patch.object(QMessageBox, "warning") as mock_warning:
        assert controller.audio_api_available(doc) is False
        assert controller.audio_api_available(doc) is False  # second call: no repeat warning
    mock_warning.assert_called_once()


def test_mixdown_already_attached_false_when_api_unavailable(qtbot):
    docker, controller = make_controller(qtbot)
    doc = NoAudioApiDocument()
    with patch.object(QMessageBox, "warning"):
        assert controller.mixdown_already_attached(doc) is False


def test_mixdown_already_attached_reflects_doc_audio_tracks(qtbot):
    docker, controller = make_controller(qtbot)
    doc = FakeAnnotatedDocument()
    assert controller.mixdown_already_attached(doc) is False
    doc.setAudioTracks(["/some/path.wav"])
    assert controller.mixdown_already_attached(doc) is True


# --------------------------------------------------------------- mixdown path
def test_mixdown_path_for_is_per_document_and_tracked(qtbot):
    docker, controller = make_controller(qtbot)
    doc = FakeAnnotatedDocument()
    path = controller.mixdown_path_for(doc)
    assert path in controller.known_mixdown_paths
    assert controller.mixdown_path_for(doc) == path  # stable across calls


# ------------------------------------------------------------- render/apply
def test_render_and_apply_with_no_clips_clears_krita_audio(qtbot):
    docker, controller = make_controller(qtbot)
    doc = FakeAnnotatedDocument()
    doc.setAudioTracks(["/stale/path.wav"])
    controller.render_and_apply(doc)
    assert doc.audioTracks() == []
    assert controller._thread is None


def test_render_and_apply_renders_and_applies_to_document(qtbot, make_clip):
    docker, controller = make_controller(qtbot)
    doc = FakeAnnotatedDocument()
    track = AudioTrack(name="T1")
    docker.timeline.add_track(track)
    track.add_clip(make_clip(start_frame=0, duration_sec=0.5, fps=24))

    controller.render_and_apply(doc)
    _wait_idle(qtbot, controller)

    assert len(doc.audioTracks()) == 1
    import os
    assert os.path.exists(doc.audioTracks()[0])
    assert doc.audioLevel() == 1.0  # bumped up from 0 on first successful apply
    assert docker.busy_history == [True, False]


def test_render_and_apply_coalesces_overlapping_renders(qtbot, make_clip):
    docker, controller = make_controller(qtbot)
    doc = FakeAnnotatedDocument()
    docker.playback.active_document.return_value = doc
    track = AudioTrack(name="T1")
    docker.timeline.add_track(track)
    track.add_clip(make_clip(start_frame=0, duration_sec=0.5, fps=24))

    controller.render_and_apply(doc)
    # QThread.start() makes isRunning() true synchronously before the new
    # thread actually begins executing run() -- so calling render_and_apply
    # again immediately is guaranteed to hit the "already in flight" branch,
    # not a race.
    assert controller._thread is not None and controller._thread.isRunning()
    controller.render_and_apply(doc)
    assert controller._pending_doc is doc

    _wait_idle(qtbot, controller)
    assert controller._pending_doc is None
    assert len(doc.audioTracks()) == 1


def test_cancel_inflight_and_drag_settled_discharges_owed_render(qtbot, make_clip):
    docker, controller = make_controller(qtbot)
    doc = FakeAnnotatedDocument()
    docker.playback.active_document.return_value = doc
    track = AudioTrack(name="T1")
    docker.timeline.add_track(track)
    track.add_clip(make_clip(start_frame=0, duration_sec=0.5, fps=24))

    controller.render_and_apply(doc)
    assert controller._thread is not None
    controller.cancel_inflight()
    assert controller._render_owed is True

    controller.drag_settled()
    assert controller._render_owed is False

    _wait_idle(qtbot, controller)
    assert len(doc.audioTracks()) == 1  # the owed render still eventually landed


def test_drag_settled_is_noop_without_owed_render(qtbot):
    docker, controller = make_controller(qtbot)
    docker.playback.active_document.return_value = None
    controller.drag_settled()  # nothing owed -- must not touch playback/active_document meaningfully
    assert controller._thread is None


# ----------------------------------------------------------------- apply/clear
def test_apply_to_krita_bumps_zero_audio_level(qtbot, tmp_path):
    docker, controller = make_controller(qtbot)
    doc = FakeAnnotatedDocument()
    path = str(tmp_path / "mix.wav")
    controller.apply_to_krita(doc, path)
    assert doc.audioTracks() == [path]
    assert doc.audioLevel() == 1.0


def test_apply_to_krita_preserves_existing_nonzero_level(qtbot, tmp_path):
    docker, controller = make_controller(qtbot)
    doc = FakeAnnotatedDocument()
    doc.setAudioLevel(0.4)
    path = str(tmp_path / "mix.wav")
    controller.apply_to_krita(doc, path)
    assert doc.audioLevel() == 0.4


def test_clear_krita_audio_sets_empty_list(qtbot):
    docker, controller = make_controller(qtbot)
    doc = FakeAnnotatedDocument()
    doc.setAudioTracks(["/some/path.wav"])
    controller.clear_krita_audio(doc)
    assert doc.audioTracks() == []


# --------------------------------------------------------------- export_mixdown
def test_export_mixdown_with_nothing_rendered_yet_shows_message(qtbot):
    docker, controller = make_controller(qtbot)
    doc = FakeAnnotatedDocument()
    with patch.object(QMessageBox, "information") as mock_info:
        controller.export_mixdown(doc)
    mock_info.assert_called_once()


def test_export_mixdown_copies_rendered_file_to_chosen_destination(qtbot, tmp_path, make_clip):
    docker, controller = make_controller(qtbot)
    doc = FakeAnnotatedDocument(file_name=str(tmp_path / "project.kra"))
    track = AudioTrack(name="T1")
    docker.timeline.add_track(track)
    track.add_clip(make_clip(start_frame=0, duration_sec=0.3, fps=24))
    controller.render_and_apply(doc)
    _wait_idle(qtbot, controller)

    dest = tmp_path / "exported.wav"
    with patch.object(QFileDialog, "getSaveFileName", return_value=(str(dest), "")):
        controller.export_mixdown(doc)
    assert dest.exists()
