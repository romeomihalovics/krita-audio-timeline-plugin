"""render_mixdown: renders real (generated) AudioTrack/AudioClip layouts
down to an actual .wav file -- verifies muted tracks contribute silence,
gain envelopes actually scale the output, clips land at the right sample
offset, and mid-render cancellation is honored. All audio is generated
on the fly; nothing here needs Krita or third-party fixture files.
"""

import wave

import pytest

from ..audio import mixdown
from ..audio.audio_track import AudioTrack


def _read_wav_samples(path):
    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
    import struct
    values = struct.unpack("<" + "h" * (len(raw) // 2), raw)
    left = values[0::2]
    right = values[1::2]
    return left, right


def test_render_mixdown_produces_silence_with_no_clips(tmp_path):
    out = tmp_path / "out.wav"
    mixdown.render_mixdown([], fps=24, total_frames=24, out_path=str(out))
    left, right = _read_wav_samples(str(out))
    assert all(v == 0 for v in left)
    assert all(v == 0 for v in right)


def test_render_mixdown_muted_track_is_silent(tmp_path, make_clip):
    track = AudioTrack(name="T1")
    track.muted = True
    clip = make_clip(start_frame=0, fps=24, duration_sec=1.0, amplitude=0.8)
    track.add_clip(clip)
    out = tmp_path / "out.wav"
    mixdown.render_mixdown([track], fps=24, total_frames=24, out_path=str(out))
    left, right = _read_wav_samples(str(out))
    assert all(v == 0 for v in left)
    assert all(v == 0 for v in right)


def test_render_mixdown_unmuted_track_has_audio(tmp_path, make_clip):
    track = AudioTrack(name="T1")
    clip = make_clip(start_frame=0, fps=24, duration_sec=1.0, amplitude=0.8)
    track.add_clip(clip)
    out = tmp_path / "out.wav"
    mixdown.render_mixdown([track], fps=24, total_frames=24, out_path=str(out))
    left, right = _read_wav_samples(str(out))
    assert any(v != 0 for v in left)


def test_render_mixdown_respects_clip_start_frame_offset(tmp_path, make_clip):
    track = AudioTrack(name="T1")
    fps = 24
    # A short clip placed halfway through a 2-second timeline -- samples
    # before its start offset should stay silent.
    clip = make_clip(start_frame=fps, fps=fps, duration_sec=1.0, amplitude=0.9)
    track.add_clip(clip)
    out = tmp_path / "out.wav"
    total_frames = fps * 2
    mixdown.render_mixdown([track], fps=fps, total_frames=total_frames, out_path=str(out), sample_rate=8000)
    left, _ = _read_wav_samples(str(out))
    half = len(left) // 2
    # First half (before the clip starts) must be silent.
    assert all(v == 0 for v in left[:half - 100])
    # Somewhere in the second half there should be actual signal.
    assert any(v != 0 for v in left[half:])


def test_render_mixdown_zero_gain_volume_point_is_silent(tmp_path, make_clip):
    track = AudioTrack(name="T1")
    clip = make_clip(start_frame=0, fps=24, duration_sec=1.0, amplitude=0.9)
    clip.volume_points = [(0.0, 0.0), (1.0, 0.0)]
    track.add_clip(clip)
    out = tmp_path / "out.wav"
    mixdown.render_mixdown([track], fps=24, total_frames=24, out_path=str(out))
    left, right = _read_wav_samples(str(out))
    assert all(v == 0 for v in left)
    assert all(v == 0 for v in right)


def test_render_mixdown_half_gain_is_quieter_than_full_gain(tmp_path, wav_factory):
    from ..audio.audio_track import AudioClip

    def make(gain):
        path = wav_factory(duration_sec=1.0, amplitude=0.8)
        clip = AudioClip(path, start_frame=0, fps=24, defer_analysis=False)
        clip.volume_points = [(0.0, gain), (1.0, gain)]
        track = AudioTrack(name="T")
        track.add_clip(clip)
        return track

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        full_path = f"{d}/full.wav"
        half_path = f"{d}/half.wav"
        mixdown.render_mixdown([make(1.0)], fps=24, total_frames=24, out_path=full_path)
        mixdown.render_mixdown([make(0.5)], fps=24, total_frames=24, out_path=half_path)
        full_left, _ = _read_wav_samples(full_path)
        half_left, _ = _read_wav_samples(half_path)
        full_peak = max(abs(v) for v in full_left)
        half_peak = max(abs(v) for v in half_left)
        assert half_peak < full_peak


def test_render_mixdown_snapshot_tracks_is_thread_safe_copy(make_clip):
    track = AudioTrack(name="T1")
    clip = make_clip(start_frame=0, fps=24, duration_sec=1.0)
    track.add_clip(clip)
    snap = mixdown.snapshot_tracks([track])
    assert snap[0].muted is False
    assert snap[0].clips[0].file_path == clip.file_path
    # Mutating the live clip afterwards must not affect the snapshot.
    clip.start_frame = 999
    assert snap[0].clips[0].start_frame == 0


def test_render_mixdown_cancellation_raises(tmp_path, make_clip):
    track = AudioTrack(name="T1")
    clip = make_clip(start_frame=0, fps=24, duration_sec=2.0, amplitude=0.9)
    track.add_clip(clip)
    out = tmp_path / "out.wav"

    def should_cancel():
        return True

    with pytest.raises(mixdown.MixdownCancelled):
        mixdown.render_mixdown([track], fps=24, total_frames=48, out_path=str(out), should_cancel=should_cancel)


def test_render_mixdown_missing_file_is_skipped_not_fatal(tmp_path, make_clip):
    track = AudioTrack(name="T1")
    clip = make_clip(start_frame=0, fps=24, duration_sec=1.0)
    clip.file_path = str(tmp_path / "does_not_exist.wav")
    track.add_clip(clip)
    out = tmp_path / "out.wav"
    # Should not raise -- a bad clip is silently skipped.
    mixdown.render_mixdown([track], fps=24, total_frames=24, out_path=str(out))
    assert out.exists()
