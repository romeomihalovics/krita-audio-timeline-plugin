import os

from krita import DockWidget

from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QTransform
from PyQt5.QtWidgets import QFileDialog, QMessageBox

from .ui.timeline_widget import AudioTimelineWidget
from .ui import docker_ui
from .ui.docker_ui import SPINNER_SIZE, SPINNER_INTERVAL_MS, SPINNER_DEGREES_PER_TICK
from .ui.mixdown_controller import MixdownController
from .ui.state_persistence import DocStateStore
from .ui.playback_sync import PlaybackSync
from .audio.audio_track import AudioTrack, AudioClip
from . import commands
from . import updater
from .update_dialog import UpdateDialog
from .settings_dialog import SettingsDialog
from .info_dialog import InfoDialog

POLL_INTERVAL_MS = 40           # ~25 checks/sec; matches typical playback tick rate


class AudioTimelineDocker(DockWidget):
    """Composition root: owns the timeline widget and wires together the
    controllers each responsibility was split into --
    MixdownController (rendering/applying the mixdown), DocStateStore
    (per-document annotation persistence), and PlaybackSync (following
    Krita's active document's frame/fps/range) -- plus the docker chrome
    itself, built by ui.docker_ui. See ui/mixdown_controller.py,
    ui/state_persistence.py, ui/playback_sync.py, ui/docker_ui.py."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Audio Timeline")

        self.timeline = AudioTimelineWidget()
        self.timeline.scrubbed.connect(lambda frame: self.playback.on_timeline_scrubbed(frame))
        # Every track/clip mutation goes through timeline.undo_stack (see
        # commands.py) and reports here via contentChanged, whether it's
        # the original action or an undo/redo of it. The annotation is
        # always re-saved, but the (comparatively expensive) mixdown
        # re-render only runs when the mutation actually affects audio.
        self.timeline.contentChanged.connect(self._on_content_changed)

        self.mixdown = MixdownController(self)
        self.state_store = DocStateStore(self)
        self.playback = PlaybackSync(self)

        docker_ui.build_undo_redo_actions(self)
        docker_ui.build_ui(self)

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(POLL_INTERVAL_MS)
        self.poll_timer.timeout.connect(self.playback.poll)
        self.poll_timer.start()

        self._mixdown_spinner_angle = 0
        self._mixdown_spinner_timer = QTimer(self)
        self._mixdown_spinner_timer.setInterval(SPINNER_INTERVAL_MS)
        self._mixdown_spinner_timer.timeout.connect(self._tick_mixdown_spinner)

        self._update_check_thread = None
        # Delay avoids competing with Krita's own startup work / other
        # docker initialization.
        QTimer.singleShot(3000, self._maybe_auto_check_for_updates)

    # ------------------------------------------------------------------ UI
    def _set_mixdown_busy(self, busy):
        self._mixdown_spinner.setVisible(busy)
        if busy:
            self._mixdown_spinner_angle = 0
            self._mixdown_spinner.setPixmap(self._mixdown_spinner_pixmap)
            self._mixdown_spinner_timer.start()
        else:
            self._mixdown_spinner_timer.stop()

    def _tick_mixdown_spinner(self):
        self._mixdown_spinner_angle = (self._mixdown_spinner_angle + SPINNER_DEGREES_PER_TICK) % 360
        transform = QTransform().rotate(self._mixdown_spinner_angle)
        rotated = self._mixdown_spinner_pixmap.transformed(transform, Qt.SmoothTransformation)
        # Rotating a square pixmap around its center grows the bounding box
        # on diagonal angles -- recenter it into a fixed SPINNER_SIZE canvas
        # so the icon doesn't visibly drift as it spins.
        x = (rotated.width() - SPINNER_SIZE) // 2
        y = (rotated.height() - SPINNER_SIZE) // 2
        cropped = rotated.copy(x, y, SPINNER_SIZE, SPINNER_SIZE)
        self._mixdown_spinner.setPixmap(cropped)

    # --------------------------------------------------------------- krita
    def canvasChanged(self, canvas):
        # Required override. Called when the active document/view changes.
        self.playback.canvas_changed(canvas)

    # -------------------------------------------------------------- updates
    def _open_settings_dialog(self):
        SettingsDialog(self).exec_()

    def _open_info_dialog(self):
        InfoDialog(self).exec_()

    def _maybe_auto_check_for_updates(self):
        if updater.auto_check_already_done_this_session():
            return
        updater.mark_auto_check_done_this_session()
        if not updater.load_update_settings()["auto_check_updates"]:
            return

        thread = updater.UpdateCheckWorker(self)
        thread.checked.connect(self._on_auto_check_checked)
        thread.failed.connect(self._on_auto_check_failed)
        self._update_check_thread = thread
        thread.start()

    def _on_auto_check_checked(self, info):
        if info is None:
            return  # already up to date -- automatic checks are silent unless there's an update
        dialog = UpdateDialog(self, automatic=True, release_info=info)
        dialog.exec_()

    def _on_auto_check_failed(self, _message):
        pass  # automatic checks fail silently; only the manual flow surfaces errors

    # ------------------------------------------------------------- actions
    def add_track(self):
        track = AudioTrack(name=f"Track {len(self.timeline.tracks) + 1}")
        self.timeline.undo_stack.push(commands.AddTrackCommand(self.timeline, track))

    def import_audio(self):
        path, _ = QFileDialog.getOpenFileName(
            None, "Import Audio Clip", "",
            "Audio Files (*.wav *.mp3 *.ogg *.flac);;All Files (*)"
        )
        if not path:
            return
        if not self.timeline.tracks:
            self.add_track()

        doc = self.playback.active_document()
        fps = doc.framesPerSecond() if doc else self.timeline.fps
        start_frame = self.timeline.current_frame

        try:
            # defer_analysis=True: only a fast, decode-free duration probe
            # runs synchronously here, so the file dialog doesn't hang on a
            # long import -- the clip appears immediately at its correct
            # full length and is fully editable, with the real waveform
            # filled in shortly after by a background decode (see
            # AudioTimelineWidget.request_waveform).
            clip = AudioClip(path, start_frame, fps, defer_analysis=True)
        except Exception as exc:  # e.g. missing pydub for non-wav files
            QMessageBox.critical(None, "Audio Timeline", str(exc))
            return

        idx = self.timeline.active_track_index
        if not (0 <= idx < len(self.timeline.tracks)):
            idx = 0
        track = self.timeline.tracks[idx]
        # Nudge the clip off the playhead if it would otherwise land on
        # top of an existing clip in this track.
        clip.start_frame = track.find_insert_start(clip.start_frame, clip.length_frames)
        self.timeline.undo_stack.push(commands.AddClipCommand(self.timeline, track, clip))
        self.timeline.request_waveform(path)

    # --------------------------------------------------------------- mixdown
    def _on_content_changed(self, affects_audio):
        self.state_store.save_state()
        if not affects_audio:
            return
        doc = self.playback.active_document()
        if doc is not None:
            self.mixdown.render_and_apply(doc)

    # ------------------------------------------------------------- cleanup
    def close(self):
        self.poll_timer.stop()
        self._mixdown_spinner_timer.stop()
        self.mixdown.wait_for_shutdown()
        self.timeline.wait_for_waveform_shutdown()
        for path in self.mixdown.known_mixdown_paths:
            try:
                os.remove(path)
            except OSError:
                pass
        super().close()
