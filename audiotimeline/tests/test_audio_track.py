"""AudioClip/AudioTrack: durations, trim/split extent bookkeeping, and the
overlap-avoidance placement rules (find_insert_start / clamp_move_start)
that back both a fresh import landing on the playhead and a mid-drag
clip move -- the "no overlapping clip on the same track" constraint the
timeline enforces everywhere a clip's start_frame changes.
"""

import pytest

from ..audio.audio_track import AudioTrack


# --------------------------------------------------------------- durations
def test_clip_length_and_end_frame(make_clip):
    clip = make_clip(start_frame=10, fps=24, duration_sec=1.0)
    assert clip.length_frames == 24
    assert clip.end_frame == 34
    assert clip.contains_frame(10)
    assert clip.contains_frame(33)
    assert not clip.contains_frame(34)
    assert not clip.contains_frame(9)


def test_trim_shrinks_duration(make_clip):
    clip = make_clip(start_frame=0, fps=24, duration_sec=2.0)
    clip.trim_in_sec = 0.5
    clip.trim_out_sec = 0.25
    assert clip.duration_sec == pytest.approx(1.25)
    assert clip.length_frames == round(1.25 * 24)


# ------------------------------------------------------------- find_insert_start
def test_find_insert_start_no_overlap_uses_desired(track, make_clip):
    assert track.find_insert_start(100, 24) == 100


def test_find_insert_start_nudges_past_overlap(track, make_clip):
    existing = make_clip(start_frame=0, duration_sec=1.0, fps=24)  # occupies [0, 24)
    track.add_clip(existing)
    # Desired start 10 overlaps [0,24) -- closer to the gap-before (nothing
    # before it) than the gap-after, so it should land right after the
    # existing clip's end rather than at 10.
    start = track.find_insert_start(10, 24)
    assert start == existing.end_frame


def test_find_insert_start_fits_in_gap_between_clips(track, make_clip):
    a = make_clip(start_frame=0, duration_sec=1.0, fps=24)   # [0, 24)
    b = make_clip(start_frame=48, duration_sec=1.0, fps=24)  # [48, 72)
    track.add_clip(a)
    track.add_clip(b)
    # A 24-frame clip fits exactly in the [24, 48) gap.
    start = track.find_insert_start(30, 24)
    assert start == 24


def test_find_insert_start_cascades_past_too_small_gap(track, make_clip):
    a = make_clip(start_frame=0, duration_sec=1.0, fps=24)   # [0, 24)
    b = make_clip(start_frame=30, duration_sec=1.0, fps=24)  # [30, 54) -- gap of 6 frames, too small
    track.add_clip(a)
    track.add_clip(b)
    # A 24-frame clip can't fit in the tiny [24,30) gap, so it should
    # cascade past b's end.
    start = track.find_insert_start(25, 24)
    assert start == b.end_frame


# ------------------------------------------------------------- clamp_move_start
def test_clamp_move_start_excludes_self(track, make_clip):
    clip = make_clip(start_frame=0, duration_sec=1.0, fps=24)
    track.add_clip(clip)
    # Moving the clip "onto itself" (same desired start) must not treat its
    # own current position as an overlap.
    assert track.clamp_move_start(clip, 0) == 0


def test_clamp_move_start_avoids_other_clips(track, make_clip):
    a = make_clip(start_frame=0, duration_sec=1.0, fps=24)    # [0, 24)
    moving = make_clip(start_frame=100, duration_sec=1.0, fps=24)
    track.add_clip(a)
    track.add_clip(moving)
    clamped = track.clamp_move_start(moving, 10)
    assert clamped == a.end_frame
    # Result must not overlap `a`.
    assert not (clamped < a.end_frame and clamped + moving.length_frames > a.start_frame)


def test_clamp_move_start_never_negative(track, make_clip):
    moving = make_clip(start_frame=50, duration_sec=1.0, fps=24)
    track.add_clip(moving)
    assert track.clamp_move_start(moving, -20) == 0


# ------------------------------------------------------------------- clone/split
def test_clone_for_split_walls_off_left_and_right(make_clip):
    clip = make_clip(start_frame=0, fps=24, duration_sec=4.0)
    left = clip.clone_for_split(
        trim_in_sec=0.0, trim_out_sec=0.0, start_frame=0,
        source_duration_sec=2.0, trim_in_floor_sec=0.0,
    )
    right = clip.clone_for_split(
        trim_in_sec=2.0, trim_out_sec=0.0, start_frame=48,
        source_duration_sec=clip.source_duration_sec, trim_in_floor_sec=2.0,
    )
    # Left sibling can never be trimmed back out past the cut point.
    assert left.source_duration_sec == 2.0
    # Right sibling can never be trimmed back left past the cut point.
    assert right.trim_in_floor_sec == 2.0
    # Both share the same decoded peaks/sample_rate (no re-decode).
    assert left.peaks is clip.peaks
    assert right.peaks is clip.peaks
    assert left.id != clip.id != right.id


def test_clone_preserves_trim_and_volume(make_clip):
    clip = make_clip(start_frame=0, fps=24, duration_sec=4.0)
    clip.trim_in_sec = 0.5
    clip.trim_out_sec = 0.25
    clip.volume_points = [(0.0, 0.5), (0.5, 1.0), (1.0, 0.75)]
    dup = clip.clone(start_frame=100)
    assert dup.start_frame == 100
    assert dup.trim_in_sec == 0.5
    assert dup.trim_out_sec == 0.25
    assert dup.volume_points == clip.volume_points
    assert dup.id != clip.id


# --------------------------------------------------------- extent<->played fraction
def test_played_fraction_round_trips_extent_fraction(make_clip):
    clip = make_clip(start_frame=0, fps=24, duration_sec=4.0)
    clip.trim_in_sec = 1.0
    clip.trim_out_sec = 1.0
    for played in (0.0, 0.25, 0.5, 0.75, 1.0):
        extent = clip.played_fraction_to_extent_fraction(played)
        back = clip.extent_fraction_to_played_fraction(extent)
        assert back == pytest.approx(played, abs=1e-6)


def test_extent_fraction_outside_played_window_is_none(make_clip):
    clip = make_clip(start_frame=0, fps=24, duration_sec=4.0)
    clip.trim_in_sec = 2.0  # played window now starts halfway through the extent
    extent_frac_before_window = 0.0
    assert clip.extent_fraction_to_played_fraction(extent_frac_before_window) is None


def test_apply_analysis_backfills_pending_clip(wav_factory):
    from ..audio.audio_track import AudioClip
    path = wav_factory(duration_sec=0.5)
    clip = AudioClip(path, start_frame=0, fps=24, defer_analysis=True)
    assert clip.peaks is None
    assert clip.length_frames == round(0.5 * 24)

    class FakeInfo:
        peaks = [(-0.5, 0.5)] * 10
        sample_rate = 8000

    clip.apply_analysis(FakeInfo())
    assert clip.peaks == FakeInfo.peaks
    assert clip.sample_rate == 8000


def test_apply_analysis_noop_if_already_has_peaks(make_clip):
    clip = make_clip(duration_sec=0.5)
    original_peaks = clip.peaks

    class FakeInfo:
        peaks = [(-1.0, 1.0)]
        sample_rate = 1

    clip.apply_analysis(FakeInfo())
    assert clip.peaks is original_peaks
