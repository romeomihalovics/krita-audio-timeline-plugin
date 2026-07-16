"""Icon lookup/tinting helpers, cached at module level since they're
re-invoked on every repaint of every clip/button that shows one."""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import QApplication

_icon_cache = {}
_fps_divisors_cache = {}


def fps_divisors(fps):
    """All divisors of `fps`, ascending (including fps itself last) --
    candidate sub-second tick spacings that land evenly within a second."""
    if fps not in _fps_divisors_cache:
        _fps_divisors_cache[fps] = [d for d in range(1, fps + 1) if fps % d == 0]
    return _fps_divisors_cache[fps]


def krita_icon(name, fallback_standard_icon=None):
    """Look up one of Krita's own themed icons by name, cached per
    (name, fallback) pair.

    Falls back to a Qt-bundled standard icon (e.g. QStyle.SP_DialogApplyButton)
    when given one and Krita's Python API isn't available (e.g. this module
    imported outside Krita) or doesn't recognize the name -- otherwise
    returns a null QIcon, and callers without a fallback need to tolerate
    that themselves.
    """
    cache_key = (name, fallback_standard_icon)
    if cache_key not in _icon_cache:
        icon = QIcon()
        try:
            from krita import Krita
            icon = Krita.instance().icon(name)
        except Exception:
            pass
        if icon.isNull() and fallback_standard_icon is not None:
            style = QApplication.style()
            if style is not None:
                icon = style.standardIcon(fallback_standard_icon)
        _icon_cache[cache_key] = icon
    return _icon_cache[cache_key]


_tinted_pixmap_cache = {}


def tinted_icon_pixmap(icon, size, color):
    """`icon` rendered at `size`x`size` and recolored solid `color`,
    keeping only its original alpha shape -- so the passive volume badge's
    icon can match theme['clip_text'] exactly (the color the badge's own
    percentage text is drawn in) rather than whatever fixed tint the
    Krita/Qt icon theme happens to bake in. Cached per (icon, size, color)
    since it's repainted every frame a badge is on screen."""
    cache_key = (icon.cacheKey(), size, color.rgba())
    if cache_key not in _tinted_pixmap_cache:
        pixmap = icon.pixmap(size, size)
        if not pixmap.isNull():
            tinted = QPixmap(pixmap.size())
            tinted.fill(Qt.transparent)
            painter = QPainter(tinted)
            painter.drawPixmap(0, 0, pixmap)
            painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
            painter.fillRect(tinted.rect(), color)
            painter.end()
            pixmap = tinted
        _tinted_pixmap_cache[cache_key] = pixmap
    return _tinted_pixmap_cache[cache_key]
