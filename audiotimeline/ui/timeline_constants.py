"""Layout/sizing constants shared across the timeline widget and its mixins
(painting, volume editing, interaction, clipboard) and satellite widgets
(ruler, header, chrome) -- kept in one place so splitting timeline_widget.py
into several files doesn't risk each one drifting its own copy out of sync.
"""

# Extensions accepted by drag-and-drop from outside Krita and by pasting a
# file path copied from the OS -- kept in sync with import_audio's own
# QFileDialog filter (AudioTimelineDocker.import_audio).
EXTERNAL_AUDIO_EXTENSIONS = ('.wav', '.mp3', '.ogg', '.flac')

TRACK_HEIGHT = 64
RULER_HEIGHT = 24
TRACK_HEADER_WIDTH = 110
BUTTON_SIZE = 18
BUTTON_GAP = 4
# How close (in px) the mouse needs to be to a clip's left/right edge for a
# press/hover there to be treated as a trim-edge drag rather than a
# whole-clip move.
HANDLE_PX = 6
# Size (px) of the small per-clip volume-editing toggle icon drawn at each
# clip's top-left corner.
VOLUME_ICON_SIZE = 13
# Gain (1.0 == 100%) at which the volume line sits at clip_rect's vertical
# middle -- the mapping's fixed point -- and the ceiling it can be dragged
# up to (mapped to clip_rect's top). 0.0 always maps to the bottom.
VOLUME_GAIN_UNITY = 1.0
VOLUME_GAIN_MAX = 2.0
# Radius (px) of the filled circle drawn at each bend point, and the hit-test
# tolerance (combined with HANDLE_PX) for grabbing/double-clicking one.
VOLUME_POINT_RADIUS = 4

# Ruler tick spacing: the smallest gap (in px) two adjacent tick labels can
# sit at before they'd start overlapping/crowding each other.
# The smallest gap (in px) two adjacent tick *marks* can sit at -- much
# tighter than the label spacing below, so ticks can show finer subdivisions
# than their labels do without the marks themselves smearing together.
RULER_MIN_TICK_SPACING_PX = 10
RULER_MIN_LABEL_SPACING_PX = 40
# "Nice" round second counts to fall back to once even a one-second step
# would be too dense (zoomed out far) -- ticks then land on these instead
# of an arbitrary number of seconds.
NICE_SECOND_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)
