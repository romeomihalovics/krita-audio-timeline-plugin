"""MixdownController: renders the timeline down to a WAV in the background
(via MixdownWorker) and hands it to Krita's native audio engine, coalescing
overlapping edits into a single re-render rather than racing them. Owned by
AudioTimelineDocker as `docker.mixdown`."""

import os
import tempfile

from PyQt5.QtWidgets import QMessageBox

from ..audio import mixdown
from ..audio.mixdown_worker import MixdownWorker


class MixdownController:
    def __init__(self, docker):
        self.docker = docker
        # Only one mixdown render runs at a time; if edits arrive while one
        # is in flight, remember the doc to re-render for once it finishes
        # rather than starting an overlapping render.
        self._thread = None
        self._pending_doc = None
        # None = not checked yet; True/False once the first real Document
        # tells us whether this Krita build has the setAudioTracks()/
        # audioTracks() API at all (added in Krita 5.3/6.0 -- absent on
        # 5.2.x and earlier). Checked lazily rather than at import time
        # since it needs an actual Document instance to probe.
        self._audio_api_supported = None
        # Every mixdown path ever handed out by mixdown_path_for(), so
        # AudioTimelineDocker.close() can clean all of them up rather than
        # just one.
        self.known_mixdown_paths = set()

    def mixdown_path_for(self, doc):
        """Every document gets its own mixdown wav, named after its
        doc_id. Keeping the file per-document (rather than one shared file
        for every open document) is what makes it safe to skip a re-render
        in mixdown_already_attached(): a document's file is only ever
        written to by that same document's own edits."""
        doc_id = self.docker.state_store.doc_id(doc)
        path = os.path.join(tempfile.gettempdir(), f"audiotimeline_mixdown_{doc_id}.wav")
        self.known_mixdown_paths.add(path)
        return path

    def audio_api_available(self, doc):
        """Checks (once, caching the result) whether this Krita build
        exposes Document.setAudioTracks()/audioTracks() at all -- that API
        was only added in Krita 5.3/6.0, so it's simply missing on 5.2.x
        and earlier. Rather than let every call site hit a raw
        AttributeError, warn about it once up front and have callers skip
        the Krita-side attach/clear from then on; the mixdown WAV is still
        rendered to disk regardless, just not handed to Krita automatically."""
        if self._audio_api_supported is None:
            self._audio_api_supported = hasattr(doc, "setAudioTracks") and hasattr(doc, "audioTracks")
            if not self._audio_api_supported:
                QMessageBox.warning(
                    None, "Audio Timeline",
                    "This Krita build doesn't support Document.setAudioTracks() / "
                    "audioTracks() -- that API was only added in Krita 5.3 / 6.0. "
                    "You appear to be on an older build (e.g. 5.2.x).\n\n"
                    "Audio Timeline will still let you arrange clips and will keep "
                    "rendering the mixed-down WAV to your temp folder, but it can't "
                    "hand that audio to Krita's native playback engine on this "
                    "version -- update Krita to 5.3.2+ or 6.0+ for that, or import "
                    "the rendered WAV manually via Krita's own \"Import Audio for "
                    "Animation\"."
                )
        return self._audio_api_supported

    def mixdown_already_attached(self, doc):
        """True if Krita's built-in Document.audioTracks() already has
        something set for this doc -- whether that's our own mixdown from
        an earlier render or anything else. Just switching to (or opening)
        a document shouldn't cost a re-render when it already has audio
        attached; a real edit (_on_content_changed) always re-renders
        regardless of this check."""
        if not self.audio_api_available(doc):
            return False
        try:
            current = doc.audioTracks()
        except Exception:
            return False
        return bool(current)

    def render_and_apply(self, doc):
        docker = self.docker
        timeline = docker.timeline
        if not any(t.clips for t in timeline.tracks):
            # Nothing left to mix down (e.g. the last clip was just
            # deleted/undone) -- clear whatever mixdown Krita still has
            # applied rather than leaving stale audio attached to the doc.
            self._pending_doc = None
            self.clear_krita_audio(doc)
            return

        if self._thread is not None and self._thread.isRunning():
            # A render is already in flight for an earlier edit -- rather
            # than start a second one racing it (or block here until it's
            # done), just remember to re-render once it finishes so the
            # result reflects this latest edit too.
            self._pending_doc = doc
            return

        mixdown_path = self.mixdown_path_for(doc)
        snapshot = mixdown.snapshot_tracks(timeline.tracks)
        docker._set_mixdown_busy(True)

        thread = MixdownWorker(
            snapshot, timeline.fps, timeline.total_frames, mixdown_path, docker,
        )
        thread.succeeded.connect(lambda path, d=doc: self._on_succeeded(d, path))
        thread.failed.connect(self._on_failed)
        self._thread = thread
        thread.start()

    def _on_succeeded(self, doc, path):
        self.docker._set_mixdown_busy(False)
        self.apply_to_krita(doc, path)
        self.start_pending_if_any()

    def _on_failed(self, message):
        self.docker._set_mixdown_busy(False)
        QMessageBox.warning(None, "Audio Timeline", f"Could not render mixdown: {message}")
        self.start_pending_if_any()

    def start_pending_if_any(self):
        doc = self._pending_doc
        self._pending_doc = None
        if doc is not None:
            self.render_and_apply(doc)

    def apply_to_krita(self, doc, path):
        if not self.audio_api_available(doc):
            return
        try:
            doc.setAudioTracks([path])
            if doc.audioLevel() <= 0.0:
                doc.setAudioLevel(1.0)
        except Exception as exc:
            QMessageBox.warning(
                None, "Audio Timeline",
                "Krita's Document.setAudioTracks() call failed "
                f"({exc}). The mixed-down audio was still rendered to:\n"
                f"{path}\nyou can load it manually via "
                "Krita's own audio-for-animation import."
            )

    def clear_krita_audio(self, doc):
        if not self.audio_api_available(doc):
            return
        try:
            doc.setAudioTracks([])
        except Exception as exc:
            QMessageBox.warning(None, "Audio Timeline", f"Could not clear Krita's audio track: {exc}")

    def wait_for_shutdown(self, timeout_ms=2000):
        if self._thread is not None and self._thread.isRunning():
            self._thread.wait(timeout_ms)
