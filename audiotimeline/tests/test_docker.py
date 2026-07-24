"""AudioTimelineDocker: the composition root. Instantiated for real (using
the fake `krita` module installed by the root conftest.py) so import_audio
(the actual toolbar-button import path), add_track, export_mixdown, close()
cleanup and the auto-update-check wiring all run through the real object,
not just the pieces it delegates to.
"""

from unittest.mock import MagicMock, patch

import pytest
from PyQt5.QtWidgets import QFileDialog, QMessageBox

from ..audiotimeline_docker import AudioTimelineDocker
from .. import updater
from .helpers import FakeAnnotatedDocument


@pytest.fixture
def docker(qtbot):
    d = AudioTimelineDocker()
    qtbot.addWidget(d)
    yield d
    d.poll_timer.stop()
    d._mixdown_spinner_timer.stop()


def _set_active_doc(docker_obj, doc):
    docker_obj.playback.active_document = lambda: doc


# ------------------------------------------------------------------ startup
def test_docker_starts_with_one_default_track(docker):
    assert len(docker.timeline.tracks) == 1


# --------------------------------------------------------------- add_track
def test_add_track_pushes_undoable_command(docker):
    before = len(docker.timeline.tracks)
    docker.add_track()
    assert len(docker.timeline.tracks) == before + 1
    docker.timeline.undo_stack.undo()
    assert len(docker.timeline.tracks) == before


# -------------------------------------------------------------- import_audio
def test_import_audio_no_path_selected_is_noop(docker):
    before = len(docker.timeline.tracks[0].clips)
    with patch.object(QFileDialog, "getOpenFileName", return_value=("", "")):
        docker.import_audio()
    assert len(docker.timeline.tracks[0].clips) == before


def test_import_audio_adds_clip_to_active_track(docker, wav_factory):
    path = wav_factory(duration_sec=0.5)
    with patch.object(QFileDialog, "getOpenFileName", return_value=(path, "")):
        docker.import_audio()
    track = docker.timeline.tracks[docker.timeline.active_track_index]
    assert len(track.clips) == 1
    assert track.clips[0].file_path == path
    # defer_analysis=True -- waveform request queued, not decoded synchronously.
    assert track.clips[0].peaks is None
    docker.timeline.wait_for_waveform_shutdown()


def test_import_audio_creates_first_track_if_none_exist(docker, wav_factory):
    docker.timeline.tracks = []  # simulate a doc with every track deleted
    path = wav_factory(duration_sec=0.3)
    with patch.object(QFileDialog, "getOpenFileName", return_value=(path, "")):
        docker.import_audio()
    assert len(docker.timeline.tracks) == 1
    assert len(docker.timeline.tracks[0].clips) == 1
    docker.timeline.wait_for_waveform_shutdown()


def test_import_audio_uses_active_document_fps(docker, wav_factory):
    doc = FakeAnnotatedDocument(fps=30)
    _set_active_doc(docker, doc)
    path = wav_factory(duration_sec=0.3)
    with patch.object(QFileDialog, "getOpenFileName", return_value=(path, "")):
        docker.import_audio()
    track = docker.timeline.tracks[docker.timeline.active_track_index]
    assert track.clips[-1].fps == 30
    docker.timeline.wait_for_waveform_shutdown()


def test_import_audio_shows_error_dialog_on_invalid_file(docker, tmp_path, monkeypatch):
    from ..audio import waveform_utils
    monkeypatch.setattr(waveform_utils, "_pydub_mediainfo", None)
    monkeypatch.setattr(waveform_utils, "HAVE_PYDUB", False)
    fake_mp3 = tmp_path / "clip.mp3"
    fake_mp3.write_bytes(b"not really audio")

    before = len(docker.timeline.tracks[0].clips)
    with patch.object(QFileDialog, "getOpenFileName", return_value=(str(fake_mp3), "")), \
         patch.object(QMessageBox, "critical") as mock_critical:
        docker.import_audio()
    mock_critical.assert_called_once()
    assert len(docker.timeline.tracks[0].clips) == before


def test_import_audio_nudges_clear_of_existing_clip(docker, make_clip):
    track = docker.timeline.tracks[0]
    existing = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(existing)
    docker.timeline.current_frame = 0

    path = existing.file_path
    with patch.object(QFileDialog, "getOpenFileName", return_value=(path, "")):
        docker.import_audio()
    new_clip = track.clips[-1]
    assert new_clip.start_frame >= existing.end_frame
    docker.timeline.wait_for_waveform_shutdown()


