"""キーボード配列の読み取り（TASKS-20261027-package 第4節）。新しい依存を入れずに、Carbon（HIToolbox）の関数を ctypes で呼ぶ。

- layout(): 今のキーボードの物理配列（JIS / ISO / ANSI）と、入力ソース・キー配列の ID
- char_of(keycode, shift): 今のキー配列で、そのキーが入力する文字（UCKeyTranslate）
- find_keycode(ch): その文字を入力するキー（Shift なし・あり）
- resolve_shortcut(name, value): 戻る・進む・ズームのキーコード。設定が "auto" なら、今の配列で「[」「]」「+」「-」のキーを使う（SPEC 13.57）
macOS 以外や、読み取れないときは例外を投げずに None を返す（呼ぶ側で「要確認」にする）。

入力ソースの関数（TIS）は、AppKit のイベントループが動いているプロセス（アプリ）では、メインスレッドから呼ばないと macOS がプロセスを止める
（dispatch_assert_queue。アプリで環境のチェックを開くと落ちた）。ほかのスレッドから呼ばれたら、メインスレッドで動かして結果を待つ（_on_main）。
"""
from __future__ import annotations

import ctypes
import functools
from ctypes import POINTER, byref, c_long, c_uint16, c_uint32, c_ulong, c_void_p

# キー名（画面に出す。文字を入力しないキー）
KEY_NAMES = {36: "Return", 48: "Tab", 49: "Space", 51: "Delete", 53: "Esc", 54: "右 Command", 55: "左 Command",
             56: "左 Shift", 57: "Caps Lock", 58: "左 Option", 59: "左 Control", 60: "右 Shift", 61: "右 Option",
             62: "右 Control", 63: "fn", 102: "英数", 104: "かな", 123: "←", 124: "→", 125: "↓", 126: "↑",
             122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5", 97: "F6", 98: "F7", 100: "F8", 101: "F9", 109: "F10",
             103: "F11", 111: "F12", 105: "F13", 107: "F14", 113: "F15", 106: "F16", 64: "F17", 79: "F18", 80: "F19",
             90: "F20", 114: "Help", 115: "Home", 116: "Page Up", 117: "Forward Delete", 119: "End", 121: "Page Down"}
MODIFIER_CODES = {54, 55, 56, 57, 58, 59, 60, 61, 62, 63}
# テンキー（ショートカットの文字を探すときは使わない。⌘ とテンキーの = や + は、アプリによって効かない）
KEYPAD_CODES = {65, 67, 69, 71, 75, 76, 78, 81, 82, 83, 84, 85, 86, 87, 88, 89, 91, 92}
FUNCTION_CODES = {122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111, 105, 107, 113, 106, 64, 79, 80, 90}
_SHIFT = 0x02  # UCKeyTranslate の修飾（shiftKey 0x0200 >> 8）


class _Carbon:
    def __init__(self):
        self.lib = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        lib, cf = self.lib, self.cf
        for name in ("TISCopyCurrentKeyboardLayoutInputSource", "TISCopyCurrentKeyboardInputSource"):
            getattr(lib, name).restype = c_void_p
        lib.TISGetInputSourceProperty.restype = c_void_p
        lib.TISGetInputSourceProperty.argtypes = [c_void_p, c_void_p]
        lib.LMGetKbdType.restype = ctypes.c_uint8
        lib.KBGetLayoutType.restype = c_uint32
        lib.KBGetLayoutType.argtypes = [ctypes.c_int16]
        lib.UCKeyTranslate.argtypes = [c_void_p, c_uint16, c_uint16, c_uint32, c_uint32, c_uint32, POINTER(c_uint32),
                                       c_ulong, POINTER(c_ulong), POINTER(c_uint16)]
        cf.CFDataGetBytePtr.restype = c_void_p
        cf.CFDataGetBytePtr.argtypes = [c_void_p]
        cf.CFStringGetCString.argtypes = [c_void_p, ctypes.c_char_p, c_long, c_uint32]
        cf.CFRelease.argtypes = [c_void_p]
        self.prop_id = c_void_p.in_dll(lib, "kTISPropertyInputSourceID")
        self.prop_layout = c_void_p.in_dll(lib, "kTISPropertyUnicodeKeyLayoutData")

    def string(self, p) -> str:
        buf = ctypes.create_string_buffer(512)
        self.cf.CFStringGetCString(p, buf, 512, 0x08000100)  # kCFStringEncodingUTF8
        return buf.value.decode()


_C: _Carbon | None = None


