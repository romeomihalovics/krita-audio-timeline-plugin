from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QWidget, QFrame,
    QGraphicsOpacityEffect,
)

# How much less intense (opacity-wise) the description text and the
# shortcut/action box borders are versus the full-strength title color.
DESCRIPTION_OPACITY = 0.8

# (feature name, keys/gesture, one-line description) -- kept in sync with
# the README's "Features" section; update both together.
FEATURES = [
    ("Add Track", None, "Adds a new empty audio lane."),
    ("Import Audio", None, "Loads a clip starting at the current playhead frame (.wav always works; "
                            "see the README for other formats)."),
    ("Copy / paste a clip", "Ctrl+C · Ctrl+V · right-click → Copy Clip/Paste", "Duplicates the selected "
                             "clip — trim, split, volume envelope and all — onto the active track at "
                             "the playhead, or at the right-clicked position. Copying clears the system "
                             "clipboard, since that takes priority over it (see below)."),
    ("Drag & drop from outside Krita", "Drag a file onto the timeline",
     "Imports .wav/.mp3/.ogg/.flac file(s) dropped in from the OS file manager."),
    ("Paste a file from outside Krita", "Ctrl+V · right-click → Paste", "Pastes whatever audio file(s) "
                             "are on the system clipboard (e.g. copied in the OS file manager) — always "
                             "takes priority over a clip copied inside the timeline."),
    ("Scrub", "Click/drag the ANIMATION TIMELINE's ruler or lane", "Moves the playhead along with Krita's native timeline, while playing the audio."),
    ("Move a clip", "Drag", "Repositions it earlier/later or onto another track; it snaps into the "
                             "nearest free gap instead of overlapping."),
    ("Trim a clip", "Drag its left/right edge", "Shortens or lengthens the clip from that end."),
    ("Split a clip", "S", "Cuts the selected clip in two at the playhead."),
    ("Select / delete a clip", "Click · Delete/Backspace · right-click → Delete Clip",
     "Selects a clip, then removes it."),
    ("Rename / delete a track", "Double-click name · header button", "Renames a track, or deletes it."),
    ("Mute a track", "Audio button in its header", "Drops it from the next mixdown."),
    ("Zoom", "Ctrl + scroll", "Zooms the timeline horizontally."),
    ("Volume editing", "Click the clip's volume icon", "Enters per-clip volume editing; drag the "
                        "flat line up/down to set overall gain."),
    ("Add a volume point", "Double-click the volume line", "Inserts a bend point there, at its current gain."),
    ("Move a volume point", "Drag it", "Reshapes the gain curve."),
    ("Remove a volume point", "Double-click it · Delete · right-click → Remove Point",
     "Deletes that bend point (endpoints are permanent)."),
    ("Set an exact gain", "Double-click a percentage readout", "Opens a number-entry dialog (up to 200%)."),
    ("Exit volume editing", "Apply/Cancel icons · Escape", "Keeps or discards the changes made this session."),
    ("Undo / Redo", "Ctrl+Z / Ctrl+Y (or the docker's own buttons)", "Its own history, independent of "
                     "Krita's canvas undo."),
    ("Auto update", "Cog icon → Settings", "Checks this plugin's GitHub releases for a newer version, "
                     "on startup or on demand."),
]


class InfoDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Audio Timeline — Features")
        self.setMinimumSize(480, 520)

        outer = QVBoxLayout(self)

        heading = QLabel("Features")
        heading_font = heading.font()
        heading_font.setPointSize(heading_font.pointSize() + 3)
        heading_font.setBold(True)
        heading.setFont(heading_font)
        outer.addWidget(heading)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll, 1)

        list_widget = QWidget()
        list_layout = QVBoxLayout(list_widget)
        list_layout.setContentsMargins(0, 4, 0, 4)
        list_layout.setSpacing(2)

        for name, keys, description in FEATURES:
            list_layout.addWidget(self._feature_row(name, keys, description))

        list_layout.addStretch(1)
        scroll.setWidget(list_widget)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        close_row.addWidget(close_btn)
        outer.addLayout(close_row)

    def _feature_row(self, name, keys, description):
        row = QFrame()
        row.setFrameShape(QFrame.NoFrame)
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 6, 0, 6)
        row_layout.setSpacing(1)

        header_line = QHBoxLayout()
        header_line.setSpacing(8)

        name_label = QLabel(name)
        name_font = name_label.font()
        name_font.setBold(True)
        name_font.setPointSize(name_font.pointSize() + 2)
        name_label.setFont(name_font)
        header_line.addWidget(name_label)

        if keys:
            keys_label = QLabel(keys)
            # Same base color as the title (palette(text), no override) --
            # the opacity effect below is what dims both the border and the
            # text to match the description's intensity.
            keys_label.setStyleSheet(
                "QLabel { border: 1px solid palette(text); border-radius: 3px; "
                "padding: 0px 5px; }"
            )
            keys_effect = QGraphicsOpacityEffect(keys_label)
            keys_effect.setOpacity(DESCRIPTION_OPACITY)
            keys_label.setGraphicsEffect(keys_effect)
            keys_font = keys_label.font()
            keys_font.setPointSize(max(keys_font.pointSize() - 1, 7))
            keys_label.setFont(keys_font)
            header_line.addWidget(keys_label)

        header_line.addStretch(1)
        row_layout.addLayout(header_line)

        desc_label = QLabel(description)
        desc_label.setWordWrap(True)
        # No explicit color -- inherits the same palette(text) color as the
        # title, dimmed only via opacity so it reads as the same color at
        # reduced intensity rather than a different hue.
        desc_effect = QGraphicsOpacityEffect(desc_label)
        desc_effect.setOpacity(DESCRIPTION_OPACITY)
        desc_label.setGraphicsEffect(desc_effect)
        row_layout.addWidget(desc_label)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setFrameShadow(QFrame.Sunken)
        row_layout.addWidget(divider)

        return row
