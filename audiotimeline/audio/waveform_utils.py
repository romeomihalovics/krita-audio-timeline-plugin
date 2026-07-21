"""
Utilities to turn an audio file into a small array of min/max "peaks"
that can be drawn cheaply regardless of zoom level, and to fetch basic
audio metadata (duration, sample rate).

WAV is handled with the stdlib `wave` module (always available).
Anything else (mp3, ogg, flac...) is handled via `pydub`, which is an
optional dependency -- if it isn't installed we degrade gracefully by
telling the user rather than crashing the docker.

NOTE: this deliberately does NOT use the stdlib `audioop` module.
`audioop` was deprecated in Python 3.11 and removed entirely in 3.13,
and Krita's nightly/5.3/6.0 builds now embed Python 3.13 -- importing
it there raises ModuleNotFoundError, which (since this module sits in
the plugin's import chain) breaks the *entire* plugin load. Downmixing
is done by hand with `struct` instead, which is 3.13-safe.
"""

import struct
import wave

try:
    from pydub import AudioSegment  # optional
    HAVE_PYDUB = True
except ImportError:
    HAVE_PYDUB = False

try:
    from pydub.utils import mediainfo as _pydub_mediainfo  # optional
except ImportError:
    _pydub_mediainfo = None


class AudioInfo:
    def __init__(self, duration_sec, sample_rate, peaks):
        self.duration_sec = duration_sec
        self.sample_rate = sample_rate
        # peaks: list of (min, max) floats in range [-1, 1], one pair per bucket
        self.peaks = peaks


class WaveformCancelled(Exception):
    """Raised internally by _peaks_from_pcm() when `should_cancel()`
    reports true at a checkpoint -- caught by WaveformWorker.run() and
    turned into its `cancelled` signal. Never meant to escape to a caller
    that isn't cooperating in cancellation."""


# How many buckets to decode between should_cancel() checks -- checking
# every single bucket would add a Python-level call per iteration of an
# already-hot loop; checking too rarely would make cancellation slow to
# take effect on a very long file. A few hundred buckets is cheap either
# way relative to the unpack/min/max work already done per bucket.
_CANCEL_CHECK_INTERVAL = 256


