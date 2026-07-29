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

import importlib.util
import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _load_qtcompat():
    # Plain `from audiotimeline import qtcompat` would run
    # audiotimeline/__init__.py first (Python always initializes a
    # package before any of its submodules), which does `from krita
    # import ...` -- exactly the import this fake `krita` module doesn't
    # exist yet to satisfy. Loading qtcompat.py directly by file path
    # sidesteps that ordering problem, so the PyQt5/PyQt6 shim it
    # installs (see qtcompat.py's docstring) is in place *before* the
    # QDockWidget import below, same as it would be for the real plugin.
    repo_root = os.path.dirname(os.path.abspath(__file__))
    module_path = os.path.join(repo_root, "audiotimeline", "qtcompat.py")
    spec = importlib.util.spec_from_file_location("audiotimeline.qtcompat", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules.setdefault("audiotimeline.qtcompat", module)
    return module


if "krita" not in sys.modules:
    _load_qtcompat()
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
