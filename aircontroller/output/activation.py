"""有効化の方式。

Activation は「いま有効か」と、その変化の通知だけを提供する。方式は差し替え可能。
- ToggleActivation（既定）: キーを押すたびに開始・終了を切り替える。
- KeyHoldActivation: キーを押している間だけ有効（ラベル付き記録に使う）。

どちらも CGEventTap を専用スレッドの CFRunLoop で動かす。同じタップで緊急停止のホットキーも監視する。
"""
from __future__ import annotations

import threading
import time
from typing import Callable

import Quartz

KEYCODES = {"jis_yen": 93, "right_command": 54, "left_command": 55}
# AirController が送るキーイベントに付ける印（kCGEventSourceUserData）。キー監視で自分の送ったものを見分ける
SYNTHETIC_TAG = 0x41437472  # "ACtr"
MOD_MASK = (Quartz.kCGEventFlagMaskControl | Quartz.kCGEventFlagMaskAlternate | Quartz.kCGEventFlagMaskCommand
            | Quartz.kCGEventFlagMaskShift | Quartz.kCGEventFlagMaskSecondaryFn)
MODIFIER_KEYS = {54: 0x10, 55: 0x08}  # 右 Command / 左 Command の device 依存フラグ（NX_DEVICE{R,L}CMDKEYMASK）
MOD_FLAGS = {"ctrl": Quartz.kCGEventFlagMaskControl, "opt": Quartz.kCGEventFlagMaskAlternate,
             "cmd": Quartz.kCGEventFlagMaskCommand, "shift": Quartz.kCGEventFlagMaskShift}
_MOD_ALL = sum(MOD_FLAGS.values())


def keycode_of(key: str | int) -> int:
    return KEYCODES[key] if isinstance(key, str) else int(key)


class Activation:
    def __init__(self):
        self.active = False
        self.on_change: Callable[[bool], None] | None = None

    def _set(self, value: bool) -> None:
        if value != self.active:
            self.active = value
            if self.on_change:
                self.on_change(value)

    def start(self) -> None: ...

    def stop(self) -> None:
        self._set(False)


class TapError(RuntimeError):
    pass


class _TapActivation(Activation):
    """CGEventTap を使う有効化の共通部分。_handle_key をサブクラスで実装する。"""

    def __init__(self, emergency_keycode: int = 47, emergency_mods: tuple[str, ...] = ("ctrl", "opt", "cmd"),
                 on_emergency: Callable[[], None] | None = None):
        super().__init__()
        self.emergency_keycode = emergency_keycode
        self.emergency_flags = sum(MOD_FLAGS[m] for m in emergency_mods)
        self.on_emergency = on_emergency
        self.physical_flags = 0  # 利用者が実際に押している修飾キー（AirController が送ったイベントを除いて追う）
        self.press_count = 0  # 有効化キーの押下回数（キーリピートを除く。検証用）
        self.repeat_count = 0
        self._loop = None
        self._tap = None
        self._ready = threading.Event()
        self._error: str | None = None
        self._thread = threading.Thread(target=self._run, name="keytap", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(5.0)
        if self._error:
            raise TapError(self._error)

    def _run(self) -> None:
        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
                | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged))
        self._tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                            Quartz.kCGEventTapOptionDefault, mask, self._callback, None)
        if self._tap is None:
            self._error = "CGEventTap を作れませんでした（入力監視・アクセシビリティの権限を確認してください）"
            self._ready.set()
            return
        src = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._loop, src, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        self._ready.set()
        Quartz.CFRunLoopRun()

    def _callback(self, proxy, etype, event, refcon):
        if etype in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
            # キーのイベントを取りこぼした可能性があるので、安全側に倒して無効化する
            self._set(False)
            Quartz.CGEventTapEnable(self._tap, True)
            return event
        code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
        flags = Quartz.CGEventGetFlags(event)
        if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData) == SYNTHETIC_TAG:
            return event  # 自分が送ったショートカット。有効化の判定にも、実際の修飾キーの追跡にも使わない
        self.physical_flags = flags & MOD_MASK
        if etype == Quartz.kCGEventKeyDown and code == self.emergency_keycode \
                and (flags & _MOD_ALL) == self.emergency_flags:
            self._set(False)
            if self.on_emergency:
                self.on_emergency()
            return None
        return self._handle_key(etype, code, flags, event)

    def _handle_key(self, etype, code, flags, event):
        return event

    def stop(self) -> None:
        super().stop()
        if self._tap is not None:
            Quartz.CGEventTapEnable(self._tap, False)
        if self._loop is not None:
            Quartz.CFRunLoopStop(self._loop)
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)