def _peaks_from_pcm(samples, sample_width, channels, num_buckets, should_cancel=None):
    """
    samples: raw interleaved PCM bytes. Returns list of (min, max)
    normalized to [-1, 1], one pair per bucket.

    Multi-channel audio is NOT downmixed to true mono here -- instead,
    each bucket's min/max is taken across all interleaved channel
    samples in that time slice together. For a peak-outline waveform
    display (not actual audio processing) this looks the same as a
    proper downmix, and it avoids any dependency on `audioop`.

    `should_cancel`, if given, is a zero-arg callable checked periodically
    (see _CANCEL_CHECK_INTERVAL) -- if it returns true, raises
    WaveformCancelled so a decode superseded before it finishes (e.g. the
    clip that requested it was deleted/undone) can bail out early instead
    of finishing a result nobody needs anymore.
    """
    fmt = {1: 'b', 2: 'h', 4: 'i'}[sample_width]
    max_val = float(2 ** (8 * sample_width - 1))
    bytes_per_frame = sample_width * max(1, channels)
    frame_count = len(samples) // bytes_per_frame
    if frame_count == 0:
        return []

    bucket_frames = max(1, frame_count // num_buckets)
    peaks = []
    bucket_idx = 0
    for i in range(0, frame_count, bucket_frames):
        if should_cancel is not None and bucket_idx % _CANCEL_CHECK_INTERVAL == 0 and should_cancel():
            raise WaveformCancelled()
        bucket_idx += 1
        start = i * bytes_per_frame
        end = min(len(samples), (i + bucket_frames) * bytes_per_frame)
        chunk = samples[start:end]
        count = len(chunk) // sample_width
        if count == 0:
            continue
        values = struct.unpack('<' + fmt * count, chunk[:count * sample_width])
        lo = min(values) / max_val
        hi = max(values) / max_val
        peaks.append((lo, hi))
    return peaks[:num_buckets] if len(peaks) > num_buckets else peaks


def _normalize_peaks(peaks, curve=0.8):
    """
    Rescale peaks so the loudest moment in the clip reaches +-1, then apply
    a sign-preserving power curve (exponent < 1) on top of that gain.

    A plain gain-to-max normalization alone still leaves quiet passages
    looking flat next to loud ones, since it's a linear rescale of the same
    shape. The power curve compresses the dynamic range perceptually (like
    a waveform "loudness" view) so quieter sections become visibly more
    detailed while staying monotonically smaller than louder ones -- lows
    and highs are still distinguishable, just not to the point where quiet
    audio renders as a near-flat line.
    """
    if not peaks:
        return peaks
    max_abs = max(max(abs(lo), abs(hi)) for lo, hi in peaks)
    if max_abs < 1e-9:
        return peaks
    gain = 1.0 / max_abs

    def shape(v):
        scaled = abs(v) * gain
        shaped = scaled ** curve
        return -shaped if v < 0 else shaped

    return [(shape(lo), shape(hi)) for lo, hi in peaks]


MIN_BUCKETS = 20
# Only a backstop against pathologically long audio (hours-long ambient
# tracks, say) -- render cost no longer scales with a clip's total bucket
# count regardless of this cap (AudioTimelineWidget._paint_waveform only
# ever walks the currently-visible buckets, aggregating them when zoomed
# out), so this just bounds the one-time import-analysis time and the
# peaks array's memory footprint, both of which scale linearly with
# duration. Sized well above any realistic animation-length clip so
# BUCKETS_PER_FRAME's density isn't cut short by clip length -- a 3-minute
# clip at 24fps wants 180*24*40 = 172,800 buckets and should get all of
# them, not a coarsened-down fraction.
MAX_BUCKETS = 2_000_000
# Buckets per timeline frame -- a fixed bucket *count* (regardless of
# duration) instead made short clips look dense (many buckets squeezed into
# few pixels) and long clips look sparse ("airy": few buckets stretched
# across many pixels).
#
# This is deliberately > 1, and specifically matches px_per_frame's max
# zoom (see its clamp in AudioTimelineWidget.wheelEvent()): the widget's
# _paint_waveform() already dynamically adjusts *display* density for the
# current zoom (drawing one line per bucket when zoomed in enough, or
# aggregating several into one when zoomed out -- see its step_px
# branching), but it can't draw detail this storage layer never captured.
# Sizing this to the max zoom means there's still a genuinely distinct
# bucket for close to every pixel even at full zoom, rather than running
# out of stored detail partway there.
BUCKETS_PER_FRAME = 40


def _buckets_for_duration(duration_sec, fps):
    frames = duration_sec * fps
    return int(min(MAX_BUCKETS, max(MIN_BUCKETS, round(frames * BUCKETS_PER_FRAME))))


def analyze_wav(path, fps, num_buckets=None, should_cancel=None):
    with wave.open(path, 'rb') as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    duration_sec = n_frames / float(sample_rate) if sample_rate else 0.0
    if num_buckets is None:
        num_buckets = _buckets_for_duration(duration_sec, fps)
    peaks = _normalize_peaks(_peaks_from_pcm(raw, sample_width, channels, num_buckets, should_cancel))
    return AudioInfo(duration_sec, sample_rate, peaks)


def analyze_with_pydub(path, fps, num_buckets=None, should_cancel=None):
    seg = AudioSegment.from_file(path)
    sample_width = seg.sample_width
    channels = seg.channels
    sample_rate = seg.frame_rate
    raw = seg.raw_data
    duration_sec = len(seg) / 1000.0
    if num_buckets is None:
        num_buckets = _buckets_for_duration(duration_sec, fps)
    peaks = _normalize_peaks(_peaks_from_pcm(raw, sample_width, channels, num_buckets, should_cancel))
    return AudioInfo(duration_sec, sample_rate, peaks)


def probe_duration(path):
    """
    Cheap (duration_sec, sample_rate) lookup that avoids decoding sample
    data, so a newly-imported clip can be placed on the timeline at its
    correct full length immediately, before the (potentially slow) full
    peak analysis has run in the background.

    For .wav this only reads the header via `wave` -- getnframes()/
    getframerate() don't touch the sample data -- so it's effectively O(1)
    regardless of file size. For everything else it shells out to a single
    `ffprobe` call via pydub's `mediainfo()` (pydub is already a required
    dependency for non-wav files, so this adds no new dependency), which
    reads container metadata instead of decoding audio.

    Raises RuntimeError (same message shape as analyze_audio_file) if
    duration can't be determined cheaply, so callers can fall back to a
    full synchronous analyze_audio_file() call.
    """
    lower = path.lower()
    if lower.endswith('.wav'):
        with wave.open(path, 'rb') as wf:
            sample_rate = wf.getframerate()
            n_frames = wf.getnframes()
        duration_sec = n_frames / float(sample_rate) if sample_rate else 0.0
        return duration_sec, sample_rate

    if _pydub_mediainfo is not None:
        info = _pydub_mediainfo(path)
        try:
            duration_sec = float(info['duration'])
            sample_rate = int(float(info.get('sample_rate') or 0))
        except (KeyError, TypeError, ValueError):
            duration_sec = 0.0
            sample_rate = 0
        if duration_sec > 0:
            return duration_sec, (sample_rate or 44100)

    raise RuntimeError(
        "This file isn't a .wav and pydub/ffprobe couldn't read its "
        "duration, so it can't be quickly probed. Install pydub + ffmpeg "
        "in Krita's Python environment, or convert the file to .wav first."
    )


def analyze_audio_file(path, fps, num_buckets=None, should_cancel=None):
    """
    Returns an AudioInfo, or raises RuntimeError with a friendly message
    if the format needs pydub/ffmpeg and neither is available.

    `fps` is the timeline's frame rate, used to size the peak bucket count
    to the clip's duration (see `_buckets_for_duration`) so waveform
    density on screen stays consistent regardless of clip length. Pass
    `num_buckets` explicitly to override that.

    `should_cancel`, if given, is forwarded to _peaks_from_pcm() -- see
    its docstring and WaveformCancelled.
    """
    lower = path.lower()
    if lower.endswith('.wav'):
        return analyze_wav(path, fps, num_buckets, should_cancel)

    if HAVE_PYDUB:
        return analyze_with_pydub(path, fps, num_buckets, should_cancel)

    raise RuntimeError(
        "This file isn't a .wav and pydub is not installed, so its "
        "waveform can't be read. Install pydub + ffmpeg in Krita's "
        "Python environment, or convert the file to .wav first."
    )
