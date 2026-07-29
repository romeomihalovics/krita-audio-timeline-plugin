from . import qtcompat  # registers the PyQt5/PyQt6 shim before anything imports it
from krita import Krita, DockWidgetFactory, DockWidgetFactoryBase
from .audiotimeline_docker import AudioTimelineDocker

Krita.instance().addDockWidgetFactory(
    DockWidgetFactory(
        "audioTimelineDocker",
        # Krita 6's own scripting API (not PyQt) also moved to scoped-only
        # enums (DockWidgetFactoryBase.DockPosition.DockRight) -- Krita 5
        # only has the flat DockRight. Same fallback qtcompat already uses
        # for Qt's own enums, just applied to a Krita class this time.
        qtcompat.enum_value(DockWidgetFactoryBase, "DockRight"),
        AudioTimelineDocker,
    )
)
