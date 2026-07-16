"""
Renders the virtual multi-track timeline down to a single interleaved
stereo WAV file, which the docker then hands to Krita via
`Document.setAudioTracks([path])`. Krita's own native audio engine (the
same one behind "Import Audio for Animation") does the actual playback,
scrubbing and sync from there -- this module's only job is turning
N tracks of clips into one flat file any time the layout changes.

Pure-`struct`/`array` implementation, matching waveform_utils.py's
reasoning: no `audioop` (removed in Python 3.13, which is what recent
Krita builds embed) and no hard dependency on `mlt`/`QtMultimedia`,
since neither was reliably importable from Krita's embedded interpreter
in the environment this was written for. `numpy` is used as an optional
fast path -- if it happens to be importable, mixing/resampling is
vectorized; if not, the same math runs as plain Python loops.
"""

import os
import struct
import threading
import wave
from array import array
from collections import namedtuple, OrderedDict

from . import volume_envelope

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

try:
    from pydub import AudioSegment
    HAVE_PYDUB = True
except ImportError:
    HAVE_PYDUB = False

TARGET_SAMPLE_RATE = 44100
TARGET_CHANNELS = 2

# Plain, immutable stand-ins for AudioTrack/AudioClip -- render_mixdown only
# ever reads .muted/.clips and a handful of AudioClip fields off whatever
# it's given, so a snapshot of just those fields is enough to run a render
# on a background thread without touching the live (mutable) timeline
# objects the UI thread keeps editing concurrently.
_ClipSnapshot = namedtuple(
    '_ClipSnapshot',
    'file_path start_frame source_duration_sec trim_in_sec trim_out_sec '
    'trim_in_floor_sec volume_points',
)
_TrackSnapshot = namedtuple('_TrackSnapshot', 'muted clips')


def snapshot_tracks(tracks):
    """Copies the fields render_mixdown() needs out of live track/clip
    objects into plain namedtuples, safe to hand to another thread."""
    return [
        _TrackSnapshot(
            muted=track.muted,
            clips=[
                _ClipSnapshot(
                    c.file_path, c.start_frame, c.source_duration_sec,
                    c.trim_in_sec, c.trim_out_sec, c.trim_in_floor_sec,
                    list(c.volume_points),
                )
                for c in track.clips
            ],
        )
        for track in tracks
    ]


def _decode_pcm(raw, sample_width, channels):
    """Raw interleaved PCM bytes -> list of per-channel float lists in [-1, 1]."""
    fmt = {1: 'b', 2: 'h', 4: 'i'}[sample_width]
    max_val = float(2 ** (8 * sample_width - 1))
    count = len(raw) // sample_width
    values = struct.unpack('<' + fmt * count, raw[:count * sample_width])
    channels = max(1, channels)
    chans = [[v / max_val for v in values[ch::channels]] for ch in range(channels)]
    return chans


def _decode_file(path):
    """Returns (channels_list_of_float_lists, sample_rate)."""
    if path.lower().endswith('.wav'):
        with wave.open(path, 'rb') as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        return _decode_pcm(raw, sample_width, channels), sample_rate

    if HAVE_PYDUB:
        seg = AudioSegment.from_file(path)
        return _decode_pcm(seg.raw_data, seg.sample_width, seg.channels), seg.frame_rate

    raise RuntimeError(
        f"'{path}' isn't a .wav and pydub is not installed, so it can't "
        "be decoded for mixdown. Install pydub + ffmpeg, or convert the "
        "file to .wav first."
    )


def _to_stereo(chans):
    if len(chans) == 1:
        return [chans[0], chans[0]]
    return chans[:2]


def _resample(samples, src_rate, dst_rate):
    if src_rate == dst_rate or not samples:
        return samples
    dst_len = max(1, int(round(len(samples) * dst_rate / float(src_rate))))

    if HAVE_NUMPY:
        src = np.asarray(samples, dtype=np.float64)
        src_idx = np.linspace(0, len(samples) - 1, dst_len)
        return np.interp(src_idx, np.arange(len(samples)), src)

    if dst_len == 1:
        return [samples[0]]
    ratio = (len(samples) - 1) / float(dst_len - 1)
    n = len(samples)
    out = [0.0] * dst_len
    for i in range(dst_len):
        pos = i * ratio
        i0 = int(pos)
        i1 = min(i0 + 1, n - 1)
        frac = pos - i0
        out[i] = samples[i0] * (1 - frac) + samples[i1] * frac
    return out