class KeyHoldActivation(_TapActivation):
    """キーを押している間だけ有効。キーのイベントは握りつぶす（¥ の文字入力を防ぐ）。リピートは無視する。"""

    def __init__(self, key: str | int = "jis_yen", swallow: bool = True, **kw):
        super().__init__(**kw)
        self.keycode = keycode_of(key)
        self.swallow = swallow

    def _handle_key(self, etype, code, flags, event):
        if code != self.keycode:
            return event
        if self.keycode in MODIFIER_KEYS:
            if etype != Quartz.kCGEventFlagsChanged:
                return event
            pressed = bool(flags & MODIFIER_KEYS[self.keycode])
            if pressed and not self.active:
                self.press_count += 1
            self._set(pressed)
        elif etype == Quartz.kCGEventKeyDown:
            if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat):
                self.repeat_count += 1  # リピートは有効化の合図にしない（握りつぶしだけ行う）
            else:
                self.press_count += 1
                self._set(True)
        elif etype == Quartz.kCGEventKeyUp:
            self._set(False)
        return None if self.swallow else event


class ToggleActivation(_TapActivation):
    """キーを押すたびに開始・終了を切り替える。

    - 通常のキー（¥ など）: リピートでない押下で切り替える。そのキーのイベントは握りつぶす。
    - 修飾キー（右 Command など）: 単独で押して離したときだけ切り替える。押している間に別のキーを押した場合
      （⌘C など）や、modifier_tap_ms より長く押した場合は切り替えない。修飾キーのイベントは握りつぶさない。
    """

    def __init__(self, keys=("right_command", "jis_yen"), swallow: bool = True, modifier_tap_ms: float = 400.0,
                 **kw):
        super().__init__(**kw)
        self.keycodes = {keycode_of(k) for k in keys}
        self.swallow = swallow
        self.modifier_tap_ms = modifier_tap_ms
        self._mod_down: dict[int, float] = {}  # 押下中の修飾キー -> 押した時刻（切り替え候補）
        self.rejected = 0  # 長押しで切り替えなかった回数

    def _toggle(self) -> None:
        self.press_count += 1
        self._set(not self.active)

    def _handle_key(self, etype, code, flags, event):
        if etype == Quartz.kCGEventKeyDown and code not in self.keycodes:
            self._mod_down.clear()  # 修飾キーとの組み合わせ（⌘C など）は切り替えにしない
            return event
        if code not in self.keycodes:
            return event
        if code in MODIFIER_KEYS:
            if etype == Quartz.kCGEventFlagsChanged:
                if flags & MODIFIER_KEYS[code]:
                    self._mod_down[code] = time.monotonic()
                else:
                    t0 = self._mod_down.pop(code, None)
                    if t0 is not None:
                        held = (time.monotonic() - t0) * 1000
                        if held <= self.modifier_tap_ms:
                            self._toggle()
                        else:
                            self.rejected += 1
                            print(f"[activation] 右 Command の押下が {held:.0f}ms で長すぎたため、切り替えませんでした"
                                  f"（{self.modifier_tap_ms:.0f}ms 以内に離してください）")
            return event
        if etype == Quartz.kCGEventKeyDown:
            if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat):
                self.repeat_count += 1
            else:
                self._toggle()
        return None if self.swallow else event


def make_activation(cfg: dict, on_emergency: Callable[[], None], method: str | None = None) -> Activation:
    a = cfg.get("activation", {})
    s = cfg.get("safety", {})
    kw = dict(emergency_keycode=s.get("emergency_keycode", 47),
              emergency_mods=tuple(s.get("emergency_mods", ["ctrl", "opt", "cmd"])), on_emergency=on_emergency)
    method = method or a.get("method", "toggle")
    if method == "toggle":
        return ToggleActivation(keys=tuple(a.get("toggle_keys", ["right_command", "jis_yen"])),
                                swallow=a.get("swallow", True), modifier_tap_ms=a.get("modifier_tap_ms", 400.0), **kw)
    if method == "hold":
        return KeyHoldActivation(key=a.get("hold_key", "jis_yen"), swallow=a.get("swallow", True), **kw)
    raise ValueError(f"未対応の有効化方式: {method}")