# ------------------------------------------------------------- export_mixdown
def test_export_mixdown_with_no_active_document_shows_message(docker):
    _set_active_doc(docker, None)
    with patch.object(QMessageBox, "information") as mock_info:
        docker.export_mixdown()
    mock_info.assert_called_once()


def test_export_mixdown_delegates_to_mixdown_controller(docker):
    doc = FakeAnnotatedDocument()
    _set_active_doc(docker, doc)
    docker.mixdown.export_mixdown = MagicMock()
    docker.export_mixdown()
    docker.mixdown.export_mixdown.assert_called_once_with(doc)


# --------------------------------------------------------------------- close
def test_close_stops_timers_and_cleans_up_mixdown_files(docker, tmp_path):
    leftover = tmp_path / "audiotimeline_mixdown_test.wav"
    leftover.write_bytes(b"fake wav data")
    docker.mixdown.known_mixdown_paths.add(str(leftover))

    docker.close()
    assert not docker.poll_timer.isActive()
    assert not docker._mixdown_spinner_timer.isActive()
    assert not leftover.exists()


# ------------------------------------------------------------- auto-updates
def test_maybe_auto_check_skips_if_already_done_this_session(docker, monkeypatch):
    monkeypatch.setattr(updater, "_auto_check_done_this_session", True)
    with patch.object(updater, "UpdateCheckWorker") as mock_worker_cls:
        docker._maybe_auto_check_for_updates()
    mock_worker_cls.assert_not_called()


def test_maybe_auto_check_skips_when_disabled_in_settings(docker, monkeypatch):
    monkeypatch.setattr(updater, "_auto_check_done_this_session", False)
    monkeypatch.setattr(updater, "load_update_settings", lambda: {"auto_check_updates": False})
    with patch.object(updater, "UpdateCheckWorker") as mock_worker_cls:
        docker._maybe_auto_check_for_updates()
    mock_worker_cls.assert_not_called()
    assert updater.auto_check_already_done_this_session() is True


def test_maybe_auto_check_shows_dialog_when_update_available(docker, monkeypatch):
    monkeypatch.setattr(updater, "_auto_check_done_this_session", False)
    monkeypatch.setattr(updater, "load_update_settings", lambda: {"auto_check_updates": True})
    info = {"tag": "v9.9.9", "version": "9.9.9", "zip_url": "https://example.invalid/x.zip"}

    monkeypatch.setattr(updater.UpdateCheckWorker, "start", lambda self: self.run())
    monkeypatch.setattr(updater, "fetch_latest_release_info", lambda: info)
    monkeypatch.setattr(updater, "is_update_available", lambda v: True)

    with patch("audiotimeline.audiotimeline_docker.UpdateDialog") as mock_dialog_cls:
        mock_dialog_cls.return_value.exec_ = MagicMock()
        docker._maybe_auto_check_for_updates()

    mock_dialog_cls.assert_called_once()
    _args, kwargs = mock_dialog_cls.call_args
    assert kwargs.get("automatic") is True or (len(_args) > 1 and _args[1] is True)
    mock_dialog_cls.return_value.exec_.assert_called_once()


def test_maybe_auto_check_silent_when_up_to_date(docker, monkeypatch):
    monkeypatch.setattr(updater, "_auto_check_done_this_session", False)
    monkeypatch.setattr(updater, "load_update_settings", lambda: {"auto_check_updates": True})

    monkeypatch.setattr(updater.UpdateCheckWorker, "start", lambda self: self.run())
    monkeypatch.setattr(updater, "fetch_latest_release_info", lambda: {"version": "0.0.1"})
    monkeypatch.setattr(updater, "is_update_available", lambda v: False)

    with patch("audiotimeline.audiotimeline_docker.UpdateDialog") as mock_dialog_cls:
        docker._maybe_auto_check_for_updates()
    mock_dialog_cls.assert_not_called()


def test_maybe_auto_check_silent_on_network_failure(docker, monkeypatch):
    monkeypatch.setattr(updater, "_auto_check_done_this_session", False)
    monkeypatch.setattr(updater, "load_update_settings", lambda: {"auto_check_updates": True})

    monkeypatch.setattr(updater.UpdateCheckWorker, "start", lambda self: self.run())
    monkeypatch.setattr(updater, "fetch_latest_release_info", MagicMock(side_effect=OSError("no network")))

    with patch("audiotimeline.audiotimeline_docker.UpdateDialog") as mock_dialog_cls, \
         patch.object(QMessageBox, "warning") as mock_warning:
        docker._maybe_auto_check_for_updates()
    mock_dialog_cls.assert_not_called()
    mock_warning.assert_not_called()  # automatic checks fail silently
