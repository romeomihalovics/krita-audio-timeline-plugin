"""Shared test helpers: real (but tiny) generated .wav fixtures and a fake
Krita Document stand-in, so tests never need an actual Krita install or
real third-party network/media files. Only imported by files under
audiotimeline/tests/ -- see test_no_runtime_test_imports.py, which enforces
that nothing under the runtime package ever imports from here.
"""

import math
import struct
import wave


def make_wav(path, duration_sec=1.0, sample_rate=8000, freq_hz=440.0,
             channels=1, sample_width=2, amplitude=0.5):
    """Writes a small mono/stereo sine-wave .wav file to `path` -- real,
    decodable PCM data (not silence) so peak analysis has something
    non-trivial to find, without any external audio fixture files or
    third-party codecs (plain stdlib `wave` + `struct`, same as
    waveform_utils.py itself)."""
    n_frames = max(1, int(round(duration_sec * sample_rate)))
    max_val = float(2 ** (8 * sample_width - 1) - 1)
    fmt = {1: 'b', 2: 'h', 4: 'i'}[sample_width]
    frames = bytearray()
    for i in range(n_frames):
        t = i / float(sample_rate)
        value = amplitude * math.sin(2 * math.pi * freq_hz * t)
        sample = int(round(value * max_val))
        for _ in range(channels):
            frames += struct.pack('<' + fmt, sample)
    with wave.open(str(path), 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return str(path)


def make_silent_wav(path, duration_sec=1.0, sample_rate=8000, channels=1, sample_width=2):
    return make_wav(path, duration_sec, sample_rate, freq_hz=0.0, channels=channels,
                     sample_width=sample_width, amplitude=0.0)


class FakeAnnotatedDocument:
    """Stands in for a Krita `Document` -- byte-blob annotation storage
    keyed by string (state_persistence.py), the setAudioTracks()/
    audioTracks() surface (mixdown_controller.py), and the frame/fps/
    current-time surface (playback_sync.py). Nothing here talks to any
    real Krita/network/file-format API."""

    def __init__(self, fps=24, frame_count=240, file_name=""):
        self._annotations = {}
        self._fps = fps
        self._frame_count = frame_count
        self._current_time = 0
        self._audio_tracks = []
        self._audio_level = 0.0
        self._file_name = file_name

    def annotation(self, key):
        return self._annotations.get(key, b"")

    def setAnnotation(self, key, _description, payload):
        self._annotations[key] = bytes(payload)

    def framesPerSecond(self):
        return self._fps

    def fullClipRangeEndTime(self):
        return self._frame_count

    def currentTime(self):
        return self._current_time

    def setCurrentTime(self, frame):
        self._current_time = frame

    def setAudioTracks(self, paths):
        self._audio_tracks = list(paths)

    def audioTracks(self):
        return list(self._audio_tracks)

    def audioLevel(self):
        return self._audio_level

    def setAudioLevel(self, level):
        self._audio_level = level

    def fileName(self):
        return self._file_name


class BrokenAnnotationDocument(FakeAnnotatedDocument):
    """A document whose annotation()/setAnnotation() always raise -- stands
    in for an older/non-conforming Krita build so state_persistence.py's
    best-effort try/except paths around those calls get exercised."""

    def annotation(self, key):
        raise RuntimeError("annotations not supported on this build")

    def setAnnotation(self, key, description, payload):
        raise RuntimeError("annotations not supported on this build")


class NoAudioApiDocument:
    """A document without setAudioTracks()/audioTracks() at all -- stands
    in for Krita 5.2.x and earlier, which predates that API entirely (see
    MixdownController.audio_api_available). Deliberately NOT a
    FakeAnnotatedDocument subclass, since inheriting those methods would
    defeat the whole point of this stand-in."""

    def __init__(self, fps=24, frame_count=240, file_name=""):
        self._annotations = {}
        self._fps = fps
        self._frame_count = frame_count
        self._file_name = file_name

    def annotation(self, key):
        return self._annotations.get(key, b"")

    def setAnnotation(self, key, _description, payload):
        self._annotations[key] = bytes(payload)

    def framesPerSecond(self):
        return self._fps

    def fullClipRangeEndTime(self):
        return self._frame_count

    def fileName(self):
        return self._file_name
