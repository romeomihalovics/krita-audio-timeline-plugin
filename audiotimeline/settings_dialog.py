from PyQt5.QtCore import QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QPushButton

from . import updater
from .update_dialog import UpdateDialog


class SettingsDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Audio Timeline Settings")
        self.setMinimumWidth(320)

        layout = QVBoxLayout(self)

        version_label = QLabel(f"Version {updater.current_version()}")
        layout.addWidget(version_label)

        self._auto_check_cb = QCheckBox("Automatically check for updates")
        self._auto_check_cb.setChecked(updater.load_update_settings()["auto_check_updates"])
        self._auto_check_cb.toggled.connect(self._on_auto_check_toggled)
        layout.addWidget(self._auto_check_cb)

        check_updates_btn = QPushButton("Check for Updates")
        check_updates_btn.clicked.connect(self._open_update_dialog)
        layout.addWidget(check_updates_btn)

        report_issue_label = QLabel(f'<a href="https://github.com/{updater.GITHUB_REPO}/issues">Report an issue</a>')
        report_issue_label.setOpenExternalLinks(False)
        report_issue_label.linkActivated.connect(self._open_issues_page)
        layout.addWidget(report_issue_label)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    def _on_auto_check_toggled(self, checked):
        settings = updater.load_update_settings()
        settings["auto_check_updates"] = checked
        updater.save_update_settings(settings)

    def _open_update_dialog(self):
        dialog = UpdateDialog(self, automatic=False)
        dialog.exec_()

    def _open_issues_page(self, _url=None):
        QDesktopServices.openUrl(QUrl(f"https://github.com/{updater.GITHUB_REPO}/issues"))
