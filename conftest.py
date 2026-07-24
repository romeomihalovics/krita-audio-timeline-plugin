"""Root conftest: installs a minimal fake `krita` module into sys.modules
*before* anything imports the `audiotimeline` package (whose __init__.py
does `from krita import ...` at module level, and would otherwise fail
outside a real Krita process on every single test, since importing any
`audiotimeline.*` submodule first runs the package's __init__.py).

This stub is deliberately tiny: just enough surface (DockWidget as a real
QDockWidget subclass, a no-op Krita.instance(), and placeholder factory
classes) for the plugin package to import and for AudioTimelineDocker to be
instantiated in tests without a running Krita. Nothing here talks to a real
Krita install.
"""

import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if "krita" not in sys.modules:
    from PyQt5.QtWidgets import QDockWidget

    class _FakeDockWidget(QDockWidget):
        def canvasChanged(self, canvas):
            pass

    class _FakeKritaInstance:
        def addDockWidgetFactory(self, *args, **kwargs):
            pass

        def activeDocument(self):
            return None

        def documents(self):
            return []

    class _FakeKrita:
        @staticmethod
        def instance():
            return _FakeKritaInstance()

    class _FakeDockWidgetFactoryBase:
        DockRight = 0

    class _FakeDockWidgetFactory:
        def __init__(self, *args, **kwargs):
            pass

    krita_module = types.ModuleType("krita")
    krita_module.DockWidget = _FakeDockWidget
    krita_module.Krita = _FakeKrita
    krita_module.DockWidgetFactory = _FakeDockWidgetFactory
    krita_module.DockWidgetFactoryBase = _FakeDockWidgetFactoryBase
    sys.modules["krita"] = krita_module
