import json
import os
import tempfile
import uuid

from krita import DockWidget

from PyQt5.QtCore import QTimer, QThread, pyqtSignal, Qt, QSize
from PyQt5.QtGui import QTransform
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QToolButton, QLabel, QScrollArea,
    QFileDialog, QMessageBox, QStyle, QDockWidget, QGraphicsOpacityEffect,
    QUndoGroup, QUndoStack, QFrame
)

SPINNER_SIZE = 16
SPINNER_INTERVAL_MS = 40      # ~25 fps
SPINNER_DEGREES_PER_TICK = 17  # a full turn every ~1.2s at the interval above

from .timeline_widget import (
    AudioTimelineWidget, AudioTimelineRulerWidget, AudioTimelineHeaderWidget,
    AudioTimelineCornerWidget, AudioTimelineHeaderWrapperWidget,
    RULER_HEIGHT, TRACK_HEADER_WIDTH,
)
from .audio_track import AudioTrack, AudioClip
from . import mixdown
from . import commands
from . import updater
from .update_dialog import UpdateDialog
from .settings_dialog import SettingsDialog
from .info_dialog import InfoDialog

POLL_INTERVAL_MS = 40           # ~25 checks/sec; matches typical playback tick rate
ANNOTATION_KEY = "audiotimeline/state"
ANNOTATION_DESC = "Audio Timeline plugin state (tracks/clips)"
# A per-document id, persisted as its own annotation. Krita's Document
# wrapper is not guaranteed to be the same Python object across separate
# activeDocument()/canvasChanged calls for the same open document, so plain
# `is`-identity isn't reliable enough to key session-only per-document state
# (undo stacks, mixdown filenames) off of -- this annotation-backed id is
# stable for as long as the document stays open.
DOC_ID_ANNOTATION_KEY = "audiotimeline/doc_id"
DOC_ID_ANNOTATION_DESC = "Audio Timeline plugin per-document session id"


class _MixdownWorker(QThread):
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