def _on_main(fn):
    """AppKit のイベントループが動いていて、メインスレッドでないときは、メインスレッドで呼んで結果を待つ。
    ループが動いていないとき（設定画面のサーバー、テスト）は、そのまま呼ぶ（メインスレッドに頼むと、返ってこない）。"""
    @functools.wraps(fn)
    def wrapper(*args, **kw):
        try:
            import AppKit
            import Foundation
            from libdispatch import dispatch_get_main_queue, dispatch_sync

            if Foundation.NSThread.isMainThread() or not AppKit.NSApplication.sharedApplication().isRunning():
                return fn(*args, **kw)
        except ImportError:
            return fn(*args, **kw)
        out = []
        dispatch_sync(dispatch_get_main_queue(), lambda: out.append(fn(*args, **kw)))
        return out[0] if out else None
    return wrapper


def _carbon() -> _Carbon | None:
    global _C
    if _C is None:
        try:
            _C = _Carbon()
        except (OSError, ValueError, AttributeError):
            return None
    return _C


@_on_main
def layout() -> dict | None:
    """{"physical": "JIS" / "ISO" / "ANSI", "layout_id": "com.apple.keylayout.ABC", "input_id": ...}。"""
    c = _carbon()
    if c is None:
        return None
    kt = c.lib.LMGetKbdType()
    four = c.lib.KBGetLayoutType(kt)  # 'JIS ' / 'ISO ' / 'ANSI' の 4 文字のコード
    physical = four.to_bytes(4, "big").decode("ascii", "replace").strip() if four > 0xFF else {0: "ANSI", 1: "ISO", 2: "JIS"}.get(four, "?")
    src = c.lib.TISCopyCurrentKeyboardLayoutInputSource()
    cur = c.lib.TISCopyCurrentKeyboardInputSource()
    try:
        return {"physical": physical, "kbd_type": int(kt),
                "layout_id": c.string(c.lib.TISGetInputSourceProperty(src, c.prop_id)) if src else "",
                "input_id": c.string(c.lib.TISGetInputSourceProperty(cur, c.prop_id)) if cur else ""}
    finally:
        for p in (src, cur):
            if p:
                c.cf.CFRelease(p)


@_on_main
def char_of(keycode: int, shift: bool = False) -> str | None:
    """今のキー配列で、そのキーが入力する文字（入力しないキーは ""）。"""
    c = _carbon()
    if c is None:
        return None
    src = c.lib.TISCopyCurrentKeyboardLayoutInputSource()
    if not src:
        return None
    try:
        data = c.lib.TISGetInputSourceProperty(src, c.prop_layout)
        if not data:
            return None
        dead, n, buf = c_uint32(0), c_ulong(0), (c_uint16 * 4)()
        # kUCKeyActionDown = 0、kUCKeyTranslateNoDeadKeysMask = 1
        c.lib.UCKeyTranslate(c.cf.CFDataGetBytePtr(data), keycode, 0, _SHIFT if shift else 0, c.lib.LMGetKbdType(), 1,
                             byref(dead), 4, byref(n), buf)
        return "".join(chr(buf[i]) for i in range(n.value))
    finally:
        c.cf.CFRelease(src)


@_on_main
def find_keycode(ch: str, shift: bool | None = None) -> int | None:
    """その文字を入力するキー（shift = None ならどちらでも。Shift なしを優先）。"""
    for sh in ((False, True) if shift is None else (shift,)):
        for code in range(0, 128):
            if code in MODIFIER_CODES or code in KEYPAD_CODES:
                continue
            if char_of(code, sh) == ch:
                return code
    return None


def key_label(keycode: int) -> str:
    """画面に出すキーの名前（文字を入力するキーはその文字）。"""
    if keycode in KEY_NAMES:
        return KEY_NAMES[keycode]
    ch = char_of(keycode)
    return ch.upper() if ch and ch.strip() else f"キーコード {keycode}"


# 戻る・進む・ズームのショートカット（SPEC 13.57、作者が案 A を選んだ）。設定が "auto" のときは、run の起動時に今の配列から引く。
# 読めないときは、以前の既定値（JIS 配列の位置）を使う
SHORTCUTS = {"back": ("[", "peace", "back_keycode"), "forward": ("]", "peace", "forward_keycode"),
             "in": ("+", "zoom", "in_keycode"), "out": ("-", "zoom", "out_keycode")}
JIS_DEFAULTS = {"back": 30, "forward": 42, "in": 41, "out": 27}


def zoom_in_keycode() -> int | None:
    """⌘+ に使うキー: Shift なしで「=」のキー（US の = + のキー）、なければ Shift で「+」のキー（JIS の ; + のキー）。"""
    return find_keycode("=", shift=False) or find_keycode("+", shift=True) or find_keycode("+", shift=False)


def auto_keycode(name: str) -> int | None:
    return zoom_in_keycode() if name == "in" else find_keycode(SHORTCUTS[name][0], shift=False)


def resolve_shortcut(name: str, value) -> tuple[int, str]:
    """(キーコード, どう決めたか)。value は設定の値（"auto" か数値）。"""
    if value != "auto":
        return int(value), "設定の値"
    code = auto_keycode(name)
    if code is None:
        return JIS_DEFAULTS[name], "配列を読めないので JIS の位置"
    return code, "今の配列から"
