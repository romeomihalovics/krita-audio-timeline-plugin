"""Docker chrome construction: the frozen-row/column track grid (corner,
ruler, header, tracks), the title bar (lock/info/settings/split/undo/redo/
float/close buttons), and the small widget-dimming helpers those buttons
share. Pure layout/wiring -- every function here takes the owning
AudioTimelineDocker and attaches widgets/state onto it, the same shapes
`_build_ui`/`_build_title_bar`/etc. used before this was split out."""

from PyQt5.QtCore import Qt, QSize
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QToolButton, QLabel, QScrollArea,
    QStyle, QDockWidget, QGraphicsOpacityEffect, QUndoGroup, QFrame,
)

from .timeline_widget import (
    AudioTimelineCornerWidget, AudioTimelineRulerWidget, AudioTimelineHeaderWidget,
    AudioTimelineHeaderWrapperWidget, RULER_HEIGHT, TRACK_HEADER_WIDTH,
)
from .timeline_icons import krita_icon

SPINNER_SIZE = 16
SPINNER_INTERVAL_MS = 40      # ~25 fps
SPINNER_DEGREES_PER_TICK = 17  # a full turn every ~1.2s at the interval above


def make_tool_button(icon_name, fallback, tooltip, handler, size=None, icon_size=None, autoraise=True):
    """Builds a QToolButton with the icon/tooltip/click-handler shape
    repeated for every toolbar/title-bar button (add track, import, info,
    settings, split, float, close, ...) -- only what varies per button
    needs to be passed in."""
    btn = QToolButton()
    btn.setIcon(krita_icon(icon_name, fallback))
    if tooltip:
        btn.setToolTip(tooltip)
    btn.setAutoRaise(autoraise)
    if size is not None:
        btn.setFixedSize(size, size)
    if icon_size is not None:
        btn.setIconSize(QSize(icon_size, icon_size))
    if handler is not None:
        btn.clicked.connect(handler)
    return btn


def dim_when_disabled(button, enabled_changed_signal, initially_enabled):
    # QToolButton's built-in disabled rendering depends on the icon
    # having a distinct QIcon::Disabled pixmap -- Krita's themed icons
    # don't reliably provide one, so the button looks identical
    # whether it's clickable or not. Fade it manually instead.
    effect = QGraphicsOpacityEffect(button)
    effect.setOpacity(1.0 if initially_enabled else 0.35)
    button.setGraphicsEffect(effect)
    enabled_changed_signal.connect(lambda enabled: effect.setOpacity(1.0 if enabled else 0.35))


def build_undo_redo_actions(docker):
    # Ctrl+Z/Ctrl+Y themselves are handled directly by the timeline
    # widget (see its ShortcutOverride/keyPressEvent handling) rather
    # than via QAction shortcuts here -- Krita's own window/canvas
    # already binds those keys, and a widget-scoped QAction shortcut
    # can still be judged ambiguous against that since this docker
    # lives inside Krita's main window. These actions exist for the
    # toolbar buttons' icon/tooltip/enabled-state only.
    #
    # One QUndoStack per open document (see state_store), all added to
    # this shared QUndoGroup -- createUndoAction()/createRedoAction()
    # below stay wired to whichever stack is currently active in the
    # group, so switching documents (which calls setActiveStack())
    # just retargets the same toolbar buttons instead of needing them
    # rebound by hand.
    docker.undo_group = QUndoGroup(docker)
    docker.undo_group.addStack(docker.timeline.undo_stack)
    docker.undo_group.setActiveStack(docker.timeline.undo_stack)

    docker._undo_action = docker.undo_group.createUndoAction(docker.timeline, "Undo")
    docker._undo_action.setToolTip("Undo (Ctrl+Z)")
    docker._undo_action.setIcon(krita_icon("edit-undo", QStyle.SP_ArrowBack))

    docker._redo_action = docker.undo_group.createRedoAction(docker.timeline, "Redo")
    docker._redo_action.setToolTip("Redo (Ctrl+Y)")
    docker._redo_action.setIcon(krita_icon("edit-redo", QStyle.SP_ArrowForward))


