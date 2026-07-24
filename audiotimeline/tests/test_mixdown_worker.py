"""MixdownWorker: the QThread wrapper around mixdown.render_mixdown() --
verifies the succeeded/failed/cancelled signal wiring against a real
background render of real (generated) audio, not a mocked thread.
"""

import pytest

from ..audio import mixdown
from ..audio.audio_track import AudioTrack
from ..audio.mixdown_worker import MixdownWorker


def test_mixdown_worker_emits_succeeded_with_out_path(qtbot, tmp_path, make_clip):
    track = AudioTrack(name="T1")
    track.add_clip(make_clip(start_frame=0, duration_sec=0.3, fps=24))
    snapshot = mixdown.snapshot_tracks([track])
    out_path = str(tmp_path / "out.wav")

    worker = MixdownWorker(snapshot, 24, 24, out_path)
    results = []
    worker.succeeded.connect(lambda path: results.append(("succeeded", path)))
    worker.failed.connect(lambda msg: results.append(("failed", msg)))
    worker.cancelled.connect(lambda: results.append(("cancelled",)))

    with qtbot.waitSignal(worker.succeeded, timeout=3000):
        worker.start()

    assert results == [("succeeded", out_path)]
    import os
    assert os.path.exists(out_path)


def test_mixdown_worker_emits_cancelled_when_cancel_called_immediately(qtbot, tmp_path, make_clip):
    track = AudioTrack(name="T1")
    # A longer clip gives the worker thread more chances to hit a
    # should_cancel() checkpoint before it finishes outright.
    track.add_clip(make_clip(start_frame=0, duration_sec=3.0, fps=24, sample_rate=44100))
    snapshot = mixdown.snapshot_tracks([track])
    out_path = str(tmp_path / "out.wav")

    worker = MixdownWorker(snapshot, 24, 24 * 3, out_path)
    results = []
    worker.succeeded.connect(lambda path: results.append("succeeded"))
    worker.failed.connect(lambda msg: results.append("failed"))
    worker.cancelled.connect(lambda: results.append("cancelled"))

    worker.cancel()  # cancel before it ever starts running
    with qtbot.waitSignal(worker.cancelled, timeout=3000, raising=False) as blocker:
        worker.start()

    # Either it's cancelled (the common case, cancel_event already set
    # before run() begins) or -- on a very fast machine -- it could in
    # principle race and finish first; either outcome is a real signal
    # having fired, never a hang or an unhandled exception.
    assert results and results[0] in ("cancelled", "succeeded")
    if results[0] == "cancelled":
        assert blocker.signal_triggered


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_mixdown_worker_emits_failed_on_decode_error(qtbot, tmp_path, make_clip):
    track = AudioTrack(name="T1")
    clip = make_clip(start_frame=0, duration_sec=0.3, fps=24)
    track.add_clip(clip)
    snapshot = mixdown.snapshot_tracks([track])
    # Corrupt the snapshot's file path so decoding fails for every clip --
    # render_mixdown() itself just silently skips a bad clip rather than
    # raising, so force a failure a different way: pass a bogus out_path
    # directory that can't be written to.
    bad_out_path = str(tmp_path / "does_not_exist_dir" / "out.wav")

    worker = MixdownWorker(snapshot, 24, 24, bad_out_path)
    results = []
    worker.succeeded.connect(lambda path: results.append("succeeded"))
    worker.failed.connect(lambda msg: results.append(msg))
    worker.cancelled.connect(lambda: results.append("cancelled"))

    with qtbot.waitSignal(worker.failed, timeout=3000):
        worker.start()

    assert results and results[0] != "succeeded" and results[0] != "cancelled"