# Decoding + resampling a whole source file is the expensive,
# file-size-proportional part of preparing a clip for mixdown; trimming and
# gain (done after this) are cheap slice/scale ops. Since editing (trim/
# move/split/volume) never changes the source file itself, the
# decoded-and-resampled-to-target-rate channels are cached per
# (path, mtime, size, target_rate) so repeated mixdowns after an edit to one
# clip don't re-decode every *other* untouched clip's file again. Keyed on
# mtime/size (not just path) so external edits to the file are still picked
# up. Capped and evicted oldest-first so long sessions with many distinct
# source files don't grow this unboundedly. Only one mixdown render runs at
# a time today, but the lock keeps this safe regardless.
_PCM_CACHE_MAX_ENTRIES = 64
_pcm_cache = OrderedDict()
_pcm_cache_lock = threading.Lock()


def _decode_and_resample_cached(path, target_rate):
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size, target_rate)
    with _pcm_cache_lock:
        cached = _pcm_cache.get(key)
        if cached is not None:
            _pcm_cache.move_to_end(key)
            return cached

    chans, rate = _decode_file(path)
    chans = _to_stereo(chans)
    chans = [_resample(ch, rate, target_rate) for ch in chans]

    with _pcm_cache_lock:
        _pcm_cache[key] = chans
        _pcm_cache.move_to_end(key)
        while len(_pcm_cache) > _PCM_CACHE_MAX_ENTRIES:
            _pcm_cache.popitem(last=False)
    return chans


def _prepare_clip(path, target_rate, trim_in_sec, trim_out_sec, source_duration_sec):
    chans = _decode_and_resample_cached(path, target_rate)

    # Trim to the played region only, at the (now-resampled) mix sample
    # rate -- trim_in_sec/trim_out_sec are relative to the *source* file,
    # same clock as source_duration_sec.
    trim_start = max(0, int(round(trim_in_sec * target_rate)))
    trim_end_sec = max(trim_in_sec, source_duration_sec - trim_out_sec)
    trim_end = max(trim_start, int(round(trim_end_sec * target_rate)))
    return [ch[trim_start:trim_end] for ch in chans]


def _apply_gain_envelope(chans, volume_points, trim_in_sec, trim_out_sec,
                          source_duration_sec, trim_in_floor_sec):
    """Scales each channel's samples by the gain envelope defined by
    `volume_points` -- a list of (fraction, gain) points, `fraction` in
    [0, 1] over the clip's permanent volume-envelope extent
    (trim_in_floor_sec .. source_duration_sec, matching
    AudioClip.played_fraction_to_extent_fraction -- NOT the played/
    trimmed window `chans` itself covers), evaluated via the shared
    volume_envelope.evaluate() (Catmull-Rom through >2 points, linear for
    exactly 2) so the audible result matches the curve timeline_widget.py
    draws exactly.

    volume_envelope.evaluate() is only called at block boundaries, not per
    sample -- for a long clip that's a large cost difference, and linearly
    interpolating the per-sample gain between adjacent boundary gains is
    indistinguishable from evaluating every sample for a smoothly-varying
    envelope like this one."""
    if not volume_points:
        return chans
    points = sorted(volume_points, key=lambda p: p[0])
    if len(points) == 1:
        gain = points[0][1]
        if gain == 1.0:
            return chans
        if HAVE_NUMPY:
            return [ch * gain for ch in chans]
        return [array('d', (v * gain for v in ch)) for ch in chans]

    n = max((len(ch) for ch in chans), default=0)
    if n == 0:
        return chans

    played_duration = source_duration_sec - trim_in_sec - trim_out_sec
    extent_duration = source_duration_sec - trim_in_floor_sec

    def extent_fraction_at(played_fraction):
        if extent_duration <= 0:
            return 0.0
        abs_time = trim_in_sec + played_fraction * played_duration
        return (abs_time - trim_in_floor_sec) / extent_duration

    block = 512
    boundary_indices = list(range(0, n, block))
    if boundary_indices[-1] != n - 1:
        boundary_indices.append(n - 1)
    boundary_gains = [
        volume_envelope.evaluate(points, extent_fraction_at(i / float(n - 1) if n > 1 else 0.0))
        for i in boundary_indices
    ]

    if HAVE_NUMPY:
        idx = np.array(boundary_indices, dtype=np.float64)
        gains_b = np.array(boundary_gains, dtype=np.float64)
        gains = np.interp(np.arange(n, dtype=np.float64), idx, gains_b)
        return [np.asarray(ch) * gains[:len(ch)] for ch in chans]

    # Interpolates a full per-sample gain list once, in a single O(n) pass
    # (samples are visited in order, so the boundary segment only ever
    # advances forward) rather than re-scanning boundary_indices from the
    # start for every sample.
    if len(boundary_indices) == 1:
        gains = [boundary_gains[0]] * n
    else:
        gains = [0.0] * n
        k = 0
        last_k = len(boundary_indices) - 2
        for i in range(n):
            while k < last_k and i > boundary_indices[k + 1]:
                k += 1
            i0, i1 = boundary_indices[k], boundary_indices[k + 1]
            if i1 == i0:
                gains[i] = boundary_gains[k]
            else:
                t = (i - i0) / float(i1 - i0)
                gains[i] = boundary_gains[k] + (boundary_gains[k + 1] - boundary_gains[k]) * t

    out = []
    for ch in chans:
        scaled = array('d', (v * gains[i] for i, v in enumerate(ch)))
        out.append(scaled)
    return out


