"""settings_dialog / update_dialog / info_dialog: the update-check UI flow
(driven by a real UpdateCheckWorker/UpdateApplyWorker run synchronously in
this thread instead of a real background one, and a fully mocked network
-- see test_updater.py for why), the settings toggle actually persisting to
disk, and a construction smoke test for the mostly-static feature list
dialog.
"""

from unittest.mock import MagicMock, patch

import pytest
from PyQt5.QtGui import QDesktopServices

from .. import updater
from ..settings_dialog import SettingsDialog
from ..update_dialog import UpdateDialog
from ..info_dialog import InfoDialog, FEATURES


@pytest.fixture(autouse=True)
def _isolated_plugin_dir(tmp_path, monkeypatch):
    """Every settings read/write in these tests must hit a throwaway
    directory, never the real installed plugin_meta.json/update_settings.json."""
    monkeypatch.setattr(updater, "plugin_dir", lambda: str(tmp_path))
    (tmp_path / updater.META_FILENAME).write_text('{"version": "1.0.0"}')
    return tmp_path


@pytest.fixture(autouse=True)
def _synchronous_workers(monkeypatch):
    """Runs UpdateCheckWorker/UpdateApplyWorker's run() directly instead of
    spinning up a real QThread, so signal delivery is deterministic and
    synchronous within the test."""
    monkeypatch.setattr(updater.UpdateCheckWorker, "start", lambda self: self.run())
    monkeypatch.setattr(updater.UpdateApplyWorker, "start", lambda self: self.run())


# ------------------------------------------------------------- settings_dialog
def test_settings_dialog_shows_current_version(qtbot):
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert "1.0.0" in dialog.findChild(type(dialog.layout().itemAt(0).widget())).text()


def test_settings_dialog_checkbox_reflects_saved_setting(qtbot):
    updater.save_update_settings({"auto_check_updates": False})
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    assert dialog._auto_check_cb.isChecked() is False


def test_toggling_checkbox_persists_setting(qtbot):
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    dialog._auto_check_cb.setChecked(False)
    assert updater.load_update_settings() == {"auto_check_updates": False}
    dialog._auto_check_cb.setChecked(True)
    assert updater.load_update_settings() == {"auto_check_updates": True}


def test_check_for_updates_button_opens_update_dialog(qtbot):
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    with patch("audiotimeline.settings_dialog.UpdateDialog") as mock_dialog_cls:
        mock_dialog_cls.return_value.exec = MagicMock()
        dialog._open_update_dialog()
    mock_dialog_cls.assert_called_once_with(dialog, automatic=False)
    mock_dialog_cls.return_value.exec.assert_called_once()


def test_report_issue_opens_external_url(qtbot):
    dialog = SettingsDialog()
    qtbot.addWidget(dialog)
    with patch.object(QDesktopServices, "openUrl") as mock_open:
        dialog._open_issues_page()
    mock_open.assert_called_once()
    assert updater.GITHUB_REPO in mock_open.call_args[0][0].toString()


# --------------------------------------------------------------- update_dialog
def test_update_dialog_shows_up_to_date_when_no_update(qtbot, monkeypatch):
    monkeypatch.setattr(updater, "fetch_latest_release_info", lambda: {"version": "1.0.0"})
    monkeypatch.setattr(updater, "is_update_available", lambda v: False)

    dialog = UpdateDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    assert "up to date" in dialog._status_label.text()
    assert dialog._close_btn.isVisible()
    assert not dialog._install_btn.isVisible()


def test_update_dialog_shows_update_available(qtbot, monkeypatch):
    info = {"tag": "v2.0.0", "version": "2.0.0", "zip_url": "https://example.invalid/x.zip"}
    monkeypatch.setattr(updater, "fetch_latest_release_info", lambda: info)
    monkeypatch.setattr(updater, "is_update_available", lambda v: True)

    dialog = UpdateDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    assert "2.0.0" in dialog._status_label.text()
    assert dialog._install_btn.isVisible()
    assert dialog._cancel_btn.isVisible()
    assert not dialog._close_btn.isVisible()


