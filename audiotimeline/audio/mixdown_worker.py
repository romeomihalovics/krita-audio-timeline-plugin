from PyQt5.QtCore import QThread, pyqtSignal

from . import mixdown


class MixdownWorker(QThread):
    """Runs mixdown.render_mixdown() off the UI thread. It only touches a
    plain snapshot of the track/clip data (see mixdown.snapshot_tracks) plus
    numpy/wave file I/O -- no Qt widgets or Krita API calls -- so it's safe
    to run concurrently with the user still editing the live timeline."""

    succeeded = pyqtSignal(str)   # out_path
    failed = pyqtSignal(str)      # error message

    def __init__(self, tracks_snapshot, fps, total_frames, out_path, parent=None):
        super().__init__(parent)
        self._tracks_snapshot = tracks_snapshot
        self._fps = fps
        self._total_frames = total_frames
        self._out_path = out_path

    def run(self):
        try:
            mixdown.render_mixdown(
                self._tracks_snapshot, self._fps, self._total_frames, self._out_path,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(self._out_path)