def _clamp16(x):
    if x > 32767:
        return 32767
    if x < -32768:
        return -32768
    return int(x)


def render_mixdown(tracks, fps, total_frames, out_path, sample_rate=TARGET_SAMPLE_RATE):
    """
    Mixes every unmuted clip on every track down to a single interleaved
    stereo 16-bit WAV at `out_path`, sized to cover `total_frames` at
    `fps`. Raises on decode failure of any individual clip is swallowed
    (that clip is just silently skipped) so one bad file doesn't sink
    the whole mixdown.
    """
    total_samples = max(1, int(round((total_frames / float(fps)) * sample_rate)))

    if HAVE_NUMPY:
        buffer = np.zeros((TARGET_CHANNELS, total_samples), dtype=np.float64)
    else:
        # A zero-filled byte string is bitwise 0.0 for IEEE754 doubles, so
        # this avoids materializing a total_samples-long Python list just
        # to zero it out.
        buffer = [array('d', bytes(8 * total_samples)) for _ in range(TARGET_CHANNELS)]

    for track in tracks:
        if track.muted:
            continue
        for clip in track.clips:
            try:
                chans = _prepare_clip(
                    clip.file_path, sample_rate,
                    clip.trim_in_sec, clip.trim_out_sec, clip.source_duration_sec,
                )
            except Exception:
                continue
            chans = _apply_gain_envelope(
                chans, clip.volume_points,
                clip.trim_in_sec, clip.trim_out_sec,
                clip.source_duration_sec, clip.trim_in_floor_sec,
            )

            start_sample = int(round((clip.start_frame / float(fps)) * sample_rate))
            if start_sample >= total_samples:
                continue

            for ch_idx in range(TARGET_CHANNELS):
                samples = chans[ch_idx]
                n = len(samples)
                end = min(total_samples, start_sample + n)
                length = end - start_sample
                if length <= 0:
                    continue
                if HAVE_NUMPY:
                    buffer[ch_idx][start_sample:end] += samples[:length]
                else:
                    dst = buffer[ch_idx]
                    for i in range(length):
                        dst[start_sample + i] += samples[i]

    if HAVE_NUMPY:
        peak = float(np.max(np.abs(buffer))) if buffer.size else 0.0
    else:
        peak = max((max(abs(min(ch)), abs(max(ch))) if len(ch) else 0.0) for ch in buffer)
    # Only scale down if the mix actually clips (peak > 1.0); never boost
    # quiet mixes, so a single clip's loudness doesn't change on export.
    scale = (1.0 / peak) if peak > 1.0 else 1.0

    if HAVE_NUMPY:
        ints = np.clip(np.round(buffer * scale * 32767), -32768, 32767).astype('<i2')
        interleaved = np.empty(total_samples * TARGET_CHANNELS, dtype='<i2')
        for ch_idx in range(TARGET_CHANNELS):
            interleaved[ch_idx::TARGET_CHANNELS] = ints[ch_idx]
        pcm_bytes = interleaved.tobytes()
    else:
        out = array('h', bytes(2 * total_samples * TARGET_CHANNELS))
        left, right = buffer
        for i in range(total_samples):
            out[i * 2] = _clamp16(round(left[i] * scale * 32767))
            out[i * 2 + 1] = _clamp16(round(right[i] * scale * 32767))
        pcm_bytes = out.tobytes()

    with wave.open(out_path, 'wb') as wf:
        wf.setnchannels(TARGET_CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
