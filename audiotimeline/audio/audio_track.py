import os
import uuid

from .waveform_utils import analyze_audio_file, probe_duration


class AudioClip:
    """A single audio file placed on a track at a given start frame."""

    def __init__(self, file_path, start_frame, fps, defer_analysis=False):
        self.id = str(uuid.uuid4())
        self.file_path = file_path
        self.name = os.path.basename(file_path)
        self.start_frame = start_frame
        self.fps = fps

        if defer_analysis:
            # Fast, decode-free duration probe only -- lets the clip appear
            # at its correct full length and be moved/trimmed/split right
            # away, without blocking on the (potentially slow) full peak
            # decode. `peaks` stays None -- a "pending" waveform -- until a
            # background analyze_audio_file() call finishes and something
            # calls apply_analysis() with the result. Callers doing this
            # must eventually call apply_analysis(); see
            # waveform_worker.WaveformWorker for the background half.
            duration_sec, sample_rate = probe_duration(file_path)
            self.source_duration_sec = duration_sec
            self.full_source_duration_sec = duration_sec
            self.sample_rate = sample_rate
            self.peaks = None
        else:
            info = analyze_audio_file(file_path, fps)
            # The full, untrimmed duration of the source file -- the ceiling
            # trim_in_sec/trim_out_sec can never eat past. `peaks` is analyzed
            # once here over the whole file and never re-analyzed on trim.
            self.source_duration_sec = info.duration_sec
            # The true duration of the decoded source file, spanned by `peaks`.
            # Unlike source_duration_sec, this is never overridden after a split
            # -- it's what _trimmed_peaks must divide by to index into `peaks`,
            # regardless of any trim-ceiling capping applied to source_duration_sec.
            self.full_source_duration_sec = info.duration_sec
            self.sample_rate = info.sample_rate
            self.peaks = info.peaks  # list of (min, max) in [-1, 1]

        # Seconds clipped off the start/end of the source by edge-dragging.
        self.trim_in_sec = 0.0
        self.trim_out_sec = 0.0
        # Floor that trim_in_sec can never be dragged below. Always 0.0
        # except for the right-hand sibling of a split, which must never be
        # able to trim back past the cut point and reveal audio that now
        # belongs to its sibling (source_duration_sec caps the equivalent
        # ceiling on the left-hand sibling instead -- see clone_for_split).
        self.trim_in_floor_sec = 0.0
        # (fraction, gain) pairs, fraction in [0, 1] relative to the
        # clip's permanent volume-envelope extent (trim_in_floor_sec ..
        # source_duration_sec -- see played_fraction_to_extent_fraction()),
        # not its current played/trimmed window, so ordinary trimming never
        # needs to rewrite them. Flat, unity gain by default.
        self.volume_points = [(0.0, 1.0), (1.0, 1.0)]

    def apply_analysis(self, info):
        """Backfills `peaks`/`sample_rate` once a background waveform decode
        (started for a `defer_analysis=True` clip) finishes. Deliberately
        does NOT touch source_duration_sec/full_source_duration_sec/
        trim_*_sec -- those were already fixed by the fast probe at import
        time and may have been further edited (trimmed, split) by the user
        in the meantime; only the redraw-only peaks/sample_rate are pending
        data. No-op if this clip already has peaks (e.g. it wasn't the
        clip the analysis was originally for, just a same-file sibling
        being backfilled that already got them some other way)."""
        if self.peaks is not None:
            return
        self.peaks = info.peaks
        self.sample_rate = info.sample_rate

    @property
    def duration_sec(self):
        return self.source_duration_sec - self.trim_in_sec - self.trim_out_sec

    @property
    def length_frames(self):
        return max(1, int(round(self.duration_sec * self.fps)))

    @property
    def end_frame(self):
        return self.start_frame + self.length_frames

    def contains_frame(self, frame):
        return self.start_frame <= frame < self.end_frame

    def seconds_into_clip(self, frame):
        return max(0.0, (frame - self.start_frame) / float(self.fps))

    def played_fraction_to_extent_fraction(self, played_fraction):
        """Maps a fraction of the clip's currently *played* (trimmed)
        window to a fraction of its permanent volume-envelope extent --
        trim_in_floor_sec .. source_duration_sec, the widest range this
        clip object can ever be trimmed back out to (only trim_in_sec/
        trim_out_sec move on an ordinary trim-edge drag; trim_in_floor_sec/
        source_duration_sec only change at a split, which permanently
        walls each sibling off to its own share of the source). Storing
        volume_points' fractions against this fixed extent instead of the
        (shrinking-and-growing) played window means an ordinary trim never
        needs to rewrite them -- a bend point trimmed out of view is still
        there, unmodified, when the clip is trimmed back out to reveal it
        again. See extent_fraction_to_played_fraction() for the inverse."""
        extent_duration = self.source_duration_sec - self.trim_in_floor_sec
        if extent_duration <= 0:
            return 0.0
        abs_time = self.trim_in_sec + played_fraction * self.duration_sec
        return (abs_time - self.trim_in_floor_sec) / extent_duration

    def extent_fraction_to_played_fraction(self, extent_fraction):
        """Inverse of played_fraction_to_extent_fraction() -- returns None
        if `extent_fraction` currently falls outside the played (trimmed)
        window, meaning that bend point exists (unaffected) but isn't
        visible/interactable until the clip is trimmed back out to reveal
        it.

        The out-of-range check tolerates up to about half a frame's worth
        of drift, not just float noise -- trim_in_sec/trim_out_sec are
        re-derived from a *frame-quantized* start/end during a trim drag
        (see mouseMoveEvent's trim_left/trim_right), so dragging a clip
        back out to what looks like its exact original length can still
        leave a sub-frame residual (a fraction of a millisecond) on
        trim_in_sec/trim_out_sec rather than landing on precisely 0.0.
        Without this tolerance, a point sitting exactly at the extent's
        0.0/1.0 edge -- which is exactly where that residual bites -- would
        flicker in and out of visibility depending on which side of true
        zero the rounding landed on, even though the clip looks fully
        expanded."""
        if self.duration_sec <= 0:
            return None
        extent_duration = self.source_duration_sec - self.trim_in_floor_sec
        abs_time = self.trim_in_floor_sec + extent_fraction * extent_duration
        played_fraction = (abs_time - self.trim_in_sec) / self.duration_sec
        frame_dur = 1.0 / float(self.fps) if self.fps else 0.0
        tolerance = (0.5 * frame_dur / self.duration_sec) + 1e-9
        if played_fraction < -tolerance or played_fraction > 1.0 + tolerance:
            return None
        return max(0.0, min(1.0, played_fraction))

    def clone_for_split(self, trim_in_sec, trim_out_sec, start_frame,
                         source_duration_sec=None, trim_in_floor_sec=None):
        """Builds a sibling AudioClip that reuses this clip's decoded
        peaks/sample_rate/full_source_duration_sec (no re-decoding the
        source file) with new trim/start values and a fresh id.

        `source_duration_sec`/`trim_in_floor_sec` default to this clip's own
        values (plain inheritance), but the split feature overrides them to
        wall off each sibling from the other's territory: the left sibling
        gets its `source_duration_sec` capped at the cut point (so its right
        edge can never be trimmed back out past the cut), and the right
        sibling gets its `trim_in_floor_sec` raised to the cut point (so its
        left edge can never be trimmed back past it either). Without this,
        both siblings would still share the full original source_duration_sec
        and could each be re-trimmed out to the whole file, duplicating audio.
        """
        clone = AudioClip.__new__(AudioClip)
        clone.id = str(uuid.uuid4())
        clone.file_path = self.file_path
        clone.name = self.name
        clone.fps = self.fps
        clone.source_duration_sec = self.source_duration_sec if source_duration_sec is None else source_duration_sec
        clone.full_source_duration_sec = self.full_source_duration_sec
        clone.sample_rate = self.sample_rate
        clone.peaks = self.peaks
        clone.trim_in_sec = trim_in_sec
        clone.trim_out_sec = trim_out_sec
        clone.trim_in_floor_sec = self.trim_in_floor_sec if trim_in_floor_sec is None else trim_in_floor_sec
        clone.start_frame = start_frame
        clone.volume_points = list(self.volume_points)
        return clone

    def clone(self, start_frame):
        """A full, independent duplicate of this clip (fresh id, given
        start_frame) -- same trim window, volume envelope, and permanent
        extent (trim_in_floor_sec/source_duration_sec), so a copy of a
        trimmed/split/volume-edited clip pastes back with all of that
        intact. Reuses clone_for_split's copying since a plain copy is the
        same operation, just with identical (not diverging) trim/floor/
        ceiling values."""
        return self.clone_for_split(
            self.trim_in_sec, self.trim_out_sec, start_frame,
            source_duration_sec=self.source_duration_sec,
            trim_in_floor_sec=self.trim_in_floor_sec,
        )