def build_ui(docker):
    docker.setTitleBarWidget(build_title_bar(docker))

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
    corner = AudioTimelineCornerWidget(docker.timeline)
    corner_layout = QHBoxLayout(corner)
    corner_layout.setContentsMargins(3, 2, 3, 2)
    corner_layout.setSpacing(2)

    # "addlayer" is Krita's own plus-sign icon, same one used for "Add
    # Layer" in the Layers docker.
    add_track_btn = make_tool_button(
        "addlayer", QStyle.SP_FileDialogNewFolder, "Add Track", docker.add_track,
        size=RULER_HEIGHT - 4, icon_size=14,
    )
    corner_layout.addWidget(add_track_btn)
    corner_layout.addStretch(1)

    # "document-open" is Krita's own folder icon, used for File > Open.
    import_btn = make_tool_button(
        "document-open", QStyle.SP_DialogOpenButton, "Import Audio…", docker.import_audio,
        size=RULER_HEIGHT - 4, icon_size=14,
    )
    corner_layout.addWidget(import_btn)

    panel_layout.addWidget(corner, 0, 0)

    docker.ruler = AudioTimelineRulerWidget(docker.timeline)
    ruler_scroll = QScrollArea()
    ruler_scroll.setWidget(docker.ruler)
    ruler_scroll.setWidgetResizable(False)
    ruler_scroll.setFixedHeight(RULER_HEIGHT)
    ruler_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    ruler_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    ruler_scroll.setFrameShape(QFrame.NoFrame)
    docker.ruler_scroll_area = ruler_scroll

    docker.header = AudioTimelineHeaderWidget(docker.timeline)
    header_scroll = QScrollArea()
    header_scroll.setWidget(docker.header)
    header_scroll.setWidgetResizable(False)
    header_scroll.setFixedWidth(TRACK_HEADER_WIDTH)
    header_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    header_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    header_scroll.setFrameShape(QFrame.NoFrame)
    docker.header_scroll_area = header_scroll

    scroll = QScrollArea()
    scroll.setWidget(docker.timeline)
    scroll.setWidgetResizable(False)
    scroll.setFrameShape(QFrame.NoFrame)
    panel_layout.addWidget(scroll, 1, 1)
    docker.scroll_area = scroll

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
    scrollbar_extent = docker.style().pixelMetric(QStyle.PM_ScrollBarExtent)

    ruler_wrapper = QWidget()
    ruler_wrapper_layout = QHBoxLayout(ruler_wrapper)
    ruler_wrapper_layout.setContentsMargins(0, 0, 0, 0)
    ruler_wrapper_layout.setSpacing(0)
    ruler_wrapper_layout.addWidget(ruler_scroll, 1)
    v_spacer = QWidget()
    v_spacer.setFixedWidth(scrollbar_extent)
    ruler_wrapper_layout.addWidget(v_spacer)
    panel_layout.addWidget(ruler_wrapper, 0, 1)

    header_wrapper = AudioTimelineHeaderWrapperWidget(docker.timeline)
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
    docker._sync_scrollbar_gutters = _sync_gutters
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
    scroll.horizontalScrollBar().valueChanged.connect(lambda _v: docker.timeline.update())
    scroll.verticalScrollBar().valueChanged.connect(
        header_scroll.verticalScrollBar().setValue)
    header_scroll.verticalScrollBar().valueChanged.connect(
        scroll.verticalScrollBar().setValue)
    docker.timeline.layoutChanged.connect(lambda: sync_ruler_geometry(docker))
    docker.timeline.layoutChanged.connect(lambda: sync_header_geometry(docker))
    docker.timeline.layoutChanged.connect(docker._sync_scrollbar_gutters)
    docker.timeline.frameChanged.connect(docker.ruler.update)
    docker.timeline.activeTrackChanged.connect(docker.header.update)
    # Mute/delete/rename act on the header widget but mutate shared
    # timeline state through contentChanged -- repaint it (icon swap,
    # name edit) whenever any of that fires, undo/redo included.
    docker.timeline.contentChanged.connect(lambda _affects_audio: docker.header.update())

    docker.setWidget(container)
    docker.add_track()  # start with one track so the docker isn't empty


def sync_ruler_geometry(docker):
    docker.ruler.resize(docker.ruler.sizeHint())
    docker.ruler.update()


def sync_header_geometry(docker):
    docker.header.resize(docker.header.sizeHint())
    docker.header.update()


