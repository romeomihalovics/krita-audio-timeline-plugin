"""ClipboardMixin: copy/paste (internal clip clipboard, and external file
paths pasted or dragged in from the OS) plus the background waveform-decode
queue those imports kick off. Mixed into AudioTimelineWidget alongside
PaintingMixin/VolumeEditingMixin/InteractionMixin (see ui/timeline_widget.py).
"""

from PyQt5.QtWidgets import QApplication, QMessageBox

from .. import commands
from ..audio.audio_track import AudioClip, AudioTrack
from ..audio.waveform_worker import WaveformWorker
from .timeline_constants import EXTERNAL_AUDIO_EXTENSIONS


class ClipboardMixin:
    def copy_selected_clip(self):
        """Ctrl+C: remembers the selected clip for a later Ctrl+V. Stores a
        live reference to the clip itself (not a frozen snapshot), so
        pasting after further edits (trim, volume, etc.) copies its
        then-current state -- same as most clipboards."""
        if self.selected_clip is not None:
            self._copy_clip(self.selected_clip)

    def _copy_clip(self, clip):
        """Shared by the Ctrl+C shortcut and the clip context menu's "Copy
        Clip" action. Clears the system clipboard so a subsequent paste
        picks up *this* clip rather than some stale external file path --
        see paste_clip/_paste_at, which check the system clipboard first."""
        self._clipboard_clip = clip
        QApplication.clipboard().clear()

    def paste_clip(self):
        """Ctrl+V: pastes onto the active track at the playhead -- see
        _paste_at for the actual clipboard-source priority."""
        idx = self.active_track_index if self.tracks else 0
        self._paste_at(idx, self.current_frame)

    def _paste_at(self, track_idx, frame):
        """Pastes at `frame` on track `track_idx` -- shared by the Ctrl+V
        shortcut (paste_clip, always at the playhead/active track) and the
        empty-space context menu's "Paste" action (at the clicked
        position). A file copied from outside Krita (e.g. the OS file
        manager) takes priority over a clip copied inside the timeline --
        see _copy_clip, which clears the system clipboard on an internal
        copy specifically so this ordering resolves unambiguously rather
        than on which happened more recently."""
        paths = self._external_audio_paths(QApplication.clipboard().mimeData())
        if paths:
            self._import_external_audio_files(paths, track_idx, frame)
            return
        if self._clipboard_clip is None:
            return
        if not (0 <= track_idx < len(self.tracks)):
            return
        track = self.tracks[track_idx]
        start_frame = track.find_insert_start(frame, self._clipboard_clip.length_frames)
        new_clip = self._clipboard_clip.clone(start_frame)
        self.undo_stack.push(commands.AddClipCommand(self, track, new_clip))

    def _clipboard_has_content(self):
        """Whether paste_clip/_paste_at currently has anything to paste --
        either an internally-copied clip or an external file path -- used
        to decide whether the empty-space context menu offers "Paste"."""
        if self._clipboard_clip is not None:
            return True
        return bool(self._external_audio_paths(QApplication.clipboard().mimeData()))

    # --------------------------------------------------------- external i/o
    @staticmethod
    def _external_audio_paths(mime_data):
        """Local file paths in `mime_data` with a supported audio
        extension -- shared by drag-and-drop from the OS file manager
        (dropEvent) and pasting a file copied there (paste_clip)."""
        if mime_data is None or not mime_data.hasUrls():
            return []
        paths = []
        for url in mime_data.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            if path.lower().endswith(EXTERNAL_AUDIO_EXTENSIONS):
                paths.append(path)
        return paths

    def _import_external_audio_files(self, paths, track_idx, start_frame):
        """Imports each of `paths` onto track `track_idx` (creating a first
        track if there isn't one yet, same fallback as
        AudioTimelineDocker.import_audio), each nudged clear of whatever it
        would otherwise overlap -- shared by dropEvent and paste_clip's
        external-clipboard fallback."""
        if not paths:
            return
        if not self.tracks:
            track = AudioTrack(name="Track 1")
            self.undo_stack.push(commands.AddTrackCommand(self, track))
            track_idx = 0
        if not (0 <= track_idx < len(self.tracks)):
            track_idx = max(0, len(self.tracks) - 1)
        track = self.tracks[track_idx]
        for path in paths:
            try:
                # defer_analysis=True: only a fast, decode-free duration
                # probe runs here, so the clip appears at its correct full
                # length and is immediately editable; the real waveform
                # decodes in the background (see request_waveform) and
                # backfills peaks onto every clip sharing this file_path
                # once it's ready (including any split/copy made from this
                # clip before that happens).
                clip = AudioClip(path, start_frame, self.fps, defer_analysis=True)
            except Exception as exc:  # e.g. missing pydub for non-wav files
                QMessageBox.critical(self, "Audio Timeline", str(exc))
                continue
            clip.start_frame = track.find_insert_start(clip.start_frame, clip.length_frames)
            self.undo_stack.push(commands.AddClipCommand(self, track, clip))
            self.request_waveform(path)
        self.active_track_index = track_idx

    def request_waveform(self, file_path):
        """Queues a background full waveform decode for `file_path`, unless
        one is already queued/in-flight for it (e.g. several clips from the
        same source file were just imported/split/pasted at once)."""
        if file_path == self._waveform_inflight_path or file_path in self._waveform_queue:
            return
        # Opportunistic: the file currently being decoded (if any) may have
        # been orphaned by an edit since its decode started (its one clip
        # deleted, or that add undone) -- this is the next point this
        # bookkeeping gets touched, so check here rather than let an
        # already-pointless decode run to completion and compete with
        # whatever this new request is about to need decoded instead.
        if (self._waveform_thread is not None and self._waveform_thread.isRunning()
                and self._waveform_inflight_path is not None
                and not self._file_still_needed(self._waveform_inflight_path)):
            self._waveform_thread.cancel()
        self._waveform_queue.append(file_path)
        self._start_next_waveform_job()

    def _file_still_needed(self, file_path):
        """Whether any clip on any track still has pending (peaks is None)
        waveform data for `file_path` -- used to skip/cancel decodes for
        files no clip needs anymore (e.g. the import that requested one
        was undone before the decode even started)."""
        return any(
            clip.file_path == file_path and clip.peaks is None
            for track in self.tracks for clip in track.clips
        )

    def _start_next_waveform_job(self):
        if self._waveform_thread is not None and self._waveform_thread.isRunning():
            return
        file_path = None
        while self._waveform_queue:
            candidate = self._waveform_queue.pop(0)
            if self._file_still_needed(candidate):
                file_path = candidate
                break
            # Nothing needs this one anymore (e.g. its clip was deleted/
            # undone while still queued, before a thread ever started for
            # it) -- drop it instead of spending a decode on it.
        if file_path is None:
            self._waveform_inflight_path = None
            return
        self._waveform_inflight_path = file_path
        thread = WaveformWorker(file_path, self.fps, self)
        thread.succeeded.connect(self._on_waveform_ready)
        thread.failed.connect(self._on_waveform_failed)
        thread.cancelled.connect(self._on_waveform_cancelled)
        self._waveform_thread = thread
        thread.start()

    def _on_waveform_ready(self, file_path, info):
        """Backfills every clip on every track sharing `file_path` and
        still pending (peaks is None) -- not just the clip that originally
        triggered the decode -- so a split or copy made while the decode
        was in flight also gets its waveform filled in."""
        for track in self.tracks:
            for clip in track.clips:
                if clip.file_path == file_path:
                    clip.apply_analysis(info)
        self.update()
        self._start_next_waveform_job()

    def _on_waveform_failed(self, file_path, message):
        # Left with peaks=None permanently -- the clip stays fully usable
        # (placeholder line, correct length, editable, included in
        # mixdown), just without a waveform preview. A modal error here for
        # a background decode failure would be disruptive for what's a
        # purely cosmetic feature.
        self._start_next_waveform_job()

    def _on_waveform_cancelled(self, file_path):
        # Routine, not an error -- see request_waveform's opportunistic
        # cancel. No result to backfill; just move on to whatever's next.
        self._start_next_waveform_job()

    def wait_for_waveform_shutdown(self, timeout_ms=2000):
        """Blocks (briefly) for any in-flight background decode to finish
        -- called from AudioTimelineDocker.close() so the QThread is never
        destroyed while still running (which Qt does not tolerate) if the
        docker is closed mid-decode. Any still-queued paths are simply
        dropped; nothing needs their result once the docker is closing."""
        if self._waveform_thread is not None and self._waveform_thread.isRunning():
            self._waveform_thread.wait(timeout_ms)

    def dragEnterEvent(self, event):
        if self._external_audio_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._external_audio_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = self._external_audio_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        pos = event.pos()
        idx = self.track_index_at_y(pos.y())
        if idx < 0:
            idx = self.active_track_index if self.tracks else 0
        frame = self.x_to_frame(pos.x())
        self._import_external_audio_files(paths, idx, frame)
        event.acceptProposedAction()
        self.update()