class AudioTrack:
    def __init__(self, name="Track"):
        self.id = str(uuid.uuid4())
        self.name = name
        self.clips = []
        self.muted = False

    def add_clip(self, clip):
        self.clips.append(clip)

    def remove_clip(self, clip_id):
        self.clips = [c for c in self.clips if c.id != clip_id]

    def clip_at_frame(self, frame):
        for c in self.clips:
            if c.contains_frame(frame):
                return c
        return None

    def find_insert_start(self, desired_start, length):
        """Resolve a start frame for a new clip of `length` frames that
        would otherwise land at `desired_start` (typically the playhead),
        nudging it clear of whatever it would overlap -- cascading past
        further clips too if the first gap it's nudged into still isn't
        big enough.
        """
        return self._resolve_free_start(self.clips, desired_start, length)

    def clamp_move_start(self, clip, desired_start):
        """Clamp `desired_start` for an existing `clip` already on this
        track so that moving it there can't overlap any other clip,
        cascading past further clips (in whichever direction the drag is
        already closer to) if the immediate gap isn't big enough to hold
        it -- e.g. dropping it into a gap between two other clips, or
        between the track start and the first clip.
        """
        others = [c for c in self.clips if c.id != clip.id]
        return self._resolve_free_start(others, desired_start, clip.length_frames)

    @staticmethod
    def _resolve_free_start(clips, desired_start, length):
        desired_start = max(0, desired_start)
        clips = sorted(clips, key=lambda c: c.start_frame)
        desired_end = desired_start + length

        overlap_idx = None
        for i, c in enumerate(clips):
            if c.start_frame < desired_end and c.end_frame > desired_start:
                overlap_idx = i
                break
        if overlap_idx is None:
            return desired_start

        overlapping = clips[overlap_idx]
        closer_to_start = (desired_start - overlapping.start_frame) <= (overlapping.end_frame - desired_start)

        def search_backward():
            # Walk toward frame 0, skipping past clips whose preceding
            # gap is too small, stopping at the first gap that fits.
            i = overlap_idx
            while i >= 0:
                left = clips[i - 1].end_frame if i > 0 else 0
                right = clips[i].start_frame
                if right - left >= length:
                    return right - length
                i -= 1
            return None

        def search_forward():
            # Walk away from frame 0, skipping past clips whose
            # following gap is too small. The space after the last clip
            # is unbounded, so this always eventually succeeds.
            i = overlap_idx
            n = len(clips)
            while i < n:
                left = clips[i].end_frame
                right = clips[i + 1].start_frame if i + 1 < n else None
                if right is None or right - left >= length:
                    return left
                i += 1
            return None

        if closer_to_start:
            start = search_backward()
            return start if start is not None else search_forward()
        start = search_forward()
        return start if start is not None else search_backward()