class AudioTimelineDocker(DockWidget):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Audio Timeline")

        self.timeline = AudioTimelineWidget()
        self.timeline.scrubbed.connect(self.on_timeline_scrubbed)
        # Every track/clip mutation goes through timeline.undo_stack (see
        # commands.py) and reports here via contentChanged, whether it's
        # the original action or an undo/redo of it. The annotation is
        # always re-saved, but the (comparatively expensive) mixdown
        # re-render only runs when the mutation actually affects audio.
        self.timeline.contentChanged.connect(self._on_content_changed)

        self._last_doc_frame = None
        self._last_total_frames = None
        self._last_fps = None
        self._loaded_doc_id = None  # _doc_id() of the Document last loaded
        # Each open document gets its own live tracks list + QUndoStack,
        # recorded here as {'doc_id': ..., 'tracks': ..., 'undo_stack': ...,
        # 'active_track_index': ...} dicts -- populated the first time a
        # document becomes active (see _load_state_fresh()) and just
        # restored (not rebuilt) on every switch back to it, so switching
        # documents never discards undo/redo history. Keyed by _doc_id()
        # (a plain list, matched by that id) rather than a dict keyed by
        # the Document object itself, since that's neither hashable nor
        # reliably the same Python object across activations.
        self._doc_states = []
        # Every mixdown path ever handed out by _mixdown_path_for(), so
        # close() can clean all of them up rather than just one.
        self._known_mixdown_paths = set()
        # Only one mixdown render runs at a time; if edits arrive while one
        # is in flight, remember the doc to re-render for once it finishes
        # rather than starting an overlapping render.
        self._mixdown_thread = None
        self._mixdown_pending_doc = None
        # None = not checked yet; True/False once the first real Document
        # tells us whether this Krita build has the setAudioTracks()/
        # audioTracks() API at all (added in Krita 5.3/6.0 -- absent on
        # 5.2.x and earlier). Checked lazily rather than at import time
        # since it needs an actual Document instance to probe.
        self._audio_api_supported = None

        self._build_undo_redo_actions()
        self._build_ui()

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(POLL_INTERVAL_MS)
        self.poll_timer.timeout.connect(self.poll_krita_document)
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
    def _build_undo_redo_actions(self):
        # Ctrl+Z/Ctrl+Y themselves are handled directly by the timeline
        # widget (see its ShortcutOverride/keyPressEvent handling) rather
        # than via QAction shortcuts here -- Krita's own window/canvas
        # already binds those keys, and a widget-scoped QAction shortcut
        # can still be judged ambiguous against that since this docker
        # lives inside Krita's main window. These actions exist for the
        # toolbar buttons' icon/tooltip/enabled-state only.
        #
        # One QUndoStack per open document (see _doc_states), all added to
        # this shared QUndoGroup -- createUndoAction()/createRedoAction()
        # below stay wired to whichever stack is currently active in the
        # group, so switching documents (which calls setActiveStack())
        # just retargets the same toolbar buttons instead of needing them
        # rebound by hand.
        self.undo_group = QUndoGroup(self)
        self.undo_group.addStack(self.timeline.undo_stack)
        self.undo_group.setActiveStack(self.timeline.undo_stack)

        self._undo_action = self.undo_group.createUndoAction(self.timeline, "Undo")
        self._undo_action.setToolTip("Undo (Ctrl+Z)")
        self._undo_action.setIcon(self._krita_icon("edit-undo", QStyle.SP_ArrowBack))

        self._redo_action = self.undo_group.createRedoAction(self.timeline, "Redo")
        self._redo_action.setToolTip("Redo (Ctrl+Y)")
        self._redo_action.setIcon(self._krita_icon("edit-redo", QStyle.SP_ArrowForward))

    def _build_ui(self):
        self.setTitleBarWidget(self._build_title_bar())

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)

        # All four pieces below are borderless -- instead, this one shared
        # frame draws a single continuous border around the whole grid, so
        # there's no seam (nor a way for one edge to visually double up or
        # misalign against another) where any of them meet.
        panel = QFrame()
        panel.setFrameShape(QFrame.StyledPanel)
        panel.setFrameShadow(QFrame.Sunken)
        panel_layout = QGridLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)
        # Only the tracks cell (row 1, col 1) should absorb extra space --
        # the corner/ruler/header cells all stay pinned at their fixed size.
        panel_layout.setColumnStretch(1, 1)
        panel_layout.setRowStretch(1, 1)

        # Laid out like a spreadsheet's frozen row/column:
        #   corner  | ruler   (row 0 -- fixed vertically, scrolls with the
        #                       tracks horizontally)
        #   header  | tracks  (row 1 -- header fixed horizontally, scrolls
        #                       with the tracks vertically; tracks scrolls
        #                       both ways)
        # so the ruler and header column both stay visible regardless of
        # how far the tracks are scrolled in the other axis.
        corner = AudioTimelineCornerWidget(self.timeline)
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(3, 2, 3, 2)
        corner_layout.setSpacing(2)

        add_track_btn = QToolButton()
        # "addlayer" is Krita's own plus-sign icon, same one used for "Add
        # Layer" in the Layers docker.
        add_track_btn.setIcon(self._krita_icon("addlayer", QStyle.SP_FileDialogNewFolder))
        add_track_btn.setToolTip("Add Track")
        add_track_btn.setAutoRaise(True)
        add_track_btn.setFixedSize(RULER_HEIGHT - 4, RULER_HEIGHT - 4)
        add_track_btn.setIconSize(QSize(14, 14))
        add_track_btn.clicked.connect(self.add_track)
        corner_layout.addWidget(add_track_btn)
        corner_layout.addStretch(1)

        import_btn = QToolButton()
        # "document-open" is Krita's own folder icon, used for File > Open.
        import_btn.setIcon(self._krita_icon("document-open", QStyle.SP_DialogOpenButton))
        import_btn.setToolTip("Import Audio…")
        import_btn.setAutoRaise(True)
        import_btn.setFixedSize(RULER_HEIGHT - 4, RULER_HEIGHT - 4)
        import_btn.setIconSize(QSize(14, 14))
        import_btn.clicked.connect(self.import_audio)
        corner_layout.addWidget(import_btn)

        panel_layout.addWidget(corner, 0, 0)

        self.ruler = AudioTimelineRulerWidget(self.timeline)
        ruler_scroll = QScrollArea()
        ruler_scroll.setWidget(self.ruler)
        ruler_scroll.setWidgetResizable(False)
        ruler_scroll.setFixedHeight(RULER_HEIGHT)
        ruler_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ruler_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ruler_scroll.setFrameShape(QFrame.NoFrame)
        self.ruler_scroll_area = ruler_scroll

        self.header = AudioTimelineHeaderWidget(self.timeline)
        header_scroll = QScrollArea()
        header_scroll.setWidget(self.header)
        header_scroll.setWidgetResizable(False)
        header_scroll.setFixedWidth(TRACK_HEADER_WIDTH)
        header_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        header_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        header_scroll.setFrameShape(QFrame.NoFrame)
        self.header_scroll_area = header_scroll

        scroll = QScrollArea()
        scroll.setWidget(self.timeline)
        scroll.setWidgetResizable(False)
        scroll.setFrameShape(QFrame.NoFrame)
        panel_layout.addWidget(scroll, 1, 1)
        self.scroll_area = scroll

        # scroll's own vertical/horizontal scrollbars eat into *its*
        # viewport, but ruler_scroll/header_scroll have no scrollbars of
        # their own -- so for the same outer (row/column) size, their
        # viewports would stay full-size while scroll's shrinks, and ruler
        # ticks/header rows would drift away from the actual track content
        # the moment either scrollbar appears (worse the further you
        # scroll). Wrapping each in a thin layout with a same-size spacer
        # on the scrollbar's side shrinks its *own* viewport to match --
        # the spacer is shown/hidden to track whichever scrollbar it's
        # compensating for.
        scrollbar_extent = self.style().pixelMetric(QStyle.PM_ScrollBarExtent)

        ruler_wrapper = QWidget()
        ruler_wrapper_layout = QHBoxLayout(ruler_wrapper)
        ruler_wrapper_layout.setContentsMargins(0, 0, 0, 0)
        ruler_wrapper_layout.setSpacing(0)
        ruler_wrapper_layout.addWidget(ruler_scroll, 1)
        v_spacer = QWidget()
        v_spacer.setFixedWidth(scrollbar_extent)
        ruler_wrapper_layout.addWidget(v_spacer)
        panel_layout.addWidget(ruler_wrapper, 0, 1)

        header_wrapper = AudioTimelineHeaderWrapperWidget(self.timeline)
        header_wrapper_layout = QVBoxLayout(header_wrapper)
        header_wrapper_layout.setContentsMargins(0, 0, 0, 0)
        header_wrapper_layout.setSpacing(0)
        header_wrapper_layout.addWidget(header_scroll, 1)
        h_spacer = QWidget()
        h_spacer.setFixedHeight(scrollbar_extent)
        header_wrapper_layout.addWidget(h_spacer)
        panel_layout.addWidget(header_wrapper, 1, 0)

        def _sync_gutters(*_args):
            v_spacer.setVisible(scroll.verticalScrollBar().isVisible())
            h_spacer.setVisible(scroll.horizontalScrollBar().isVisible())

        scroll.verticalScrollBar().rangeChanged.connect(_sync_gutters)
        scroll.horizontalScrollBar().rangeChanged.connect(_sync_gutters)
        self._sync_scrollbar_gutters = _sync_gutters
        _sync_gutters()

        layout.addWidget(panel)

        # Synced both ways -- each scrollbar is hidden on its own side
        # (AlwaysOff) so the user never drags them directly, but a mouse
        # wheel scroll with the cursor over the ruler/header still moves
        # that widget's own (hidden) scrollbar, which would desync from
        # the tracks' otherwise. Each QScrollBar.setValue() is a no-op
        # once already equal, so this can't loop.
        scroll.horizontalScrollBar().valueChanged.connect(
            ruler_scroll.horizontalScrollBar().setValue)
        ruler_scroll.horizontalScrollBar().valueChanged.connect(
            scroll.horizontalScrollBar().setValue)
        # Clip names are pinned to the left edge of the visible viewport
        # (see AudioTimelineWidget._paint_clip), so their draw position
        # depends on scroll offset -- but Qt's default scroll optimization
        # blits the widget's existing backing-store pixels and only
        # repaints the newly-exposed sliver, leaving stale copies of the
        # label at its previous position. Forcing a full repaint on every
        # scroll step keeps just one, correctly-positioned copy on screen.
        scroll.horizontalScrollBar().valueChanged.connect(lambda _v: self.timeline.update())
        scroll.verticalScrollBar().valueChanged.connect(
            header_scroll.verticalScrollBar().setValue)
        header_scroll.verticalScrollBar().valueChanged.connect(
            scroll.verticalScrollBar().setValue)
        self.timeline.layoutChanged.connect(self._sync_ruler_geometry)
        self.timeline.layoutChanged.connect(self._sync_header_geometry)
        self.timeline.layoutChanged.connect(self._sync_scrollbar_gutters)
        self.timeline.frameChanged.connect(self.ruler.update)
        self.timeline.activeTrackChanged.connect(self.header.update)
        # Mute/delete/rename act on the header widget but mutate shared
        # timeline state through contentChanged -- repaint it (icon swap,
        # name edit) whenever any of that fires, undo/redo included.
        self.timeline.contentChanged.connect(lambda _affects_audio: self.header.update())

        self.setWidget(container)
        self.add_track()  # start with one track so the docker isn't empty

    def _sync_ruler_geometry(self):
        self.ruler.resize(self.ruler.sizeHint())
        self.ruler.update()

    def _sync_header_geometry(self):
        self.header.resize(self.header.sizeHint())
        self.header.update()

    def _build_title_bar(self):
        # Mirrors the layout of Krita's own built-in docker title bars:
        # lock toggle on the far left, title + panel actions next to it,
        # then a stretch, then the float/close controls on the far right.
        title_bar = QWidget()
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(4, 2, 4, 2)
        title_layout.setSpacing(2)

        self._lock_btn = QToolButton()
        self._lock_btn.setCheckable(True)
        self._lock_btn.setAutoRaise(True)
        self._lock_btn.setToolTip("Lock Docker")
        self._lock_btn.setIcon(self._krita_icon("docker_lock_a", QStyle.SP_DialogYesButton))
        self._lock_btn.toggled.connect(self._on_lock_toggled)
        title_layout.addWidget(self._lock_btn)

        title_label = QLabel("Audio Timeline")
        title_layout.addWidget(title_label)
        title_layout.addSpacing(8)

        self._mixdown_spinner = QLabel()
        self._mixdown_spinner.setFixedSize(SPINNER_SIZE, SPINNER_SIZE)
        self._mixdown_spinner.setToolTip("Buffering (mixing down audio)…")
        self._mixdown_spinner.setVisible(False)
        self._mixdown_spinner_pixmap = self._krita_icon(
            "selectionMask", QStyle.SP_BrowserReload
        ).pixmap(SPINNER_SIZE, SPINNER_SIZE)
        title_layout.addWidget(self._mixdown_spinner)

        title_layout.addStretch(1)

        self._info_btn = QToolButton()
        self._info_btn.setAutoRaise(True)
        self._info_btn.setToolTip("Feature List")
        self._info_btn.setIcon(self._krita_icon("system-help", QStyle.SP_MessageBoxInformation))
        self._info_btn.clicked.connect(self._open_info_dialog)
        title_layout.addWidget(self._info_btn)

        self._settings_btn = QToolButton()
        self._settings_btn.setAutoRaise(True)
        self._settings_btn.setToolTip("Audio Timeline Settings")
        self._settings_btn.setIcon(self._krita_icon("configure", QStyle.SP_FileDialogDetailedView))
        self._settings_btn.clicked.connect(self._open_settings_dialog)
        title_layout.addWidget(self._settings_btn)

        self._split_btn = QToolButton()
        self._split_btn.setAutoRaise(True)
        self._split_btn.setToolTip("Split Selected Clip at Playhead (S)")
        # "edit-cut" is Krita's own scissors glyph (used for Edit > Cut).
        self._split_btn.setIcon(self._krita_icon("edit-cut", QStyle.SP_DialogResetButton))
        can_split = self.timeline.can_split_selected_clip()
        self._split_btn.setEnabled(can_split)
        self._split_btn.clicked.connect(self.timeline.split_selected_clip)
        self._split_btn_opacity = QGraphicsOpacityEffect(self._split_btn)
        self._split_btn_opacity.setOpacity(1.0 if can_split else 0.35)
        self._split_btn.setGraphicsEffect(self._split_btn_opacity)
        self.timeline.selectionChanged.connect(self._update_split_btn_enabled)
        self.timeline.frameChanged.connect(self._update_split_btn_enabled)
        title_layout.addWidget(self._split_btn)

        undo_btn = QToolButton()
        undo_btn.setAutoRaise(True)
        undo_btn.setDefaultAction(self._undo_action)
        self._dim_when_disabled(undo_btn, self.undo_group.canUndoChanged, self.undo_group.canUndo())
        title_layout.addWidget(undo_btn)

        redo_btn = QToolButton()
        redo_btn.setAutoRaise(True)
        redo_btn.setDefaultAction(self._redo_action)
        self._dim_when_disabled(redo_btn, self.undo_group.canRedoChanged, self.undo_group.canRedo())
        title_layout.addWidget(redo_btn)

        float_btn = QToolButton()
        float_btn.setAutoRaise(True)
        float_btn.setToolTip("Float Docker")
        # Krita's own icon set has no bundled float/restore glyph for the
        # docker title bar (it's drawn from the OS/Qt style, not koIcon) --
        # "view-fullscreen" is the closest themed stand-in.
        float_btn.setIcon(self._krita_icon("view-fullscreen", QStyle.SP_TitleBarNormalButton))
        float_btn.clicked.connect(lambda: self.setFloating(not self.isFloating()))
        title_layout.addWidget(float_btn)

        close_btn = QToolButton()
        close_btn.setAutoRaise(True)
        close_btn.setToolTip("Close Docker")
        close_btn.setIcon(self._krita_icon("window-close", QStyle.SP_TitleBarCloseButton))
        close_btn.clicked.connect(self.close)
        title_layout.addWidget(close_btn)

        return title_bar

    def _on_lock_toggled(self, checked):
        self._lock_btn.setIcon(self._krita_icon(
            "docker_lock_b" if checked else "docker_lock_a",
            QStyle.SP_DialogYesButton,
        ))
        features = QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetFloatable
        if not checked:
            features |= QDockWidget.DockWidgetMovable
        self.setFeatures(features)

    def _update_split_btn_enabled(self, *_args):
        # Re-evaluated on both selection and playhead changes -- the split
        # button needs the playhead strictly inside the selected clip, not
        # just a clip being selected (see can_split_selected_clip).
        enabled = self.timeline.can_split_selected_clip()
        self._split_btn.setEnabled(enabled)
        self._split_btn_opacity.setOpacity(1.0 if enabled else 0.35)

    def _dim_when_disabled(self, button, enabled_changed_signal, initially_enabled):
        # QToolButton's built-in disabled rendering depends on the icon
        # having a distinct QIcon::Disabled pixmap -- Krita's themed icons
        # don't reliably provide one, so the button looks identical
        # whether it's clickable or not. Fade it manually instead.
        effect = QGraphicsOpacityEffect(button)
        effect.setOpacity(1.0 if initially_enabled else 0.35)
        button.setGraphicsEffect(effect)
        enabled_changed_signal.connect(lambda enabled: effect.setOpacity(1.0 if enabled else 0.35))

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

    def _krita_icon(self, name, fallback_standard_icon):
        try:
            from krita import Krita
            icon = Krita.instance().icon(name)
            if not icon.isNull():
                return icon
        except Exception:
            pass
        return self.style().standardIcon(fallback_standard_icon)

    # --------------------------------------------------------------- krita
    def canvasChanged(self, canvas):
        # Required override. Called when the active document/view changes.
        doc = self._active_document()
        if doc is not None:
            self._last_fps = doc.framesPerSecond()
            self._last_total_frames = doc.fullClipRangeEndTime()
            self.timeline.set_fps(self._last_fps)
            self.timeline.set_total_frames(self._last_total_frames)
            doc_id = self._doc_id(doc)
            if doc_id != self._loaded_doc_id:
                self._sync_active_doc_state()
                self._load_state(doc)
                self._loaded_doc_id = doc_id

    def _active_document(self):
        from krita import Krita
        return Krita.instance().activeDocument()

    def _doc_id(self, doc):
        """A stable id for `doc`, persisted as an annotation on the
        document itself -- generated the first time it's needed. See the
        DOC_ID_ANNOTATION_KEY comment for why this exists instead of just
        comparing/keying off the Document object."""
        try:
            raw = doc.annotation(DOC_ID_ANNOTATION_KEY)
        except Exception:
            raw = None
        doc_id = bytes(raw).decode("utf-8") if raw else ""
        if not doc_id:
            doc_id = uuid.uuid4().hex
            try:
                doc.setAnnotation(DOC_ID_ANNOTATION_KEY, DOC_ID_ANNOTATION_DESC, doc_id.encode("utf-8"))
            except Exception:
                pass  # best-effort -- worst case this doc looks "new" every time it's activated
        return doc_id

    def _find_doc_state(self, doc):
        doc_id = self._doc_id(doc)
        for state in self._doc_states:
            if state['doc_id'] == doc_id:
                return state
        return None

    def _sync_active_doc_state(self):
        """Writes back whatever's changed on the timeline widget (right
        now, just active_track_index -- tracks and the undo stack are
        already the same objects stored in _doc_states, mutated in place)
        before switching away from the previously-active document."""
        if self._loaded_doc_id is None:
            return
        for state in self._doc_states:
            if state['doc_id'] == self._loaded_doc_id:
                state['active_track_index'] = self.timeline.active_track_index
                break

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

        doc = self._active_document()
        fps = doc.framesPerSecond() if doc else self.timeline.fps
        start_frame = self.timeline.current_frame

        try:
            clip = AudioClip(path, start_frame, fps)
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

    # --------------------------------------------------------------- mixdown
    def _on_content_changed(self, affects_audio):
        self._save_state()
        if not affects_audio:
            return
        doc = self._active_document()
        if doc is not None:
            self._render_and_apply_mixdown(doc)

    def _mixdown_path_for(self, doc):
        """Every document gets its own mixdown wav, named after _doc_id().
        Keeping the file per-document (rather than one shared file for
        every open document) is what makes it safe to skip a re-render in
        _mixdown_already_attached(): a document's file is only ever
        written to by that same document's own edits."""
        path = os.path.join(tempfile.gettempdir(), f"audiotimeline_mixdown_{self._doc_id(doc)}.wav")
        self._known_mixdown_paths.add(path)
        return path

    def _audio_api_available(self, doc):
        """Checks (once, caching the result) whether this Krita build
        exposes Document.setAudioTracks()/audioTracks() at all -- that API
        was only added in Krita 5.3/6.0, so it's simply missing on 5.2.x
        and earlier. Rather than let every call site hit a raw
        AttributeError, warn about it once up front and have callers skip
        the Krita-side attach/clear from then on; the mixdown WAV is still
        rendered to disk regardless, just not handed to Krita automatically."""
        if self._audio_api_supported is None:
            self._audio_api_supported = hasattr(doc, "setAudioTracks") and hasattr(doc, "audioTracks")
            if not self._audio_api_supported:
                QMessageBox.warning(
                    None, "Audio Timeline",
                    "This Krita build doesn't support Document.setAudioTracks() / "
                    "audioTracks() -- that API was only added in Krita 5.3 / 6.0. "
                    "You appear to be on an older build (e.g. 5.2.x).\n\n"
                    "Audio Timeline will still let you arrange clips and will keep "
                    "rendering the mixed-down WAV to your temp folder, but it can't "
                    "hand that audio to Krita's native playback engine on this "
                    "version -- update Krita to 5.3.2+ or 6.0+ for that, or import "
                    "the rendered WAV manually via Krita's own \"Import Audio for "
                    "Animation\"."
                )
        return self._audio_api_supported

    def _mixdown_already_attached(self, doc):
        """True if Krita's built-in Document.audioTracks() already has
        something set for this doc -- whether that's our own mixdown from
        an earlier render or anything else. Just switching to (or opening)
        a document shouldn't cost a re-render when it already has audio
        attached; a real edit (_on_content_changed) always re-renders
        regardless of this check."""
        if not self._audio_api_available(doc):
            return False
        try:
            current = doc.audioTracks()
        except Exception:
            return False
        return bool(current)

    def _render_and_apply_mixdown(self, doc):
        if not any(t.clips for t in self.timeline.tracks):
            # Nothing left to mix down (e.g. the last clip was just
            # deleted/undone) -- clear whatever mixdown Krita still has
            # applied rather than leaving stale audio attached to the doc.
            self._mixdown_pending_doc = None
            self._clear_krita_audio(doc)
            return

        if self._mixdown_thread is not None and self._mixdown_thread.isRunning():
            # A render is already in flight for an earlier edit -- rather
            # than start a second one racing it (or block here until it's
            # done), just remember to re-render once it finishes so the
            # result reflects this latest edit too.
            self._mixdown_pending_doc = doc
            return

        mixdown_path = self._mixdown_path_for(doc)
        snapshot = mixdown.snapshot_tracks(self.timeline.tracks)
        self._set_mixdown_busy(True)

        thread = _MixdownWorker(
            snapshot, self.timeline.fps, self.timeline.total_frames, mixdown_path, self,
        )
        thread.succeeded.connect(lambda path, d=doc: self._on_mixdown_succeeded(d, path))
        thread.failed.connect(self._on_mixdown_failed)
        self._mixdown_thread = thread
        thread.start()

    def _on_mixdown_succeeded(self, doc, path):
        self._set_mixdown_busy(False)
        self._apply_mixdown_to_krita(doc, path)
        self._start_pending_mixdown_if_any()

    def _on_mixdown_failed(self, message):
        self._set_mixdown_busy(False)
        QMessageBox.warning(None, "Audio Timeline", f"Could not render mixdown: {message}")
        self._start_pending_mixdown_if_any()

    def _start_pending_mixdown_if_any(self):
        doc = self._mixdown_pending_doc
        self._mixdown_pending_doc = None
        if doc is not None:
            self._render_and_apply_mixdown(doc)

    def _apply_mixdown_to_krita(self, doc, path):
        if not self._audio_api_available(doc):
            return
        try:
            doc.setAudioTracks([path])
            if doc.audioLevel() <= 0.0:
                doc.setAudioLevel(1.0)
        except Exception as exc:
            QMessageBox.warning(
                None, "Audio Timeline",
                "Krita's Document.setAudioTracks() call failed "
                f"({exc}). The mixed-down audio was still rendered to:\n"
                f"{path}\nyou can load it manually via "
                "Krita's own audio-for-animation import."
            )

    def _clear_krita_audio(self, doc):
        if not self._audio_api_available(doc):
            return
        try:
            doc.setAudioTracks([])
        except Exception as exc:
            QMessageBox.warning(None, "Audio Timeline", f"Could not clear Krita's audio track: {exc}")

    # ------------------------------------------------------------ persistence
    def _save_state(self):
        doc = self._active_document()
        if doc is None:
            return
        data = {
            "tracks": [
                {
                    "name": track.name,
                    "muted": track.muted,
                    "clips": [
                        {
                            "file_path": clip.file_path,
                            "start_frame": clip.start_frame,
                            "fps": clip.fps,
                            "source_duration_sec": clip.source_duration_sec,
                            "trim_in_sec": clip.trim_in_sec,
                            "trim_out_sec": clip.trim_out_sec,
                            "trim_in_floor_sec": clip.trim_in_floor_sec,
                            "volume_points": [list(p) for p in clip.volume_points],
                        }
                        for clip in track.clips
                    ],
                }
                for track in self.timeline.tracks
            ]
        }
        payload = json.dumps(data).encode("utf-8")
        try:
            doc.setAnnotation(ANNOTATION_KEY, ANNOTATION_DESC, payload)
        except Exception:
            pass  # best-effort; a save failure here shouldn't block editing

    def _load_state(self, doc):
        state = self._find_doc_state(doc)
        if state is not None:
            # Already visited this document earlier in this Krita session
            # -- restore its live tracks/undo stack rather than re-parsing
            # the saved annotation, so switching back to it doesn't lose
            # undo/redo history built up on an earlier visit.
            self.timeline.tracks = state['tracks']
            self.timeline.undo_stack = state['undo_stack']
            self.timeline.active_track_index = state['active_track_index']
            self.timeline.selected_clip = None
            self.undo_group.setActiveStack(state['undo_stack'])
            self.timeline.refresh_layout()
        else:
            self._load_state_fresh(doc)

        if self._mixdown_already_attached(doc):
            # Switching to this document, not editing it -- its own mixdown
            # is already sitting on Document.audioTracks() and up to date.
            return

        self._render_and_apply_mixdown(doc)

    def _load_state_fresh(self, doc):
        """Parses this document's saved annotation (if any) into a brand
        new tracks list + undo stack. Only runs the first time a document
        becomes active in this session -- see _load_state()."""
        try:
            raw = doc.annotation(ANNOTATION_KEY)
        except Exception:
            raw = None

        # A document with no saved annotation (e.g. brand new, or one that's
        # never had audio tracks) still needs the timeline reset below --
        # otherwise switching to it would just leave the previous
        # document's tracks/clips on screen.
        data = {}
        if raw:
            try:
                data = json.loads(bytes(raw).decode("utf-8"))
            except Exception:
                data = {}

        stack = QUndoStack(self)
        self.undo_group.addStack(stack)
        self.undo_group.setActiveStack(stack)

        self.timeline.tracks = []
        self.timeline.selected_clip = None
        self.timeline.active_track_index = 0
        self.timeline.undo_stack = stack
        for track_dict in data.get("tracks", []):
            track = AudioTrack(name=track_dict.get("name", "Track"))
            track.muted = bool(track_dict.get("muted", False))
            for clip_dict in track_dict.get("clips", []):
                path = clip_dict.get("file_path")
                if not path or not os.path.exists(path):
                    # Clip references a file that's since moved/been deleted
                    # -- skip it rather than crash the whole load, same
                    # path-based-reference caveat as fresh imports.
                    continue
                try:
                    clip = AudioClip(
                        path, clip_dict.get("start_frame", 0),
                        clip_dict.get("fps", self.timeline.fps),
                    )
                except Exception:
                    continue
                # source_duration_sec may have been capped by a prior split
                # (see AudioClip.clone_for_split) -- restore that saved
                # ceiling rather than trusting the freshly re-decoded full
                # file duration, so a reloaded split clip still can't be
                # trimmed back out past its cut point.
                if "source_duration_sec" in clip_dict:
                    clip.source_duration_sec = float(clip_dict["source_duration_sec"])
                clip.trim_in_sec = float(clip_dict.get("trim_in_sec", 0.0))
                clip.trim_out_sec = float(clip_dict.get("trim_out_sec", 0.0))
                clip.trim_in_floor_sec = float(clip_dict.get("trim_in_floor_sec", 0.0))
                volume_points = clip_dict.get("volume_points")
                if volume_points:
                    clip.volume_points = [tuple(p) for p in volume_points]
                track.add_clip(clip)
            self.timeline.add_track(track)

        if not self.timeline.tracks:
            self.add_track()

        # The fallback empty track above (or whatever was just parsed) is
        # pre-existing document state, not a fresh user edit -- it
        # shouldn't be undoable back to an empty timeline. Safe to clear
        # unconditionally here since this stack was only just created.
        stack.clear()

        self._doc_states.append({
            'doc_id': self._doc_id(doc),
            'tracks': self.timeline.tracks,
            'undo_stack': stack,
            'active_track_index': self.timeline.active_track_index,
        })

    # ------------------------------------------------------- sync (poll)
    def poll_krita_document(self):
        doc = self._active_document()
        if doc is None:
            return

        frame = doc.currentTime()
        frame_changed = frame != self._last_doc_frame
        if frame_changed:
            previous_frame = self._last_doc_frame
            self._last_doc_frame = frame
            self.timeline.set_current_frame(frame, emit=False)
            moving_forward = previous_frame is None or frame >= previous_frame
            self._ensure_playhead_visible(moving_forward)

        # canvasChanged only fires on view/document switches, not when the
        # user edits the animation range on Krita's own timeline -- poll
        # for that too so it doesn't take a reopen to notice.
        total_frames = doc.fullClipRangeEndTime()
        if total_frames != self._last_total_frames:
            self._last_total_frames = total_frames
            self.timeline.set_total_frames(total_frames)

        fps = doc.framesPerSecond()
        if fps != self._last_fps:
            self._last_fps = fps
            self.timeline.set_fps(fps)

    def _ensure_playhead_visible(self, moving_forward):
        """Auto-scrolls the docker's QScrollArea horizontally so the
        playhead stays on screen during Krita playback -- otherwise it
        would just run off the edge of whatever's currently scrolled into
        view with nothing following it.

        Landing spot depends on playback direction so the user always sees
        the playhead sweep across the viewport rather than snapping to the
        same edge it just vanished past: moving forward, it reappears at
        the *left* edge (revealing upcoming waveform to the right); moving
        backward, it reappears at the *right* edge (revealing upcoming
        waveform to the left).
        """
        scroll_area = getattr(self, 'scroll_area', None)
        if scroll_area is None:
            return
        hbar = scroll_area.horizontalScrollBar()
        viewport_width = scroll_area.viewport().width()
        if viewport_width <= 0:
            return

        x = self.timeline.frame_to_x(self.timeline.current_frame)
        left = hbar.value()
        right = left + viewport_width
        if left <= x <= right:
            return
        new_left = x if moving_forward else x - viewport_width
        hbar.setValue(max(0, new_left))

    def on_timeline_scrubbed(self, frame):
        doc = self._active_document()
        if doc is not None:
            doc.setCurrentTime(frame)
            self._last_doc_frame = frame

    # ------------------------------------------------------------- cleanup
    def close(self):
        self.poll_timer.stop()
        self._mixdown_spinner_timer.stop()
        if self._mixdown_thread is not None and self._mixdown_thread.isRunning():
            self._mixdown_thread.wait(2000)
        for path in self._known_mixdown_paths:
            try:
                os.remove(path)
            except OSError:
                pass
        super().close()
