import os
import uuid

from .waveform_utils import analyze_audio_file


class AudioClip:
    """A single audio file placed on a track at a given start frame."""

    def __init__(self, file_path, start_frame, fps):
        self.id = str(uuid.uuid4())
        self.file_path = file_path
        self.name = os.path.basename(file_path)
        self.start_frame = start_frame
        self.fps = fps

        info = analyze_audio_file(file_path, fps)
        self.duration_sec = info.duration_sec
        self.sample_rate = info.sample_rate
        self.peaks = info.peaks  # list of (min, max) in [-1, 1]

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
