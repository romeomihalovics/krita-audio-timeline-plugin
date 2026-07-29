"""Qt binding compatibility shim.

Krita 5.x bundles PyQt5; Krita 6.x switched to PyQt6 (Qt6). This module
must be imported before anything does ``from PyQt5... import ...`` (the
package ``__init__.py`` does this first): if only PyQt6 is present, it
registers PyQt6's QtCore/QtGui/QtWidgets under the ``PyQt5`` name in
``sys.modules`` so every existing ``from PyQt5.QtX import Y`` statement
keeps working unchanged on both versions.

It also exposes normalized enum members, since PyQt6 removed the flat
``Qt.AlignLeft``-style enum access in favour of scoped access
(``Qt.AlignmentFlag.AlignLeft``), plus small helpers for other Qt5/Qt6
API differences (``.exec_()`` removal, ``QMouseEvent.globalPos()``
removal).
"""

import sys
import types

try:
    import PyQt5  # noqa: F401
    from PyQt5 import QtCore, QtGui, QtWidgets

    QT_MAJOR = 5
except ImportError:
    import PyQt6
    from PyQt6 import QtCore, QtGui, QtWidgets

    QT_MAJOR = 6

    # PyQt6 also relocated a few classes from QtWidgets to QtGui (e.g.
    # QUndoStack/QUndoGroup/QUndoCommand, QAction) -- rather than mutate
    # the real QtWidgets module, build a stand-in module exposing both so
    # `from PyQt5.QtWidgets import QUndoStack` still works unchanged.
    _widgets_compat = types.ModuleType("PyQt5.QtWidgets")
    _widgets_compat.__dict__.update(QtGui.__dict__)
    _widgets_compat.__dict__.update(QtWidgets.__dict__)
    # Every other module in this package reaches QtWidgets classes via
    # `qtcompat.QtWidgets.X` (see below), not via sys.modules -- so this
    # name must be rebound to the merged stand-in too, or the relocated
    # classes it exists to backfill (QUndoStack and friends) would be
    # invisible to them even though sys.modules["PyQt5.QtWidgets"] has them.
    QtWidgets = _widgets_compat

    sys.modules.setdefault("PyQt5", PyQt6)
    sys.modules.setdefault("PyQt5.QtCore", QtCore)
    sys.modules.setdefault("PyQt5.QtGui", QtGui)
    sys.modules.setdefault("PyQt5.QtWidgets", _widgets_compat)

Qt = QtCore.Qt

import enum as _enum_module


def enum_value(owner, name):
    """Return enum member `name` belonging to `owner` (any Qt class, or
    the ``Qt`` namespace), on either binding.

    PyQt5 exposes enum members directly on the owning class (flat access,
    e.g. ``QStyle.SP_MediaVolume``). PyQt6 removed that in favour of
    scoped access only (``QStyle.StandardPixmap.SP_MediaVolume``) -- so
    when the flat attribute isn't there, this searches `owner`'s nested
    enum types for one with a matching member.
    """
    direct = getattr(owner, name, None)
    if direct is not None:
        return direct
    for attr_name in dir(owner):
        attr = getattr(owner, attr_name, None)
        if isinstance(attr, type) and issubclass(attr, _enum_module.Enum) and hasattr(attr, name):
            return getattr(attr, name)
    raise AttributeError(f"{owner!r} has no enum member {name!r}")


# Mouse buttons
LEFT_BUTTON = enum_value(Qt, "LeftButton")
RIGHT_BUTTON = enum_value(Qt, "RightButton")
NO_BUTTON = enum_value(Qt, "NoButton")

# Keyboard modifiers
CONTROL_MODIFIER = enum_value(Qt, "ControlModifier")
SHIFT_MODIFIER = enum_value(Qt, "ShiftModifier")
NO_MODIFIER = enum_value(Qt, "NoModifier")

# Drag-and-drop / wheel
COPY_ACTION = enum_value(Qt, "CopyAction")
NO_SCROLL_PHASE = enum_value(Qt, "NoScrollPhase")

# Alignment
ALIGN_LEFT = enum_value(Qt, "AlignLeft")
ALIGN_TOP = enum_value(Qt, "AlignTop")
ALIGN_VCENTER = enum_value(Qt, "AlignVCenter")
ALIGN_CENTER = enum_value(Qt, "AlignCenter")

# Pen / brush / cursor / color
NO_PEN = enum_value(Qt, "NoPen")
NO_BRUSH = enum_value(Qt, "NoBrush")
ARROW_CURSOR = enum_value(Qt, "ArrowCursor")
SIZE_HOR_CURSOR = enum_value(Qt, "SizeHorCursor")
SIZE_VER_CURSOR = enum_value(Qt, "SizeVerCursor")
TRANSPARENT = enum_value(Qt, "transparent")
SMOOTH_TRANSFORMATION = enum_value(Qt, "SmoothTransformation")

# Widget behavior
SCROLLBAR_ALWAYS_OFF = enum_value(Qt, "ScrollBarAlwaysOff")
STRONG_FOCUS = enum_value(Qt, "StrongFocus")
WA_TRANSPARENT_FOR_MOUSE_EVENTS = enum_value(Qt, "WA_TransparentForMouseEvents")

# Keys
KEY_DELETE = enum_value(Qt, "Key_Delete")
KEY_BACKSPACE = enum_value(Qt, "Key_Backspace")
KEY_ESCAPE = enum_value(Qt, "Key_Escape")
KEY_S = enum_value(Qt, "Key_S")
KEY_Z = enum_value(Qt, "Key_Z")
KEY_Y = enum_value(Qt, "Key_Y")
KEY_C = enum_value(Qt, "Key_C")
KEY_V = enum_value(Qt, "Key_V")

# QEvent.Type members -- used by tests to construct synthetic QMouseEvent/
# QKeyEvent instances dispatched directly at widget event handlers.
EVENT_MOUSE_BUTTON_PRESS = enum_value(QtCore.QEvent, "MouseButtonPress")
EVENT_MOUSE_MOVE = enum_value(QtCore.QEvent, "MouseMove")
EVENT_MOUSE_BUTTON_RELEASE = enum_value(QtCore.QEvent, "MouseButtonRelease")
EVENT_MOUSE_BUTTON_DBL_CLICK = enum_value(QtCore.QEvent, "MouseButtonDblClick")
EVENT_KEY_PRESS = enum_value(QtCore.QEvent, "KeyPress")


def event_global_pos(event):
    """QMouseEvent global position: ``globalPos()`` on Qt5,
    ``globalPosition().toPoint()`` on Qt6 (``globalPos()`` was removed)."""
    if hasattr(event, "globalPosition"):
        return event.globalPosition().toPoint()
    return event.globalPos()


def event_pos(event):
    """Widget-local event position: ``pos()`` on Qt5 (kept, deprecated, on
    QMouseEvent in Qt6 too, but dropped entirely by Qt6 for QDropEvent/
    QDragEnterEvent/QDragMoveEvent/QWheelEvent), ``position().toPoint()``
    on Qt6 where ``pos()`` is gone."""
    if hasattr(event, "pos"):
        return event.pos()
    return event.position().toPoint()
