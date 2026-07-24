"""DocStateStore: saving the track/clip layout into a (fake) Krita document
annotation and restoring it later -- the ".kra metadata" round trip the
user relies on to persist their timeline across a save/reopen, plus the
in-session "switching between two already-visited documents keeps each
one's live undo stack" behavior.
"""

from unittest.mock import MagicMock

from PyQt5.QtCore import QObject
from PyQt5.QtWidgets import QUndoGroup

from ..ui.state_persistence import DocStateStore, ANNOTATION_KEY
from ..ui.timeline_widget import AudioTimelineWidget
from ..audio.audio_track import AudioTrack
from .helpers import FakeAnnotatedDocument, BrokenAnnotationDocument


class FakeDocker(QObject):
    def __init__(self, timeline):
        super().__init__()
        self.timeline = timeline
        self.undo_group = QUndoGroup()
        self.playback = MagicMock()
        self.mixdown = MagicMock()
        self.mixdown.mixdown_already_attached.return_value = True  # skip real render in tests


def make_docker(qtbot):
    timeline = AudioTimelineWidget()
    qtbot.addWidget(timeline)
    docker = FakeDocker(timeline)
    docker.add_track = lambda: docker.timeline.add_track(AudioTrack(name="Track 1"))
    store = DocStateStore(docker)
    return docker, store


def test_save_and_load_round_trips_tracks_and_clips(qtbot, make_clip):
    docker, store = make_docker(qtbot)
    doc = FakeAnnotatedDocument()
    docker.playback.active_document.return_value = doc

    track = AudioTrack(name="Vocals")
    docker.timeline.add_track(track)
    clip = make_clip(start_frame=10, fps=24, duration_sec=1.0)
    clip.trim_in_sec = 0.1
    clip.volume_points = [(0.0, 0.5), (0.5, 1.0), (1.0, 0.5)]
    track.add_clip(clip)

    store.save_state()
    assert doc.annotation(ANNOTATION_KEY)

    # Simulate reopening: a brand new DocStateStore/timeline, same doc_id.
    docker2, store2 = make_docker(qtbot)
    docker2.playback.active_document.return_value = doc
    store2.load_state_fresh(doc)
    docker2.timeline.wait_for_waveform_shutdown()

    assert len(docker2.timeline.tracks) == 1
    loaded_track = docker2.timeline.tracks[0]
    assert loaded_track.name == "Vocals"
    assert len(loaded_track.clips) == 1
    loaded_clip = loaded_track.clips[0]
    assert loaded_clip.start_frame == 10
    assert loaded_clip.trim_in_sec == 0.1
    assert loaded_clip.volume_points == [(0.0, 0.5), (0.5, 1.0), (1.0, 0.5)]
    # defer_analysis=True on load -- waveform fills in later, not synchronously.
    assert loaded_clip.peaks is None


def test_load_state_fresh_skips_missing_files(qtbot, tmp_path):
    docker, store = make_docker(qtbot)
    doc = FakeAnnotatedDocument()
    import json
    data = {
        "tracks": [{
            "name": "T1", "muted": False,
            "clips": [{
                "file_path": str(tmp_path / "does_not_exist.wav"),
                "start_frame": 0, "fps": 24, "source_duration_sec": 1.0,
                "trim_in_sec": 0.0, "trim_out_sec": 0.0, "trim_in_floor_sec": 0.0,
                "volume_points": [[0.0, 1.0], [1.0, 1.0]],
            }],
        }]
    }
    doc.setAnnotation("audiotimeline/state", "", json.dumps(data).encode("utf-8"))

    store.load_state_fresh(doc)
    assert len(docker.timeline.tracks) == 1
    assert docker.timeline.tracks[0].clips == []  # missing file silently skipped


def test_load_state_fresh_with_no_tracks_adds_default_track(qtbot):
    docker, store = make_docker(qtbot)
    doc = FakeAnnotatedDocument()

    store.load_state_fresh(doc)
    assert len(docker.timeline.tracks) == 1


def test_load_state_fresh_clears_undo_stack_of_fallback_track(qtbot):
    docker, store = make_docker(qtbot)
    doc = FakeAnnotatedDocument()

    store.load_state_fresh(doc)
    stack = docker.timeline.undo_stack
    assert stack.count() == 0  # fallback empty track isn't itself undoable


