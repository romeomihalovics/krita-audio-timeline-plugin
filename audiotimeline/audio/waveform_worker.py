"""Background decode of a single audio file's full waveform peaks, so
newly-imported clips (constructed with AudioClip(..., defer_analysis=True))
can appear immediately at their probed full length and be edited right away,
while the (potentially slow) full peak analysis finishes off the UI thread.
"""

from PyQt5.QtCore import QThread, pyqtSignal

from .waveform_utils import analyze_audio_file


class WaveformWorker(QThread):
    """Runs waveform_utils.analyze_audio_file() for one file path off the UI
    thread. Only touches the file on disk -- no Qt widgets, no Krita API, no
    live timeline objects -- so it's safe to run concurrently with the user
    editing the timeline (including splitting/copying the very clip whose
    waveform is still pending; see AudioTimelineDocker's by-file-path
    backfill in _on_waveform_ready)."""

    succeeded = pyqtSignal(str, object)  # file_path, AudioInfo
    failed = pyqtSignal(str, str)        # file_path, error message

    def __init__(self, file_path, fps, parent=None):
        super().__init__(parent)
        self._file_path = file_path
        self._fps = fps

    def run(self):
        try:
            info = analyze_audio_file(self._file_path, self._fps)
        except Exception as exc:
            self.failed.emit(self._file_path, str(exc))
            return
        self.succeeded.emit(self._file_path, info)
