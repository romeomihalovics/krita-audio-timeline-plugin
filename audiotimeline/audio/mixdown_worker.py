import threading

from PyQt5.QtCore import QThread, pyqtSignal

from . import mixdown


class MixdownWorker(QThread):
    """Runs mixdown.render_mixdown() off the UI thread. It only touches a
    plain snapshot of the track/clip data (see mixdown.snapshot_tracks) plus
    numpy/wave file I/O -- no Qt widgets or Krita API calls -- so it's safe
    to run concurrently with the user still editing the live timeline."""

    succeeded = pyqtSignal(str)   # out_path
    failed = pyqtSignal(str)      # error message
    cancelled = pyqtSignal()

    def __init__(self, tracks_snapshot, fps, total_frames, out_path, parent=None):
        super().__init__(parent)
        self._tracks_snapshot = tracks_snapshot
        self._fps = fps
        self._total_frames = total_frames
        self._out_path = out_path
        self._cancel_event = threading.Event()

    def cancel(self):
        """Cooperatively asks this render to stop at its next checkpoint
        (see render_mixdown's `should_cancel`) -- called by
        MixdownController when a newer edit arrives before this render
        finishes, so the (about to be stale) result is never applied and
        the correct re-render can start immediately after, instead of
        waiting for this run to finish decoding/writing a result nobody
        wants anymore. threading.Event is safe to set from another
        thread, which is exactly how this is called."""
        self._cancel_event.set()

    def run(self):
        try:
            mixdown.render_mixdown(
                self._tracks_snapshot, self._fps, self._total_frames, self._out_path,
                should_cancel=self._cancel_event.is_set,
            )
        except mixdown.MixdownCancelled:
            self.cancelled.emit()
            return
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(self._out_path)