def test_split_clip_ceiling_restored_from_saved_source_duration(qtbot, make_clip):
    docker, store = make_docker(qtbot)
    doc = FakeAnnotatedDocument()
    docker.playback.active_document.return_value = doc

    track = AudioTrack(name="T1")
    docker.timeline.add_track(track)
    clip = make_clip(start_frame=0, fps=24, duration_sec=4.0)
    # Simulate a post-split clip: its source_duration_sec was capped below
    # the real file's full duration.
    clip.source_duration_sec = 2.0
    track.add_clip(clip)

    store.save_state()

    docker2, store2 = make_docker(qtbot)
    store2.load_state_fresh(doc)
    docker2.timeline.wait_for_waveform_shutdown()
    loaded_clip = docker2.timeline.tracks[0].clips[0]
    # Must restore the capped ceiling, not the freshly re-probed full
    # file duration -- otherwise the split clip could be trimmed back out
    # past its cut point after a reload.
    assert loaded_clip.source_duration_sec == 2.0
    assert loaded_clip.full_source_duration_sec == 4.0  # re-probed from the real file


def test_switching_between_visited_documents_preserves_live_undo_stack(qtbot, make_clip):
    docker, store = make_docker(qtbot)
    doc_a = FakeAnnotatedDocument()
    doc_b = FakeAnnotatedDocument()
    docker.playback.active_document.return_value = doc_a

    # First visit to doc_a: fresh load, then make an edit. (load_state()
    # itself never updates _loaded_doc_id -- see playback_sync.py's own
    # canvas_changed, which always calls note_loaded() right after.)
    store.load_state(doc_a)
    store.note_loaded(store.doc_id(doc_a))
    track = AudioTrack(name="A-Track")
    docker.timeline.add_track(track)
    from .. import commands
    clip = make_clip(start_frame=0)
    docker.timeline.undo_stack.push(commands.AddClipCommand(docker.timeline, track, clip))
    assert docker.timeline.undo_stack.count() == 1

    store.sync_active_doc_state()

    # Switch to doc_b (first visit -- fresh/empty).
    docker.playback.active_document.return_value = doc_b
    store.load_state(doc_b)
    store.note_loaded(store.doc_id(doc_b))
    # doc_b has no saved annotation -- load_state_fresh's own empty-doc
    # fallback (docker.add_track()) gives it a single empty track, not the
    # doc_a track this docker just had.
    assert track not in docker.timeline.tracks

    # Switch back to doc_a -- must restore the SAME live tracks/undo stack,
    # not re-parse from scratch (which would discard the undo history).
    docker.playback.active_document.return_value = doc_a
    store.load_state(doc_a)
    # doc_a itself had no saved annotation either, so its own first
    # load_state_fresh added its own empty fallback track before `track`
    # was added on top -- the live list (not a fresh reparse) must still
    # contain both, in the same objects/order as before the switch away.
    assert track in docker.timeline.tracks
    assert docker.timeline.undo_stack.count() == 1


def test_doc_id_is_stable_across_calls(qtbot):
    docker, store = make_docker(qtbot)
    doc = FakeAnnotatedDocument()
    first = store.doc_id(doc)
    second = store.doc_id(doc)
    assert first == second
    assert first  # non-empty


# ------------------------------------------------------ annotation-failure paths
def test_doc_id_best_effort_when_annotations_unsupported(qtbot):
    """doc_id() must still return *something* usable (rather than raise)
    even if the underlying Document can't store annotations at all -- e.g.
    an older Krita build, or any other doc that doesn't conform. Every
    call generates a fresh id in that case (nothing durable to read back),
    but callers keying purely on identity within one process run still get
    a stable, non-empty string per call."""
    docker, store = make_docker(qtbot)
    doc = BrokenAnnotationDocument()
    doc_id = store.doc_id(doc)
    assert doc_id


def test_save_state_does_not_raise_when_annotations_unsupported(qtbot, make_clip):
    docker, store = make_docker(qtbot)
    doc = BrokenAnnotationDocument()
    docker.playback.active_document.return_value = doc
    track = AudioTrack(name="T1")
    docker.timeline.add_track(track)
    track.add_clip(make_clip(start_frame=0))

    store.save_state()  # must not raise -- setAnnotation's failure is swallowed


def test_load_state_fresh_treats_unreadable_annotation_as_empty_document(qtbot):
    docker, store = make_docker(qtbot)
    doc = BrokenAnnotationDocument()

    store.load_state_fresh(doc)  # doc.annotation() raises -- must fall back to an empty doc
    assert len(docker.timeline.tracks) == 1  # the usual empty-doc fallback track
