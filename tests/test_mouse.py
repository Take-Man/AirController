"""出力層: 送るイベントの修飾キー（SPEC 13.23）。実際には送らず、CGEventPost を差し替えて中身を調べる。"""
import pytest

Quartz = pytest.importorskip("Quartz")

from aircontroller.output import mouse as mouse_mod  # noqa: E402
from aircontroller.output.activation import SYNTHETIC_TAG  # noqa: E402


@pytest.fixture
def posted(monkeypatch):
    out = []
    monkeypatch.setattr(mouse_mod.Quartz, "CGEventPost", lambda tap, ev: out.append(ev))
    return out


def test_click_carries_only_physical_modifiers(posted):
    m = mouse_mod.Mouse(physical_flags=lambda: 0)
    m.click()
    assert [Quartz.CGEventGetFlags(e) & mouse_mod.MOD_MASK for e in posted] == [0, 0]
    posted.clear()
    m.physical_flags = lambda: Quartz.kCGEventFlagMaskCommand  # 利用者が ⌘ を押している → ⌘クリック
    m.click()
    assert all(Quartz.CGEventGetFlags(e) & Quartz.kCGEventFlagMaskCommand for e in posted)


def test_shortcut_presses_and_releases_modifiers(posted):
    m = mouse_mod.Mouse(physical_flags=lambda: 0)
    m.shortcut(124, ("ctrl", "fn"))
    kinds = [(Quartz.CGEventGetType(e), Quartz.CGEventGetIntegerValueField(e, Quartz.kCGKeyboardEventKeycode),
              Quartz.CGEventGetFlags(e) & mouse_mod.MOD_MASK) for e in posted]
    fc, kd, ku = Quartz.kCGEventFlagsChanged, Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp
    ctrl, fn = Quartz.kCGEventFlagMaskControl, Quartz.kCGEventFlagMaskSecondaryFn
    assert kinds == [(fc, 59, ctrl), (fc, 63, ctrl | fn), (kd, 124, ctrl | fn), (ku, 124, ctrl | fn),
                     (fc, 63, ctrl), (fc, 59, 0)]  # 最後は修飾キーなし
    assert all(Quartz.CGEventGetIntegerValueField(e, Quartz.kCGEventSourceUserData) == SYNTHETIC_TAG for e in posted)
