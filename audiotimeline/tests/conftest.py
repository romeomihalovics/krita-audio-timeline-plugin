import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from ..audio.audio_track import AudioClip, AudioTrack
from ..ui.timeline_widget import AudioTimelineWidget
from .helpers import make_wav


@pytest.fixture
def wav_factory(tmp_path):
    """Returns a function(name, **kwargs) -> path that writes a small real
    sine-wave .wav under tmp_path -- see helpers.make_wav."""
    counter = {"n": 0}

    def _make(name=None, **kwargs):
        counter["n"] += 1
        filename = name or f"clip{counter['n']}.wav"
        path = tmp_path / filename
        return make_wav(path, **kwargs)

    return _make


@pytest.fixture
def make_clip(wav_factory):
    """Returns a function(start_frame, **kwargs) -> AudioClip backed by a
    freshly generated 1-second wav file, analyzed synchronously
    (defer_analysis=False) so tests see real peaks/duration immediately."""
    def _make(start_frame=0, fps=24, duration_sec=1.0, path=None, defer_analysis=False, **wav_kwargs):
        if path is None:
            path = wav_factory(duration_sec=duration_sec, **wav_kwargs)
        return AudioClip(path, start_frame, fps, defer_analysis=defer_analysis)
    return _make


@pytest.fixture
def track():
    return AudioTrack(name="Track 1")


@pytest.fixture
def timeline(qtbot):
    widget = AudioTimelineWidget()
    qtbot.addWidget(widget)
    widget.resize(2000, 400)
    return widget