def build_title_bar(docker):
    # Mirrors the layout of Krita's own built-in docker title bars:
    # lock toggle on the far left, title + panel actions next to it,
    # then a stretch, then the float/close controls on the far right.
    title_bar = QWidget()
    title_layout = QHBoxLayout(title_bar)
    title_layout.setContentsMargins(4, 2, 4, 2)
    title_layout.setSpacing(2)

    docker._lock_btn = QToolButton()
    docker._lock_btn.setCheckable(True)
    docker._lock_btn.setAutoRaise(True)
    docker._lock_btn.setToolTip("Lock Docker")
    docker._lock_btn.setIcon(krita_icon("docker_lock_a", QStyle.SP_DialogYesButton))
    docker._lock_btn.toggled.connect(lambda checked: on_lock_toggled(docker, checked))
    title_layout.addWidget(docker._lock_btn)

    title_label = QLabel("Audio Timeline")
    title_layout.addWidget(title_label)
    title_layout.addSpacing(8)

    docker._mixdown_spinner = QLabel()
    docker._mixdown_spinner.setFixedSize(SPINNER_SIZE, SPINNER_SIZE)
    docker._mixdown_spinner.setToolTip("Buffering (mixing down audio)…")
    docker._mixdown_spinner.setVisible(False)
    docker._mixdown_spinner_pixmap = krita_icon(
        "selectionMask", QStyle.SP_BrowserReload
    ).pixmap(SPINNER_SIZE, SPINNER_SIZE)
    title_layout.addWidget(docker._mixdown_spinner)

    title_layout.addStretch(1)

    docker._info_btn = make_tool_button(
        "system-help", QStyle.SP_MessageBoxInformation, "Feature List", docker._open_info_dialog,
    )
    title_layout.addWidget(docker._info_btn)

    docker._settings_btn = make_tool_button(
        "configure", QStyle.SP_FileDialogDetailedView, "Audio Timeline Settings", docker._open_settings_dialog,
    )
    title_layout.addWidget(docker._settings_btn)

    # "edit-cut" is Krita's own scissors glyph (used for Edit > Cut).
    docker._split_btn = make_tool_button(
        "edit-cut", QStyle.SP_DialogResetButton, "Split Selected Clip at Playhead (S)",
        docker.timeline.split_selected_clip,
    )
    can_split = docker.timeline.can_split_selected_clip()
    docker._split_btn.setEnabled(can_split)
    docker._split_btn_opacity = QGraphicsOpacityEffect(docker._split_btn)
    docker._split_btn_opacity.setOpacity(1.0 if can_split else 0.35)
    docker._split_btn.setGraphicsEffect(docker._split_btn_opacity)
    docker.timeline.selectionChanged.connect(lambda *_a: update_split_btn_enabled(docker))
    docker.timeline.frameChanged.connect(lambda *_a: update_split_btn_enabled(docker))
    title_layout.addWidget(docker._split_btn)

    undo_btn = QToolButton()
    undo_btn.setAutoRaise(True)
    undo_btn.setDefaultAction(docker._undo_action)
    dim_when_disabled(undo_btn, docker.undo_group.canUndoChanged, docker.undo_group.canUndo())
    title_layout.addWidget(undo_btn)

    redo_btn = QToolButton()
    redo_btn.setAutoRaise(True)
    redo_btn.setDefaultAction(docker._redo_action)
    dim_when_disabled(redo_btn, docker.undo_group.canRedoChanged, docker.undo_group.canRedo())
    title_layout.addWidget(redo_btn)

    # Krita's own icon set has no bundled float/restore glyph for the
    # docker title bar (it's drawn from the OS/Qt style, not koIcon) --
    # "view-fullscreen" is the closest themed stand-in.
    float_btn = make_tool_button(
        "view-fullscreen", QStyle.SP_TitleBarNormalButton, "Float Docker",
        lambda: docker.setFloating(not docker.isFloating()),
    )
    title_layout.addWidget(float_btn)

    close_btn = make_tool_button(
        "window-close", QStyle.SP_TitleBarCloseButton, "Close Docker", docker.close,
    )
    title_layout.addWidget(close_btn)

    return title_bar


def on_lock_toggled(docker, checked):
    docker._lock_btn.setIcon(krita_icon(
        "docker_lock_b" if checked else "docker_lock_a",
        QStyle.SP_DialogYesButton,
    ))
    features = QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetFloatable
    if not checked:
        features |= QDockWidget.DockWidgetMovable
    docker.setFeatures(features)


def update_split_btn_enabled(docker):
    # Re-evaluated on both selection and playhead changes -- the split
    # button needs the playhead strictly inside the selected clip, not
    # just a clip being selected (see can_split_selected_clip).
    enabled = docker.timeline.can_split_selected_clip()
    docker._split_btn.setEnabled(enabled)
    docker._split_btn_opacity.setOpacity(1.0 if enabled else 0.35)
