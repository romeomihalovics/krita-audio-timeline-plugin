"""waveform_utils: turning a real (generated) .wav file into peak buckets
for the waveform preview, and the fast decode-free duration probe used so a
freshly-imported clip appears at its correct length immediately. All audio
here is generated on the fly (see helpers.make_wav) -- no third-party
fixture files or network access.
"""

import pytest

from ..audio import waveform_utils as wu


def test_probe_duration_matches_generated_wav(wav_factory):
    path = wav_factory(duration_sec=1.5, sample_rate=8000)
    duration_sec, sample_rate = wu.probe_duration(path)
    assert duration_sec == pytest.approx(1.5, abs=1e-3)
    assert sample_rate == 8000


def test_analyze_wav_returns_peaks_and_metadata(wav_factory):
    path = wav_factory(duration_sec=1.0, sample_rate=8000, freq_hz=440.0, amplitude=0.5)
    info = wu.analyze_wav(path, fps=24)
    assert info.sample_rate == 8000
    assert info.duration_sec == pytest.approx(1.0, abs=1e-3)
    assert len(info.peaks) > 0
    for lo, hi in info.peaks:
        assert -1.0 - 1e-9 <= lo <= hi <= 1.0 + 1e-9


def test_analyze_wav_normalizes_quiet_signal_to_full_scale(wav_factory):
    # A quiet (amplitude=0.1) sine should still normalize so its loudest
    # bucket reaches close to +-1 after _normalize_peaks.
    path = wav_factory(duration_sec=0.5, sample_rate=8000, amplitude=0.1)
    info = wu.analyze_wav(path, fps=24)
    max_abs = max(max(abs(lo), abs(hi)) for lo, hi in info.peaks)
    assert max_abs > 0.5


def test_analyze_wav_silent_file_returns_flat_zero_peaks(wav_factory):
    path = wav_factory(duration_sec=0.5, sample_rate=8000, amplitude=0.0)
    info = wu.analyze_wav(path, fps=24)
    for lo, hi in info.peaks:
        assert lo == 0.0 and hi == 0.0


def test_analyze_audio_file_dispatches_wav(wav_factory):
    path = wav_factory(duration_sec=0.3)
    info = wu.analyze_audio_file(path, fps=24)
    assert info.duration_sec > 0


def test_analyze_audio_file_non_wav_without_pydub_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(wu, "HAVE_PYDUB", False)
    fake_mp3 = tmp_path / "clip.mp3"
    fake_mp3.write_bytes(b"not really audio")
    with pytest.raises(RuntimeError):
        wu.analyze_audio_file(str(fake_mp3), fps=24)


def test_probe_duration_non_wav_without_ffprobe_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(wu, "_pydub_mediainfo", None)
    fake_mp3 = tmp_path / "clip.mp3"
    fake_mp3.write_bytes(b"not really audio")
    with pytest.raises(RuntimeError):
        wu.probe_duration(str(fake_mp3))


def test_buckets_for_duration_respects_min_and_max():
    assert wu._buckets_for_duration(0.0001, 24) == wu.MIN_BUCKETS
    assert wu._buckets_for_duration(1e9, 24) == wu.MAX_BUCKETS


def test_peaks_from_pcm_cancellation(wav_factory):
    import wave as wave_mod
    path = wav_factory(duration_sec=2.0, sample_rate=8000)
    with wave_mod.open(path, "rb") as wf:
        raw = wf.readframes(wf.getnframes())
        sample_width = wf.getsampwidth()
        channels = wf.getnchannels()

    calls = {"n": 0}

    def should_cancel():
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(wu.WaveformCancelled):
        wu._peaks_from_pcm(raw, sample_width, channels, num_buckets=100000, should_cancel=should_cancel)
