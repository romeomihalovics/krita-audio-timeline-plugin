"""qtcompat.enum_value()/event_global_pos(): the fallback logic that lets
the rest of the plugin use one spelling for an enum member or a mouse
event's global position regardless of whether PyQt5 (flat enum access,
`globalPos()`) or PyQt6 (scoped-only enum access, `globalPosition()`) is
the binding actually loaded. Exercised here against small stand-in
classes rather than real PyQt5/PyQt6 objects, so these pass regardless
of which binding happens to be installed in the environment running the
tests.
"""

import enum

import pytest

from .. import qtcompat


class _FlatOwner:
    """Mimics a PyQt5-style class: the enum member sits directly on the
    class, no nested enum type."""
    SomeMember = 42


class _ScopedEnum(enum.Enum):
    SomeMember = "scoped-value"
    OtherMember = "other-value"


class _ScopedOwner:
    """Mimics a PyQt6-style class: the member only exists on a nested
    enum type, not directly on the owning class."""
    SomeEnum = _ScopedEnum


class _UnrelatedEnum(enum.Enum):
    Unrelated = "unrelated-value"


class _NoSuchMemberOwner:
    OtherEnum = _UnrelatedEnum


def test_enum_value_returns_flat_attribute_when_present():
    assert qtcompat.enum_value(_FlatOwner, "SomeMember") == 42


def test_enum_value_falls_back_to_nested_scoped_enum():
    assert qtcompat.enum_value(_ScopedOwner, "SomeMember") is _ScopedEnum.SomeMember


def test_enum_value_raises_when_member_is_nowhere_to_be_found():
    with pytest.raises(AttributeError):
        qtcompat.enum_value(_NoSuchMemberOwner, "SomeMember")


class _Qt5StyleEvent:
    def globalPos(self):
        return "qt5-global-pos"


class _Qt6StylePosition:
    def toPoint(self):
        return "qt6-global-pos-as-point"


class _Qt6StyleEvent:
    def globalPosition(self):
        return _Qt6StylePosition()


def test_event_global_pos_uses_global_pos_when_no_global_position_method():
    assert qtcompat.event_global_pos(_Qt5StyleEvent()) == "qt5-global-pos"


def test_event_global_pos_prefers_global_position_when_available():
    assert qtcompat.event_global_pos(_Qt6StyleEvent()) == "qt6-global-pos-as-point"


def test_qt_major_matches_the_bound_qtcore_module():
    # Whichever binding qtcompat actually resolved is the one QT_MAJOR
    # should report -- checked by inspecting qtcompat.QtCore's own
    # __package__ rather than doing a fresh `import PyQt5`/`import PyQt6`
    # here: qtcompat already aliased "PyQt5" to "PyQt6" (or vice versa) in
    # sys.modules by the time this test runs, which would make a fresh
    # import of either name resolve trivially through that alias instead
    # of reflecting what's genuinely installed.
    bound_package = qtcompat.QtCore.__package__
    assert bound_package == f"PyQt{qtcompat.QT_MAJOR}"
