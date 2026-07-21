"""Background decode of a single audio file's full waveform peaks, so
newly-imported clips (constructed with AudioClip(..., defer_analysis=True))
can appear immediately at their probed full length and be edited right away,
while the (potentially slow) full peak analysis finishes off the UI thread.
"""

import threading

from PyQt5.QtCore import QThread, pyqtSignal

from .waveform_utils import analyze_audio_file, WaveformCancelled


class WaveformWorker(QThread):
    """Runs waveform_utils.analyze_audio_file() for one file path off the UI
    thread. Only touches the file on disk -- no Qt widgets, no Krita API, no
    live timeline objects -- so it's safe to run concurrently with the user
    editing the timeline (including splitting/copying the very clip whose
    waveform is still pending; see AudioTimelineDocker's by-file-path
    backfill in _on_waveform_ready)."""

    succeeded = pyqtSignal(str, object)  # file_path, AudioInfo
    failed = pyqtSignal(str, str)        # file_path, error message
    cancelled = pyqtSignal(str)          # file_path

    def __init__(self, file_path, fps, parent=None):
        super().__init__(parent)
        self._file_path = file_path
        self._fps = fps
        self._cancel_event = threading.Event()

    def cancel(self):
        """Cooperatively asks this decode to stop at its next checkpoint
        (see analyze_audio_file's `should_cancel`) -- called when nothing
        on the timeline needs this file's peaks anymore before the decode
        finished (e.g. the clip that requested it was deleted/undone).
        threading.Event is safe to set from another thread, which is
        exactly how this is called."""
        self._cancel_event.set()

    def run(self):
        try:
            info = analyze_audio_file(self._file_path, self._fps, should_cancel=self._cancel_event.is_set)
        except WaveformCancelled:
            self.cancelled.emit(self._file_path)
            return
        except Exception as exc:
            self.failed.emit(self._file_path, str(exc))
            return
        self.succeeded.emit(self._file_path, info)
