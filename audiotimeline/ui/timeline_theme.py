"""Color derivation for the timeline widgets -- every paint color is derived
from the live QPalette rather than hardcoded, so the timeline stays
consistent with whatever theme Krita is currently using (dark, light, or a
custom one), instead of assuming a specific dark scheme."""

from PyQt5.QtCore import QRect
from PyQt5.QtGui import QColor, QPalette


def mix(c1, c2, t):
    """Linear-interpolate between two QColors (t=0 -> c1, t=1 -> c2)."""
    t = max(0.0, min(1.0, t))
    return QColor(
        int(c1.red() + (c2.red() - c1.red()) * t),
        int(c1.green() + (c2.green() - c1.green()) * t),
        int(c1.blue() + (c2.blue() - c1.blue()) * t),
    )


def complementary(c):
    """The hue-opposite of `c` (same saturation/value/alpha, hue rotated
    180 degrees) -- used to derive a color that reads as unmistakably
    distinct from the accent-derived clip/text colors while still being
    computed from (so themed consistently with) the same palette."""
    h, s, v, a = c.getHsv()
    if h < 0:  # fully desaturated (grayscale) -- no hue to rotate
        return QColor(c)
    return QColor.fromHsv((h + 180) % 360, s, v, a)


def intensify(c):
    """A more saturated, brighter version of `c` at the same hue -- used
    for the selected-bend-point highlight so it reads as "this same color,
    turned up" rather than an unrelated accent color competing with it."""
    h, s, v, a = c.getHsv()
    if h < 0:  # fully desaturated -- no hue to push, just brighten
        h, s = 0, 0
    s = min(255, int(s * 1.35) + 40)
    v = min(255, int(v * 1.2) + 40)
    return QColor.fromHsv(h, s, v, a)


def paint_out_of_range_overlay(painter, end_x, width, height):
    """When a clip runs past the animation's own length, the timeline is
    widened to show its full end (see _content_end_frame()) -- shade that
    trailing region (from `end_x`, the animation's real end, to `width`)
    over the full `height` so it reads as "outside the playable range"
    rather than more animation. Shared between the track widget (its own
    lanes) and the ruler widget (the ticks above them) so the shading
    lines up seamlessly across both."""
    if end_x >= width:
        return
    painter.fillRect(QRect(end_x, 0, width - end_x, height), QColor(15, 15, 15, 90))


def theme_colors(widget):
    """Derive every paint color from `widget`'s live QPalette. Called as
    AudioTimelineWidget._theme_colors(self), and also reached directly by
    the satellite widgets (ruler/header/corner/border) via
    `timeline._theme_colors()` -- see ThemeMixin."""
    pal = widget.palette()
    canvas_bg = pal.color(QPalette.Window)
    text = pal.color(QPalette.WindowText)
    accent = pal.color(QPalette.Highlight)
    accent_text = pal.color(QPalette.HighlightedText)

    is_dark = canvas_bg.lightness() < 128
    black, white = QColor(0, 0, 0), QColor(255, 255, 255)
    # Direction that reads as "raised"/foreground relative to the
    # background, whichever theme we're in.
    toward_fg = white if is_dark else black
    toward_bg = black if is_dark else white

    def elevate(c, t):
        return mix(c, toward_fg, t)

    def recede(c, t):
        return mix(c, toward_bg, t)

    header_bg = elevate(canvas_bg, 0.10)
    # Selected track: an unambiguously darker shade of the header
    # (not a hardcoded blue), regardless of light/dark theme.
    header_active_bg = mix(header_bg, black, 0.35)

    return {
        'canvas_bg': canvas_bg,
        'header_bg': header_bg,
        'header_active_bg': header_active_bg,
        'header_text': text,
        'lane_bg': recede(canvas_bg, 0.08),
        'lane_muted_bg': recede(canvas_bg, 0.20),
        'border': recede(canvas_bg, 0.45),
        'ruler_bg': recede(canvas_bg, 0.35),
        'ruler_text': mix(text, canvas_bg, 0.3),
        'button_bg': elevate(canvas_bg, 0.18),
        'button_muted_bg': mix(QColor(190, 70, 70), canvas_bg, 0.15),
        'clip_fill': mix(accent, canvas_bg, 0.25),
        'clip_border': mix(accent, canvas_bg, 0.55),
        'clip_border_selected': elevate(accent, 0.25),
        'clip_text': accent_text,
        'waveform': mix(accent_text, accent, 0.25),
        # Hue-complement of the accent, elevated toward the foreground --
        # deliberately not built from clip_fill/clip_border/clip_text (all
        # accent-derived) so the volume-editing line reads as clearly
        # distinct from everything else drawn on a clip, in either theme.
        'volume_line': elevate(complementary(accent), 0.2),
        # Same hue as volume_line, just turned up (more saturated/
        # brighter) -- the selected-bend-point ring, so it reads as
        # "this point, emphasized" rather than an unrelated highlight
        # color competing with the curve/points it's drawn around.
        'volume_point_selected': intensify(elevate(complementary(accent), 0.2)),
        # Segment of a clip's waveform that's been driven past +/-100%
        # by its own gain (i.e. would itself clip on mixdown) -- reuses
        # the same warning-red base as button_muted_bg (just mixed less
        # toward canvas_bg, so it reads as more saturated/alarming than
        # the muted-button tint) rather than a fresh hardcoded color.
        'waveform_clipped': mix(QColor(190, 70, 70), canvas_bg, 0.1),
        # Background behind the volume readout/badge text -- opposite
        # brightness from clip_text (the color actually drawn on top of
        # it, not just canvas_bg/is_dark) so the text stays legible
        # whichever theme makes clip_text light or dark.
        'readout_bg': QColor(0, 0, 0, 170) if accent_text.lightness() >= 128 else QColor(255, 255, 255, 170),
    }


class ThemeMixin:
    def _theme_colors(self):
        return theme_colors(self)