def test_update_dialog_automatic_shows_dont_show_again_checkbox(qtbot):
    info = {"tag": "v2.0.0", "version": "2.0.0", "zip_url": "https://example.invalid/x.zip"}
    dialog = UpdateDialog(automatic=True, release_info=info)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog._dont_show_again_cb.isVisible()


def test_update_dialog_manual_hides_dont_show_again_checkbox(qtbot):
    info = {"tag": "v2.0.0", "version": "2.0.0", "zip_url": "https://example.invalid/x.zip"}
    dialog = UpdateDialog(automatic=False, release_info=info)
    qtbot.addWidget(dialog)
    dialog.show()
    assert not dialog._dont_show_again_cb.isVisible()


def test_update_dialog_shows_failure_message(qtbot, monkeypatch):
    monkeypatch.setattr(updater, "fetch_latest_release_info", MagicMock(side_effect=OSError("no network")))

    dialog = UpdateDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    assert "Couldn't check for updates" in dialog._status_label.text()
    assert dialog._close_btn.isVisible()


def test_update_dialog_install_succeeds(qtbot, monkeypatch):
    info = {"tag": "v2.0.0", "version": "2.0.0", "zip_url": "https://example.invalid/x.zip"}
    monkeypatch.setattr(updater, "download_and_apply_update", lambda url: None)

    dialog = UpdateDialog(automatic=False, release_info=info)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._on_install_clicked()
    assert "installed" in dialog._status_label.text()
    assert dialog._close_btn.isVisible()


def test_update_dialog_install_fails(qtbot, monkeypatch):
    info = {"tag": "v2.0.0", "version": "2.0.0", "zip_url": "https://example.invalid/x.zip"}
    monkeypatch.setattr(
        updater, "download_and_apply_update",
        MagicMock(side_effect=RuntimeError("disk full")),
    )

    dialog = UpdateDialog(automatic=False, release_info=info)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._on_install_clicked()
    assert "Update failed" in dialog._status_label.text()
    assert "disk full" in dialog._status_label.text()


def test_close_event_with_dont_show_again_disables_auto_check(qtbot):
    info = {"tag": "v2.0.0", "version": "2.0.0", "zip_url": "https://example.invalid/x.zip"}
    dialog = UpdateDialog(automatic=True, release_info=info)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._dont_show_again_cb.setChecked(True)

    dialog.close()
    assert updater.load_update_settings() == {"auto_check_updates": False}


def test_close_event_without_dont_show_again_leaves_setting_untouched(qtbot):
    updater.save_update_settings({"auto_check_updates": True})
    info = {"tag": "v2.0.0", "version": "2.0.0", "zip_url": "https://example.invalid/x.zip"}
    dialog = UpdateDialog(automatic=True, release_info=info)
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.close()
    assert updater.load_update_settings() == {"auto_check_updates": True}


def test_close_event_manual_flow_never_touches_setting(qtbot):
    updater.save_update_settings({"auto_check_updates": True})
    info = {"tag": "v2.0.0", "version": "2.0.0", "zip_url": "https://example.invalid/x.zip"}
    dialog = UpdateDialog(automatic=False, release_info=info)
    qtbot.addWidget(dialog)
    dialog.show()
    # The manual flow's dialog has no visible "don't show again" checkbox
    # at all, so leaving it in its default (unchecked) state must never
    # disable auto-check on close.
    dialog.close()
    assert updater.load_update_settings() == {"auto_check_updates": True}


# ------------------------------------------------------------------ info_dialog
def test_info_dialog_constructs_and_lists_every_feature(qtbot):
    dialog = InfoDialog()
    qtbot.addWidget(dialog)
    assert dialog.windowTitle() == "Audio Timeline — Features"
    labels = dialog.findChildren(type(dialog.layout().itemAt(0).widget()))
    all_text = " ".join(label.text() for label in labels)
    for name, _keys, _description in FEATURES:
        assert name in all_text
