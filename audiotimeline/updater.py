"""Update checking and self-update for the Audio Timeline plugin.

Plain functions plus one pair of QThread workers -- no Qt widgets here, so
it's usable from both the docker toolbar and the settings dialog.
"""

import json
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile

from PyQt5.QtCore import QThread, pyqtSignal

GITHUB_REPO = "romeomihalovics/krita-audio-timeline-plugin"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
REQUEST_TIMEOUT_SEC = 10

META_FILENAME = "plugin_meta.json"
SETTINGS_FILENAME = "update_settings.json"
DESKTOP_FILENAME = "audiotimeline.desktop"

# Only run the automatic startup check once per Krita session, even though
# multiple documents can each instantiate their own docker instance.
_auto_check_done_this_session = False


def plugin_dir():
    """The on-disk directory this running plugin package lives in, e.g.
    .../pykrita/audiotimeline/ -- derived from __file__ so it's correct
    regardless of OS or how Krita was installed."""
    return os.path.dirname(os.path.abspath(__file__))


def current_version():
    path = os.path.join(plugin_dir(), META_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("version", "0.0.0"))
    except Exception:
        return "0.0.0"


def load_update_settings():
    path = os.path.join(plugin_dir(), SETTINGS_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"auto_check_updates": bool(data.get("auto_check_updates", True))}
    except Exception:
        return {"auto_check_updates": True}


def save_update_settings(settings):
    path = os.path.join(plugin_dir(), SETTINGS_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


def parse_version(v):
    """"1.4.0" -> (1, 4, 0), for tuple comparison; tolerant of a leading
    "v" and of non-numeric trailing components."""
    v = v.strip()
    if v.startswith("v") or v.startswith("V"):
        v = v[1:]
    parts = []
    for chunk in v.split("."):
        match = re.match(r"\d+", chunk)
        parts.append(int(match.group()) if match else 0)
    return tuple(parts)


def fetch_latest_release_info():
    """Blocking network call to RELEASES_API_URL. Returns
    {'tag': 'v1.5.0', 'version': '1.5.0',
    'zip_url': 'https://github.com/.../archive/refs/tags/v1.5.0.zip'}.
    Raises on network/HTTP error -- callers decide how to surface it."""
    request = urllib.request.Request(
        RELEASES_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "audiotimeline-plugin"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
        data = json.loads(response.read().decode("utf-8"))
    tag = data["tag_name"]
    version = tag[1:] if tag[:1] in ("v", "V") else tag
    zip_url = f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{tag}.zip"
    return {"tag": tag, "version": version, "zip_url": zip_url}


def is_update_available(latest_version):
    return parse_version(latest_version) > parse_version(current_version())


def download_and_apply_update(zip_url):
    """Blocking. Downloads the tag's source zip, extracts it to a temp
    dir, locates the audiotimeline/ package + .desktop file inside it (the
    archive root is `<repo>-<tag>/`), then copies files over plugin_dir()
    and its sibling .desktop file, deliberately skipping
    update_settings.json if it already exists on disk. Raises on failure."""
    tmp_dir = tempfile.mkdtemp(prefix="audiotimeline_update_")
    try:
        zip_path = os.path.join(tmp_dir, "update.zip")
        request = urllib.request.Request(zip_url, headers={"User-Agent": "audiotimeline-plugin"})
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
            with open(zip_path, "wb") as f:
                shutil.copyfileobj(response, f)

        extract_dir = os.path.join(tmp_dir, "extracted")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        entries = os.listdir(extract_dir)
        if len(entries) != 1:
            raise RuntimeError(f"Unexpected archive layout: {entries}")
        archive_root = os.path.join(extract_dir, entries[0])

        src_package_dir = os.path.join(archive_root, "audiotimeline")
        if not os.path.isdir(src_package_dir):
            raise RuntimeError("Downloaded archive is missing the audiotimeline/ package")

        dest_package_dir = plugin_dir()
        _copy_tree_skip_settings(src_package_dir, dest_package_dir)

        src_desktop = os.path.join(archive_root, DESKTOP_FILENAME)
        if os.path.isfile(src_desktop):
            dest_desktop = os.path.join(os.path.dirname(dest_package_dir), DESKTOP_FILENAME)
            shutil.copy2(src_desktop, dest_desktop)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _copy_tree_skip_settings(src_dir, dest_dir):
    """Copies every file under src_dir over the corresponding path in
    dest_dir, except update_settings.json is skipped entirely if it
    already exists at the destination -- so an existing user preference
    (e.g. auto_check_updates: false) is never clobbered by an update.
    __pycache__ is left alone; Python recompiles on next import anyway."""
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel_dir = os.path.relpath(root, src_dir)
        dest_root = dest_dir if rel_dir == "." else os.path.join(dest_dir, rel_dir)
        os.makedirs(dest_root, exist_ok=True)
        for filename in files:
            dest_path = os.path.join(dest_root, filename)
            if filename == SETTINGS_FILENAME and os.path.exists(dest_path):
                continue
            shutil.copy2(os.path.join(root, filename), dest_path)


class UpdateCheckWorker(QThread):
    """Mirrors MixdownWorker's shape: calls fetch_latest_release_info() +
    is_update_available() off the UI thread, emits checked(dict_or_None)
    on success (None release info if already up to date, for a uniform
    "no update" signal) or failed(str) on exception."""

    checked = pyqtSignal(object)  # dict or None
    failed = pyqtSignal(str)

    def run(self):
        try:
            info = fetch_latest_release_info()
            if is_update_available(info["version"]):
                self.checked.emit(info)
            else:
                self.checked.emit(None)
        except Exception as exc:
            self.failed.emit(str(exc))


class UpdateApplyWorker(QThread):
    succeeded = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, zip_url, parent=None):
        super().__init__(parent)
        self._zip_url = zip_url

    def run(self):
        try:
            download_and_apply_update(self._zip_url)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit()


def auto_check_already_done_this_session():
    return _auto_check_done_this_session


def mark_auto_check_done_this_session():
    global _auto_check_done_this_session
    _auto_check_done_this_session = True
