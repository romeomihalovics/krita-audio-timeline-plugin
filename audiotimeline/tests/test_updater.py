"""updater.py: version parsing/comparison, and the update-check/self-update
network flow -- entirely mocked (urllib.request.urlopen never actually
touches the network; no real GitHub API/zip download here), plus the
on-disk settings persistence.
"""

import io
import json
import zipfile
from unittest.mock import MagicMock, patch

import pytest

from .. import updater


# ------------------------------------------------------------- parse_version
@pytest.mark.parametrize("raw,expected", [
    ("1.4.0", (1, 4, 0)),
    ("v1.4.0", (1, 4, 0)),
    ("V2.0.0", (2, 0, 0)),
    ("1.4.0-beta", (1, 4, 0)),
    ("1.4", (1, 4)),
])
def test_parse_version(raw, expected):
    assert updater.parse_version(raw) == expected


def test_is_update_available_compares_tuples(monkeypatch):
    monkeypatch.setattr(updater, "current_version", lambda: "1.0.0")
    assert updater.is_update_available("1.1.0") is True
    assert updater.is_update_available("1.0.0") is False
    assert updater.is_update_available("0.9.0") is False


# ------------------------------------------------------------ settings i/o
def test_current_version_reads_plugin_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "plugin_dir", lambda: str(tmp_path))
    (tmp_path / updater.META_FILENAME).write_text(json.dumps({"version": "2.3.4"}))
    assert updater.current_version() == "2.3.4"


def test_current_version_missing_file_defaults_to_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "plugin_dir", lambda: str(tmp_path))
    assert updater.current_version() == "0.0.0"


def test_load_update_settings_defaults_true_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "plugin_dir", lambda: str(tmp_path))
    assert updater.load_update_settings() == {"auto_check_updates": True}


def test_save_and_load_update_settings_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "plugin_dir", lambda: str(tmp_path))
    updater.save_update_settings({"auto_check_updates": False})
    assert updater.load_update_settings() == {"auto_check_updates": False}


