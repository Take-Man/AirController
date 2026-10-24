"""マウス・スクロール・ショートカットの送出（CGEvent）。複数スレッドから呼ばれるので、ロックで直列化する。"""
from __future__ import annotations

import threading
import time

import AppKit
import Quartz

from .activation import MOD_MASK, SYNTHETIC_TAG

_MOD_FLAG = {"ctrl": Quartz.kCGEventFlagMaskControl, "opt": Quartz.kCGEventFlagMaskAlternate,
             "cmd": Quartz.kCGEventFlagMaskCommand, "shift": Quartz.kCGEventFlagMaskShift,
             "fn": Quartz.kCGEventFlagMaskSecondaryFn}
MOD_KEYCODES = {"ctrl": 59, "opt": 58, "cmd": 55, "shift": 56, "fn": 63}  # 左側の修飾キー


class Mouse:
    def __init__(self, physical_flags=None):
        """physical_flags: 利用者が実際に押している修飾キーのフラグを返す関数（キー監視から）。無ければ修飾なし。"""
        self.physical_flags = physical_flags
        self._lock = threading.Lock()
        self._src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        self._pos = self.current_position()
        self._left_down = False
        self._last_click_t = 0.0
        self._last_click_pos = (-1e9, -1e9)
        self._click_count = 0
        self._dbl_interval = float(AppKit.NSEvent.doubleClickInterval())

    @staticmethod
    def current_position() -> tuple[float, float]:
        p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return (p.x, p.y)

    def _post(self, kind, pos, button=Quartz.kCGMouseButtonLeft, click_state: int | None = None) -> None:
        ev = Quartz.CGEventCreateMouseEvent(self._src, kind, pos, button)
        # 修飾キーは、利用者が実際に押しているものだけを付ける。イベントの元（HID の状態）から引き継ぐと、
        # 送ったショートカットの ⌃ や ⌘ が押されたままと扱われたとき、クリックが ⌃クリック（＝右クリック）や
        # ⌘クリックになってしまう（作者の報告: いいねの後などに、クリックが右クリックになり、右クリックが効かない）
        Quartz.CGEventSetFlags(ev, self.physical_flags() if self.physical_flags else 0)
        if click_state is not None:
            Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, click_state)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def move(self, x: float, y: float) -> None:
        with self._lock:
            self._pos = (x, y)
            kind = Quartz.kCGEventLeftMouseDragged if self._left_down else Quartz.kCGEventMouseMoved
            self._post(kind, self._pos)

    def _next_click_state(self) -> int:
        now = time.monotonic()
        near = abs(self._pos[0] - self._last_click_pos[0]) < 6 and abs(self._pos[1] - self._last_click_pos[1]) < 6
        self._click_count = self._click_count + 1 if (now - self._last_click_t < self._dbl_interval and near) else 1
        self._last_click_t, self._last_click_pos = now, self._pos
        return self._click_count

    def click(self) -> None:
        """現在位置で左クリック。ダブルクリックの間隔内なら click state を増やす。"""
        with self._lock:
            n = self._next_click_state()
            self._post(Quartz.kCGEventLeftMouseDown, self._pos, click_state=n)
            self._left_down = True
            self._post(Quartz.kCGEventLeftMouseUp, self._pos, click_state=n)
            self._left_down = False

    def right_click(self) -> None:
        with self._lock:
            self._post(Quartz.kCGEventRightMouseDown, self._pos, Quartz.kCGMouseButtonRight, click_state=1)
            self._post(Quartz.kCGEventRightMouseUp, self._pos, Quartz.kCGMouseButtonRight, click_state=1)

    def left_down(self, x: float, y: float) -> None:
        """ドラッグの開始。押した位置へ移動してからボタンを押す。"""
        with self._lock:
            if self._left_down:
                return
            self._pos = (x, y)
            self._post(Quartz.kCGEventMouseMoved, self._pos)
            self._post(Quartz.kCGEventLeftMouseDown, self._pos, click_state=self._next_click_state())
            self._left_down = True

    def left_up(self) -> None:
        with self._lock:
            if self._left_down:
                self._post(Quartz.kCGEventLeftMouseUp, self._pos, click_state=self._click_count)
                self._left_down = False

    def scroll(self, dy: int) -> None:
        """縦スクロール（ピクセル単位）。dy > 0 でページの先（下）へ進む。CGEvent のホイール値は、負が下方向。"""
        with self._lock:
            ev = Quartz.CGEventCreateScrollWheelEvent(self._src, Quartz.kCGScrollEventUnitPixel, 1, int(-dy))
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def shortcut(self, keycode: int, mods: tuple[str, ...] = ("cmd",)) -> None:
        """キーボードショートカットを送る（例: ⌘[）。キーコードはキーの位置なので、配列（JIS/US）に合わせて設定する。"""
        flags = 0
        for m in mods:
            flags |= {"cmd": Quartz.kCGEventFlagMaskCommand, "shift": Quartz.kCGEventFlagMaskShift,
                      "opt": Quartz.kCGEventFlagMaskAlternate, "ctrl": Quartz.kCGEventFlagMaskControl,
                      # 矢印キーのショートカットは、macOS 側で fn・テンキーのフラグ付きで登録されている
                      "fn": Quartz.kCGEventFlagMaskSecondaryFn, "numpad": Quartz.kCGEventFlagMaskNumericPad}[m]
        with self._lock:
            # 修飾キーも実際に押して離す（押下→キー→離す）。フラグだけを付けて送ると、システムの修飾キーの状態と
            # 食い違い、⌃ や ⌘ が押されたままと扱われることがある
            mod_keys = [MOD_KEYCODES[m] for m in mods if m in MOD_KEYCODES]
            held = 0
            for m, kc in zip([m for m in mods if m in MOD_KEYCODES], mod_keys):
                held |= _MOD_FLAG[m]
                self._post_key(kc, True, held, flags_changed=True)
            self._post_key(keycode, True, flags)
            self._post_key(keycode, False, flags)
            for m, kc in reversed(list(zip([m for m in mods if m in MOD_KEYCODES], mod_keys))):
                held &= ~_MOD_FLAG[m]
                self._post_key(kc, False, held, flags_changed=True)

    def _post_key(self, keycode: int, down: bool, flags: int, flags_changed: bool = False) -> None:
        ev = Quartz.CGEventCreateKeyboardEvent(self._src, keycode, down)
        if flags_changed:
            Quartz.CGEventSetType(ev, Quartz.kCGEventFlagsChanged)
        Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGEventSourceUserData, SYNTHETIC_TAG)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def stuck_modifiers(self) -> int:
        """システムが押下中と見ているが、利用者は押していない修飾キー（⌃ ⌥ ⌘ ⇧ fn）。"""
        state = Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateHIDSystemState) & MOD_MASK
        physical = self.physical_flags() if self.physical_flags else 0
        return state & ~physical

    def release_stuck_modifiers(self) -> list[str]:
        """押されたままと扱われている修飾キーを、離すイベントを送って解除する。解除したものの名前を返す。"""
        stuck = self.stuck_modifiers()
        names = []
        if stuck:
            with self._lock:
                remaining = Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateHIDSystemState) & MOD_MASK
                for m, flag in _MOD_FLAG.items():
                    if stuck & flag and m in MOD_KEYCODES:
                        remaining &= ~flag
                        self._post_key(MOD_KEYCODES[m], False, remaining, flags_changed=True)
                        names.append(m)
        return names

    @property
    def is_left_down(self) -> bool:
        return self._left_down

    def release_all(self) -> None:
        """押下中のボタンを必ず離す（有効化の解除、緊急停止、終了処理から呼ぶ）。"""
        with self._lock:
            if self._left_down:
                self._post(Quartz.kCGEventLeftMouseUp, self._pos)
                self._left_down = False
