"""ClipboardMixin: internal Ctrl+C/Ctrl+V clip clipboard, pasting a file
path copied from outside Krita (or dropped in), and the background
waveform-decode queue those external imports kick off. Uses the real
(offscreen) QApplication clipboard and a real QThread for the waveform
decode -- no mocked Qt plumbing -- but every audio *file* involved is a
tiny generated wav (see helpers.make_wav), never a third-party fixture.
"""

from PyQt5.QtCore import QMimeData, QUrl
from PyQt5.QtGui import QDropEvent
from PyQt5.QtWidgets import QApplication

from ..audio.audio_track import AudioTrack


def test_copy_paste_internal_clip(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)
    timeline.selected_clip = clip

    QApplication.clipboard().clear()
    timeline.copy_selected_clip()
    assert timeline._clipboard_clip is clip

    timeline.current_frame = 200
    timeline.active_track_index = 0
    timeline.paste_clip()

    assert len(track.clips) == 2
    pasted = [c for c in track.clips if c is not clip][0]
    assert pasted.start_frame == 200
    assert pasted.file_path == clip.file_path
    assert pasted.id != clip.id


def test_paste_clones_current_trim_and_volume_state(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=2.0, fps=24)
    clip.trim_in_sec = 0.5
    clip.volume_points = [(0.0, 0.3), (1.0, 0.3)]
    track.add_clip(clip)
    timeline.selected_clip = clip

    timeline.copy_selected_clip()
    timeline.current_frame = 300
    timeline.paste_clip()

    pasted = [c for c in track.clips if c is not clip][0]
    assert pasted.trim_in_sec == 0.5
    assert pasted.volume_points == [(0.0, 0.3), (1.0, 0.3)]


def test_paste_at_empty_space_via_context_menu_location(timeline, make_clip):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)
    timeline.selected_clip = clip
    timeline.copy_selected_clip()

    timeline._paste_at(0, 500)
    pasted = [c for c in track.clips if c is not clip][0]
    assert pasted.start_frame == 500


def test_clipboard_has_content_reflects_internal_clip(timeline, make_clip):
    QApplication.clipboard().clear()
    assert timeline._clipboard_has_content() is False
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    clip = make_clip(start_frame=0)
    track.add_clip(clip)
    timeline.selected_clip = clip
    timeline.copy_selected_clip()
    assert timeline._clipboard_has_content() is True


def test_external_paste_imports_file_and_takes_priority_over_internal_clip(timeline, make_clip, wav_factory, qtbot):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    internal_clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(internal_clip)
    timeline.selected_clip = internal_clip
    timeline.copy_selected_clip()  # sets _clipboard_clip, but external paste should win

    external_path = wav_factory("external.wav", duration_sec=0.5)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(external_path)])
    QApplication.clipboard().setMimeData(mime)

    timeline.current_frame = 0
    timeline.active_track_index = 0
    timeline.paste_clip()

    imported = [c for c in track.clips if c.file_path == external_path]
    assert len(imported) == 1
    # Waveform starts pending (defer_analysis=True) and gets filled in by
    # the background decode.
    clip = imported[0]
    qtbot.waitUntil(lambda: clip.peaks is not None, timeout=3000)


def test_external_paste_creates_first_track_if_none_exists(timeline, wav_factory, qtbot):
    assert timeline.tracks == []
    path = wav_factory(duration_sec=0.3)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path)])
    QApplication.clipboard().setMimeData(mime)

    timeline.paste_clip()
    assert len(timeline.tracks) == 1
    assert len(timeline.tracks[0].clips) == 1


def test_external_paths_filters_unsupported_extensions(timeline, tmp_path):
    mime = QMimeData()
    txt = tmp_path / "notes.txt"
    txt.write_text("hi")
    mime.setUrls([QUrl.fromLocalFile(str(txt))])
    assert timeline._external_audio_paths(mime) == []


def test_drop_event_imports_audio_at_drop_position(timeline, wav_factory, qtbot):
    track = AudioTrack(name="T1")
    timeline.add_track(track)
    path = wav_factory(duration_sec=0.4)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path)])

    from PyQt5.QtCore import QPointF, Qt
    event = QDropEvent(
        QPointF(timeline.frame_to_x(50), 10), Qt.CopyAction, mime,
        Qt.LeftButton, Qt.NoModifier,
    )
    timeline.dropEvent(event)
    assert len(track.clips) == 1
    assert track.clips[0].file_path == path


def test_request_waveform_does_not_duplicate_queued_path(timeline, wav_factory):
    path = wav_factory(duration_sec=0.3)
    timeline.request_waveform(path)
    queued_before = list(timeline._waveform_queue)
    timeline.request_waveform(path)  # already inflight/queued -- must be a no-op
    assert timeline._waveform_queue == queued_before
