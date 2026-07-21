"""DocStateStore: per-document persistence -- saving the track/clip layout
into a Krita document annotation, restoring it (or a live in-session cache
of it) when a document becomes active again, and the stable per-document id
that keys all of that. Owned by AudioTimelineDocker as `docker.state_store`.
"""

import json
import os
import uuid

from PyQt5.QtWidgets import QUndoStack

from ..audio.audio_track import AudioTrack, AudioClip

ANNOTATION_KEY = "audiotimeline/state"
ANNOTATION_DESC = "Audio Timeline plugin state (tracks/clips)"
# A per-document id, persisted as its own annotation. Krita's Document
# wrapper is not guaranteed to be the same Python object across separate
# activeDocument()/canvasChanged calls for the same open document, so plain
# `is`-identity isn't reliable enough to key session-only per-document state
# (undo stacks, mixdown filenames) off of -- this annotation-backed id is
# stable for as long as the document stays open.
DOC_ID_ANNOTATION_KEY = "audiotimeline/doc_id"
DOC_ID_ANNOTATION_DESC = "Audio Timeline plugin per-document session id"


class DocStateStore:
    def __init__(self, docker):
        self.docker = docker
        # Each open document gets its own live tracks list + QUndoStack,
        # recorded here keyed by doc_id -- populated the first time a
        # document becomes active (see load_state_fresh()) and just
        # restored (not rebuilt) on every switch back to it, so switching
        # documents never discards undo/redo history.
        self._doc_states = {}
        self._loaded_doc_id = None  # doc_id() of the Document last loaded

    def doc_id(self, doc):
        """A stable id for `doc`, persisted as an annotation on the
        document itself -- generated the first time it's needed. See the
        DOC_ID_ANNOTATION_KEY comment for why this exists instead of just
        comparing/keying off the Document object."""
        try:
            raw = doc.annotation(DOC_ID_ANNOTATION_KEY)
        except Exception:
            raw = None
        doc_id = bytes(raw).decode("utf-8") if raw else ""
        if not doc_id:
            doc_id = uuid.uuid4().hex
            try:
                doc.setAnnotation(DOC_ID_ANNOTATION_KEY, DOC_ID_ANNOTATION_DESC, doc_id.encode("utf-8"))
            except Exception:
                pass  # best-effort -- worst case this doc looks "new" every time it's activated
        return doc_id

    def sync_active_doc_state(self):
        """Writes back whatever's changed on the timeline widget (right
        now, just active_track_index -- tracks and the undo stack are
        already the same objects stored in _doc_states, mutated in place)
        before switching away from the previously-active document."""
        if self._loaded_doc_id is None:
            return
        state = self._doc_states.get(self._loaded_doc_id)
        if state is not None:
            state['active_track_index'] = self.docker.timeline.active_track_index

    def note_loaded(self, doc_id):
        self._loaded_doc_id = doc_id

    def save_state(self):
        doc = self.docker.playback.active_document()
        if doc is None:
            return
        timeline = self.docker.timeline
        data = {
            "tracks": [
                {
                    "name": track.name,
                    "muted": track.muted,
                    "clips": [
                        {
                            "file_path": clip.file_path,
                            "start_frame": clip.start_frame,
                            "fps": clip.fps,
                            "source_duration_sec": clip.source_duration_sec,
                            "trim_in_sec": clip.trim_in_sec,
                            "trim_out_sec": clip.trim_out_sec,
                            "trim_in_floor_sec": clip.trim_in_floor_sec,
                            "volume_points": [list(p) for p in clip.volume_points],
                        }
                        for clip in track.clips
                    ],
                }
                for track in timeline.tracks
            ]
        }
        payload = json.dumps(data).encode("utf-8")
        try:
            doc.setAnnotation(ANNOTATION_KEY, ANNOTATION_DESC, payload)
        except Exception:
            pass  # best-effort; a save failure here shouldn't block editing

    def load_state(self, doc):
        docker = self.docker
        timeline = docker.timeline
        state = self._doc_states.get(self.doc_id(doc))
        if state is not None:
            # Already visited this document earlier in this Krita session
            # -- restore its live tracks/undo stack rather than re-parsing
            # the saved annotation, so switching back to it doesn't lose
            # undo/redo history built up on an earlier visit.
            timeline.tracks = state['tracks']
            timeline.undo_stack = state['undo_stack']
            timeline.active_track_index = state['active_track_index']
            timeline.selected_clip = None
            docker.undo_group.setActiveStack(state['undo_stack'])
            timeline.refresh_layout()
        else:
            self.load_state_fresh(doc)

        if docker.mixdown.mixdown_already_attached(doc):
            # Switching to this document, not editing it -- its own mixdown
            # is already sitting on Document.audioTracks() and up to date.
            return

        docker.mixdown.render_and_apply(doc)

    def load_state_fresh(self, doc):
        """Parses this document's saved annotation (if any) into a brand
        new tracks list + undo stack. Only runs the first time a document
        becomes active in this session -- see load_state()."""
        docker = self.docker
        timeline = docker.timeline
        try:
            raw = doc.annotation(ANNOTATION_KEY)
        except Exception:
            raw = None

        # A document with no saved annotation (e.g. brand new, or one that's
        # never had audio tracks) still needs the timeline reset below --
        # otherwise switching to it would just leave the previous
        # document's tracks/clips on screen.
        data = {}
        if raw:
            try:
                data = json.loads(bytes(raw).decode("utf-8"))
            except Exception:
                data = {}

        stack = QUndoStack(docker)
        docker.undo_group.addStack(stack)
        docker.undo_group.setActiveStack(stack)

        timeline.tracks = []
        timeline.selected_clip = None
        timeline.active_track_index = 0
        timeline.undo_stack = stack
        for track_dict in data.get("tracks", []):
            track = AudioTrack(name=track_dict.get("name", "Track"))
            track.muted = bool(track_dict.get("muted", False))
            # Registered in timeline.tracks *before* its clips are added
            # (and before request_waveform() is called for them) below --
            # request_waveform()'s own pruning (see ClipboardMixin.
            # _file_still_needed) checks self.tracks to decide whether a
            # queued decode is still needed, and would otherwise see this
            # track (and every clip about to be added to it) as not
            # existing yet, silently dropping the decode request for
            # every clip restored from a saved document.
            timeline.add_track(track)
            for clip_dict in track_dict.get("clips", []):
                path = clip_dict.get("file_path")
                if not path or not os.path.exists(path):
                    # Clip references a file that's since moved/been deleted
                    # -- skip it rather than crash the whole load, same
                    # path-based-reference caveat as fresh imports.
                    continue
                try:
                    # defer_analysis=True: loading a document with many/large
                    # clips only needs a fast duration probe per clip here,
                    # not a full decode -- otherwise switching to a
                    # heavy project would freeze the UI for as long as every
                    # clip's file takes to decode, one after another. Real
                    # waveforms are queued and filled in just below.
                    clip = AudioClip(
                        path, clip_dict.get("start_frame", 0),
                        clip_dict.get("fps", timeline.fps),
                        defer_analysis=True,
                    )
                except Exception:
                    continue
                # source_duration_sec may have been capped by a prior split
                # (see AudioClip.clone_for_split) -- restore that saved
                # ceiling rather than trusting the freshly re-decoded full
                # file duration, so a reloaded split clip still can't be
                # trimmed back out past its cut point.
                if "source_duration_sec" in clip_dict:
                    clip.source_duration_sec = float(clip_dict["source_duration_sec"])
                clip.trim_in_sec = float(clip_dict.get("trim_in_sec", 0.0))
                clip.trim_out_sec = float(clip_dict.get("trim_out_sec", 0.0))
                clip.trim_in_floor_sec = float(clip_dict.get("trim_in_floor_sec", 0.0))
                volume_points = clip_dict.get("volume_points")
                if volume_points:
                    clip.volume_points = [tuple(p) for p in volume_points]
                track.add_clip(clip)
                timeline.request_waveform(path)

        if not timeline.tracks:
            docker.add_track()

        # The fallback empty track above (or whatever was just parsed) is
        # pre-existing document state, not a fresh user edit -- it
        # shouldn't be undoable back to an empty timeline. Safe to clear
        # unconditionally here since this stack was only just created.
        stack.clear()

        self._doc_states[self.doc_id(doc)] = {
            'tracks': timeline.tracks,
            'undo_stack': stack,
            'active_track_index': timeline.active_track_index,
        }
