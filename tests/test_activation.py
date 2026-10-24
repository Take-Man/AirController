"""有効化（トグル・押下中）のキー処理のテスト。実際の CGEvent を作って _handle_key / _callback に渡す（タップは作らない）。"""
from __future__ import annotations

import time

import pytest

Quartz = pytest.importorskip("Quartz")

from aircontroller.output.activation import KeyHoldActivation, ToggleActivation  # noqa: E402

YEN, RCMD, C_KEY, PERIOD = 93, 54, 8, 47


def key(code, down=True, repeat=False, flags=0):
    ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
    Quartz.CGEventSetFlags(ev, flags)
    if repeat:
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGKeyboardEventAutorepeat, 1)
    return (Quartz.kCGEventKeyDown if down else Quartz.kCGEventKeyUp), ev


def rcmd(pressed):
    ev = Quartz.CGEventCreateKeyboardEvent(None, RCMD, pressed)
    Quartz.CGEventSetType(ev, Quartz.kCGEventFlagsChanged)
    Quartz.CGEventSetFlags(ev, 0x100110 if pressed else 0x100)
    return Quartz.kCGEventFlagsChanged, ev


def feed(act, *events):
    out = []
    for etype, ev in events:
        out.append(act._callback(None, etype, ev, None))
    return out


def test_toggle_with_yen_ignores_repeat_and_swallows():
    a = ToggleActivation(keys=("jis_yen",))  # 通常のキーでも切り替えられる（既定は右 Command だけ）
    out = feed(a, key(YEN), key(YEN, repeat=True), key(YEN, repeat=True), key(YEN, down=False))
    assert a.active and out == [None, None, None, None]  # 長押ししても1回だけ切り替わり、¥ は入力されない
    feed(a, key(YEN), key(YEN, down=False))
    assert not a.active


def test_yen_marks_and_does_not_toggle():
    """¥ は印を付けるだけ（TASKS-20261025 1.1）。有効化は切り替えず、イベントは握りつぶす。リピートでは印を付けない。"""
    marks = []
    a = ToggleActivation(mark_key="jis_yen")
    a.on_mark = lambda: marks.append(a.active)
    out = feed(a, key(YEN), key(YEN, repeat=True), key(YEN, down=False))
    assert not a.active and marks == [False] and out == [None, None, None]
    feed(a, rcmd(True), rcmd(False))
    assert a.active and a.last_reason == "key"
    feed(a, key(YEN), key(YEN, down=False))
    assert a.active and marks == [False, True]


def test_activation_reason():
    a = ToggleActivation()
    feed(a, rcmd(True), rcmd(False))
    a.set(False, "hand_absent")
    assert not a.active and a.last_reason == "hand_absent"


def test_toggle_with_right_command_tap():
    a = ToggleActivation()
    out = feed(a, rcmd(True), rcmd(False))
    assert a.active and all(o is not None for o in out)  # 右 Command は握りつぶさない
    feed(a, rcmd(True), rcmd(False))
    assert not a.active


def test_right_command_combo_does_not_toggle():
    a = ToggleActivation()
    feed(a, rcmd(True), key(C_KEY, flags=0x100110), key(C_KEY, down=False, flags=0x100110), rcmd(False))
    assert not a.active  # ⌘C では切り替えない


def test_right_command_long_press_does_not_toggle():
    a = ToggleActivation(modifier_tap_ms=50)
    feed(a, rcmd(True))
    time.sleep(0.08)
    feed(a, rcmd(False))
    assert not a.active


def test_deactivate_callback_and_emergency():
    calls, emerg = [], []
    a = ToggleActivation(on_emergency=lambda: emerg.append(1))
    a.on_change = calls.append
    feed(a, rcmd(True), rcmd(False))
    out = feed(a, key(PERIOD, flags=Quartz.kCGEventFlagMaskControl | Quartz.kCGEventFlagMaskAlternate
                       | Quartz.kCGEventFlagMaskCommand))
    assert calls == [True, False] and emerg == [1] and out == [None] and a.last_reason == "emergency"


def test_hold_mode():
    a = KeyHoldActivation()
    feed(a, key(YEN), key(YEN, repeat=True))
    assert a.active and a.press_count == 1 and a.repeat_count == 1
    feed(a, key(YEN, down=False))
    assert not a.active


def test_physical_modifiers_ignore_synthetic_events():
    from aircontroller.output.activation import SYNTHETIC_TAG
    a = ToggleActivation()
    ev = Quartz.CGEventCreateKeyboardEvent(None, 59, True)  # ⌃（AirController が送ったもの）
    Quartz.CGEventSetType(ev, Quartz.kCGEventFlagsChanged)
    Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskControl)
    Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGEventSourceUserData, SYNTHETIC_TAG)
    a._callback(None, Quartz.kCGEventFlagsChanged, ev, None)
    assert a.physical_flags == 0  # 自分の送ったものは数えない
    ev2 = Quartz.CGEventCreateKeyboardEvent(None, 59, True)  # 利用者が押した ⌃
    Quartz.CGEventSetType(ev2, Quartz.kCGEventFlagsChanged)
    Quartz.CGEventSetFlags(ev2, Quartz.kCGEventFlagMaskControl)
    a._callback(None, Quartz.kCGEventFlagsChanged, ev2, None)
    assert a.physical_flags & Quartz.kCGEventFlagMaskControl
    ev3 = Quartz.CGEventCreateKeyboardEvent(None, 59, False)
    Quartz.CGEventSetType(ev3, Quartz.kCGEventFlagsChanged)
    Quartz.CGEventSetFlags(ev3, 0)
    a._callback(None, Quartz.kCGEventFlagsChanged, ev3, None)
    assert a.physical_flags == 0
    assert not a.active  # ⌃ は有効化キーではない
