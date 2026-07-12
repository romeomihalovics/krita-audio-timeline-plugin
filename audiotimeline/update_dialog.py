from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QCheckBox,
)

from . import updater


class UpdateDialog(QDialog):
    """Used for both the manual "Check for Updates" flow (opened from the
    settings dialog) and the automatic startup prompt -- same widget, two
    entry points, since the "check -> show progress/result -> optionally
    confirm -> install -> tell user to restart" sequence is identical
    either way; only the initial state and the "don't show again"
    checkbox differ."""

    def __init__(self, parent=None, automatic=False, release_info=None):
        super().__init__(parent)
        self.setWindowTitle("Audio Timeline Updates")
        self.setMinimumWidth(360)

        self._automatic = automatic
        self._release_info = release_info
        self._check_thread = None
        self._apply_thread = None

        layout = QVBoxLayout(self)

        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)  # indeterminate/busy spinner
        layout.addWidget(self._progress_bar)

        self._dont_show_again_cb = QCheckBox("Don't show again")
        self._dont_show_again_cb.setChecked(False)
        layout.addWidget(self._dont_show_again_cb)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self._install_btn = QPushButton("Install")
        self._install_btn.clicked.connect(self._on_install_clicked)
        button_row.addWidget(self._install_btn)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        button_row.addWidget(self._cancel_btn)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.close)
        button_row.addWidget(self._close_btn)
        layout.addLayout(button_row)

        if release_info is not None:
            self._show_update_available(release_info)
        else:
            self._start_check()

    # ------------------------------------------------------------- states
    def _set_buttons(self, install=False, cancel=False, close=False):
        self._install_btn.setVisible(install)
        self._cancel_btn.setVisible(cancel)
        self._close_btn.setVisible(close)

    def _start_check(self):
        self._status_label.setText("Checking for updates…")
        self._progress_bar.setVisible(True)
        self._dont_show_again_cb.setVisible(False)
        self._set_buttons()

        thread = updater.UpdateCheckWorker(self)
        thread.checked.connect(self._on_checked)
        thread.failed.connect(self._on_check_failed)
        self._check_thread = thread
        thread.start()

    def _on_checked(self, info):
        if info is None:
            self._status_label.setText(f"You're up to date (v{updater.current_version()}).")
            self._progress_bar.setVisible(False)
            self._dont_show_again_cb.setVisible(False)
            self._set_buttons(close=True)
            return
        self._show_update_available(info)

    def _on_check_failed(self, message):
        self._status_label.setText(f"Couldn't check for updates: {message}.")
        self._progress_bar.setVisible(False)
        self._dont_show_again_cb.setVisible(False)
        self._set_buttons(close=True)

    def _show_update_available(self, info):
        self._release_info = info
        self._status_label.setText(
            f"A new version is available: v{info['version']} "
            f"(you have v{updater.current_version()})."
        )
        self._progress_bar.setVisible(False)
        # The "don't show again" checkbox only makes sense for the
        # automatic startup prompt -- the manual flow's dialog was already
        # opened deliberately, so gating installation behind an extra
        # opt-out there would be redundant.
        self._dont_show_again_cb.setVisible(self._automatic)
        self._set_buttons(install=True, cancel=True)

    def _on_install_clicked(self):
        self._status_label.setText("Downloading and installing update…")
        self._progress_bar.setVisible(True)
        self._dont_show_again_cb.setVisible(False)
        self._set_buttons()

        thread = updater.UpdateApplyWorker(self._release_info["zip_url"], self)
        thread.succeeded.connect(self._on_install_succeeded)
        thread.failed.connect(self._on_install_failed)
        self._apply_thread = thread
        thread.start()

    def _on_install_succeeded(self):
        self._status_label.setText("Update installed. Restart Krita for the changes to take effect.")
        self._progress_bar.setVisible(False)
        self._set_buttons(close=True)

    def _on_install_failed(self, message):
        self._status_label.setText(f"Update failed: {message}.")
        self._progress_bar.setVisible(False)
        self._set_buttons(close=True)

    def _on_cancel_clicked(self):
        self.close()

    def closeEvent(self, event):
        # Covers every way the dialog can go away -- Cancel, Close, the
        # window's own X button, Esc -- not just an explicit Cancel click.
        if self._automatic and self._dont_show_again_cb.isChecked():
            settings = updater.load_update_settings()
            settings["auto_check_updates"] = False
            updater.save_update_settings(settings)
        super().closeEvent(event)