# ------------------------------------------------------------------ network
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self._pos = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        # shutil.copyfileobj (used by download_and_apply_update) reads in
        # fixed-size chunks, not all at once -- support both that and the
        # single no-arg read() fetch_latest_release_info does.
        if size is None or size < 0:
            chunk, self._pos = self._payload[self._pos:], len(self._payload)
            return chunk
        chunk = self._payload[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk


def test_fetch_latest_release_info_parses_github_response():
    payload = json.dumps({"tag_name": "v1.5.0"}).encode("utf-8")
    with patch("audiotimeline.updater.urllib.request.urlopen", return_value=_FakeResponse(payload)):
        info = updater.fetch_latest_release_info()
    assert info["tag"] == "v1.5.0"
    assert info["version"] == "1.5.0"
    assert info["zip_url"].endswith("/archive/refs/tags/v1.5.0.zip")


def test_fetch_latest_release_info_propagates_network_errors():
    with patch("audiotimeline.updater.urllib.request.urlopen", side_effect=OSError("no network")):
        with pytest.raises(OSError):
            updater.fetch_latest_release_info()


def _make_update_zip(version_tag="v9.9.9", extra_files=None):
    buf = io.BytesIO()
    root = f"krita-audio-timeline-plugin-{version_tag}"
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{root}/audiotimeline/plugin_meta.json", json.dumps({"version": version_tag.lstrip("v")}))
        zf.writestr(f"{root}/audiotimeline/updater.py", "# updated updater\n")
        zf.writestr(f"{root}/audiotimeline.desktop", "[Desktop Entry]\n")
        for rel_path, content in (extra_files or {}).items():
            zf.writestr(f"{root}/{rel_path}", content)
    return buf.getvalue()


def test_download_and_apply_update_copies_package_and_desktop_file(tmp_path, monkeypatch):
    dest_plugin_dir = tmp_path / "pykrita" / "audiotimeline"
    dest_plugin_dir.mkdir(parents=True)
    monkeypatch.setattr(updater, "plugin_dir", lambda: str(dest_plugin_dir))

    zip_bytes = _make_update_zip()
    with patch("audiotimeline.updater.urllib.request.urlopen", return_value=_FakeResponse(zip_bytes)):
        updater.download_and_apply_update("https://example.invalid/fake.zip")

    assert (dest_plugin_dir / "plugin_meta.json").exists()
    assert (dest_plugin_dir / "updater.py").read_text() == "# updated updater\n"
    assert (dest_plugin_dir.parent / "audiotimeline.desktop").exists()


def test_download_and_apply_update_preserves_existing_settings_file(tmp_path, monkeypatch):
    dest_plugin_dir = tmp_path / "pykrita" / "audiotimeline"
    dest_plugin_dir.mkdir(parents=True)
    monkeypatch.setattr(updater, "plugin_dir", lambda: str(dest_plugin_dir))
    existing_settings = dest_plugin_dir / updater.SETTINGS_FILENAME
    existing_settings.write_text(json.dumps({"auto_check_updates": False}))

    zip_bytes = _make_update_zip(extra_files={updater.SETTINGS_FILENAME: json.dumps({"auto_check_updates": True})})
    with patch("audiotimeline.updater.urllib.request.urlopen", return_value=_FakeResponse(zip_bytes)):
        updater.download_and_apply_update("https://example.invalid/fake.zip")

    # The update archive's own settings file must NOT clobber the user's
    # existing local preference.
    assert json.loads(existing_settings.read_text()) == {"auto_check_updates": False}


def test_download_and_apply_update_raises_on_unexpected_archive_layout(tmp_path, monkeypatch):
    dest_plugin_dir = tmp_path / "pykrita" / "audiotimeline"
    dest_plugin_dir.mkdir(parents=True)
    monkeypatch.setattr(updater, "plugin_dir", lambda: str(dest_plugin_dir))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("one/file.txt", "a")
        zf.writestr("two/file.txt", "b")  # two top-level entries -- unexpected layout
    with patch("audiotimeline.updater.urllib.request.urlopen", return_value=_FakeResponse(buf.getvalue())):
        with pytest.raises(RuntimeError):
            updater.download_and_apply_update("https://example.invalid/fake.zip")


def test_download_and_apply_update_raises_if_package_missing(tmp_path, monkeypatch):
    dest_plugin_dir = tmp_path / "pykrita" / "audiotimeline"
    dest_plugin_dir.mkdir(parents=True)
    monkeypatch.setattr(updater, "plugin_dir", lambda: str(dest_plugin_dir))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("repo-v1.0.0/README.md", "no audiotimeline package here")
    with patch("audiotimeline.updater.urllib.request.urlopen", return_value=_FakeResponse(buf.getvalue())):
        with pytest.raises(RuntimeError):
            updater.download_and_apply_update("https://example.invalid/fake.zip")


# -------------------------------------------------------- worker thread shape
def test_update_check_worker_emits_checked_with_release_info(qtbot):
    info = {"tag": "v2.0.0", "version": "2.0.0", "zip_url": "https://example.invalid/x.zip"}
    with patch("audiotimeline.updater.fetch_latest_release_info", return_value=info), \
         patch("audiotimeline.updater.is_update_available", return_value=True):
        worker = updater.UpdateCheckWorker()
        received = []
        worker.checked.connect(lambda payload: received.append(payload))
        worker.run()  # run synchronously in-test rather than via a real thread
    assert received == [info]


def test_update_check_worker_emits_none_when_up_to_date():
    info = {"tag": "v1.0.0", "version": "1.0.0", "zip_url": "https://example.invalid/x.zip"}
    with patch("audiotimeline.updater.fetch_latest_release_info", return_value=info), \
         patch("audiotimeline.updater.is_update_available", return_value=False):
        worker = updater.UpdateCheckWorker()
        received = []
        worker.checked.connect(lambda payload: received.append(payload))
        worker.run()
    assert received == [None]


def test_update_check_worker_emits_failed_on_exception():
    with patch("audiotimeline.updater.fetch_latest_release_info", side_effect=RuntimeError("boom")):
        worker = updater.UpdateCheckWorker()
        failures = []
        worker.failed.connect(lambda msg: failures.append(msg))
        worker.run()
    assert failures == ["boom"]


def test_auto_check_session_flag_only_flips_once(monkeypatch):
    monkeypatch.setattr(updater, "_auto_check_done_this_session", False)
    assert updater.auto_check_already_done_this_session() is False
    updater.mark_auto_check_done_this_session()
    assert updater.auto_check_already_done_this_session() is True
