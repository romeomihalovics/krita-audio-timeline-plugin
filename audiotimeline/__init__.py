from krita import Krita, DockWidgetFactory, DockWidgetFactoryBase
from .audiotimeline_docker import AudioTimelineDocker

Krita.instance().addDockWidgetFactory(
    DockWidgetFactory(
        "audioTimelineDocker",
        DockWidgetFactoryBase.DockRight,
        AudioTimelineDocker,
    )
)
