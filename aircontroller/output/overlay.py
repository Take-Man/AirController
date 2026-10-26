"""オーバーレイ表示（常に手前、クリックを通す、フォーカスを奪わない）。

段階1の内容: カメラ画像内の検出位置と操作範囲、現在のモード、ピンチのプログレスリング、
フレームレート、検出信頼度、遅延。描画はメインスレッドの NSTimer から行う（推論スレッドとは独立）。
"""
from __future__ import annotations

import math
import time
from typing import Callable

import AppKit
import objc
from Foundation import NSObject, NSMakeRect, NSMakePoint

W, H = 280, 236
SMALL_W, SMALL_H = 252, 52  # カメラを止めている間（No signal）の大きさ（ギアのボタンの分、210 から広げた。13.51）
COMPACT_W, COMPACT_H = 263, 52  # 畳んだ状態（TASKS-20261025 第4節）。ギアのボタンの分、236 から広げた（13.51）
BTN = 30  # カメラのボタンの大きさ
SBTN = 22  # 畳んだ帯のボタンの大きさ
POWER_JA = {"on": "オン", "off": "オフ", "probe": "確認中"}
CAM_W, CAM_H = 256, 144
PAD = 12

MODE_COLORS = {"idle": (0.6, 0.6, 0.6), "cursor": (0.25, 0.8, 0.35), "pinch": (1.0, 0.65, 0.1),
               "drag": (1.0, 0.3, 0.3), "right": (0.55, 0.5, 1.0), "peace": (0.2, 0.75, 1.0),
               "fist": (0.45, 0.45, 0.5), "thumbsup": (0.95, 0.45, 0.85),
               "zoom": (0.3, 0.9, 0.8)}


def _color(rgb, a=1.0):
    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], a)


def _text(s, x, y, size=12, rgb=(1, 1, 1), bold=False):
    font = AppKit.NSFont.boldSystemFontOfSize_(size) if bold else AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(size, 0.0)
    attrs = {AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: _color(rgb)}
    AppKit.NSString.stringWithString_(s).drawAtPoint_withAttributes_(NSMakePoint(x, y), attrs)


def _text_fit(s, x, y, size, rgb, max_w: float, bold: bool = False) -> None:
    """幅 max_w に収まるよう、末尾を … で切って描く。"""
    font = AppKit.NSFont.boldSystemFontOfSize_(size) if bold else AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(size, 0.0)
    attrs = {AppKit.NSFontAttributeName: font}

    def width(t):
        return AppKit.NSString.stringWithString_(t).sizeWithAttributes_(attrs).width

    if width(s) > max_w:
        while s and width(s + "…") > max_w:
            s = s[:-1]
        s += "…"
    _text(s, x, y, size, rgb, bold)


def _draw_peace(v, cx: float, cy: float, r: float) -> None:
    """ピースモードの4領域: 中央の円が不感帯、斜めの線で上下左右に分ける。外周 = 最大（スクロール最大速度）。
    左右の破線の円 = 発火の角度。点が現在の指先の向き（上が上）。"""
    def circle(rad, fill=None, stroke=None, width=1.0, dash=False):
        path = AppKit.NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - rad, cy - rad, 2 * rad, 2 * rad))
        if fill:
            _color(*fill).setFill()
            path.fill()
        if stroke:
            _color(*stroke).setStroke()
            path.setLineWidth_(width)
            if dash:
                path.setLineDash_count_phase_([3.0, 3.0], 2, 0.0)
            path.stroke()

    circle(r, fill=((0, 0, 0), 0.55), stroke=((1, 1, 1), 0.5))
    # 現在の領域を塗る（扇形）
    angles = {"right": (-45, 45), "up": (225, 315), "left": (135, 225), "down": (45, 135)}  # 反転座標系の角度
    if v.region in angles:
        a0, a1 = angles[v.region]
        wedge = AppKit.NSBezierPath.bezierPath()
        wedge.moveToPoint_(NSMakePoint(cx, cy))
        wedge.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(NSMakePoint(cx, cy), r, a0, a1, False)
        wedge.closePath()
        _color((0.2, 0.75, 1.0), 0.35).setFill()
        wedge.fill()
    lines = AppKit.NSBezierPath.bezierPath()
    for dx, dy in ((1, 1), (1, -1)):
        lines.moveToPoint_(NSMakePoint(cx - dx * r * 0.707, cy - dy * r * 0.707))
        lines.lineToPoint_(NSMakePoint(cx + dx * r * 0.707, cy + dy * r * 0.707))
    _color((1, 1, 1), 0.3).setStroke()
    lines.setLineWidth_(1.0)
    lines.stroke()
    circle(r * v.fire, stroke=((1.0, 0.65, 0.1), 0.8), dash=True)
    circle(r * v.dead, fill=((0.08, 0.08, 0.1), 0.9), stroke=((1, 1, 1), 0.6))
    if v.nav_progress > 0:  # 戻る・進むの発火までの経過
        arc = AppKit.NSBezierPath.bezierPath()
        arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
            NSMakePoint(cx, cy), r + 4, -90, -90 + 360 * v.nav_progress, False)
        arc.setLineWidth_(4)
        _color((1.0, 0.65, 0.1)).setStroke()
        arc.stroke()
    px = cx + max(-1.2, min(1.2, v.sx)) * r
    py = cy - max(-1.2, min(1.2, v.sy)) * r  # 上が正 → 反転座標系では y を引く
    _color((1, 1, 1)).setFill()
    AppKit.NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(px - 4, py - 4, 8, 8)).fill()


def _draw_camera_button(r, slashed: bool) -> None:
    """カメラのマークのボタン。slashed=True は「押すと止める」（斜線つき）。"""
    _draw_symbol_button(r, "video.slash.fill" if slashed else "video.fill", "■" if slashed else "▶")


def _draw_collapse_button(r, collapsed: bool) -> None:
    """畳む／広げるのボタン。"""
    _draw_symbol_button(r, "chevron.down" if collapsed else "chevron.up", "▼" if collapsed else "▲")


def _draw_settings_button(r) -> None:
    """設定画面を開くボタン（ギア）。"""
    _draw_symbol_button(r, "gearshape.fill", "⚙")


def full_buttons(ty: float) -> list[tuple[str, tuple]]:
    """展開した表示のボタン（右から カメラ・設定・畳む）。ty はモードの行の上端。"""
    return [("camera", (W - BTN - 10, ty, BTN, BTN)), ("settings", (W - 2 * BTN - 16, ty, BTN, BTN)),
            ("collapse", (W - 3 * BTN - 22, ty, BTN, BTN))]


def compact_buttons() -> list[tuple[str, tuple]]:
    """畳んだ帯のボタン（右から カメラ・設定・広げる）。"""
    y = (COMPACT_H - SBTN) / 2
    return [("camera", (COMPACT_W - SBTN - 8, y, SBTN, SBTN)), ("settings", (COMPACT_W - 2 * SBTN - 13, y, SBTN, SBTN)),
            ("collapse", (COMPACT_W - 3 * SBTN - 18, y, SBTN, SBTN))]


def small_buttons() -> list[tuple[str, tuple]]:
    """No signal の表示のボタン（右から カメラ・設定）。"""
    y = (SMALL_H - BTN) / 2
    return [("camera", (SMALL_W - BTN - 11, y, BTN, BTN)), ("settings", (SMALL_W - 2 * BTN - 17, y, BTN, BTN))]


_SYMBOLS: dict = {}  # (名前, 大きさ) → 白く塗ったアイコン。毎回作ると、待機中の描画だけで CPU 15〜24% を使っていた


def _symbol(name: str, pt: int):
    key = (name, pt)
    if key not in _SYMBOLS:
        img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, name)
        tinted = None
        if img is not None:
            conf = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(pt, 0.0)
            img = img.imageWithSymbolConfiguration_(conf) or img
            tinted = AppKit.NSImage.alloc().initWithSize_(img.size())
            tinted.lockFocus()
            img.drawAtPoint_fromRect_operation_fraction_(NSMakePoint(0, 0), AppKit.NSZeroRect,
                                                         AppKit.NSCompositingOperationSourceOver, 1.0)
            _color((1, 1, 1)).set()
            AppKit.NSRectFillUsingOperation(NSMakeRect(0, 0, img.size().width, img.size().height),
                                            AppKit.NSCompositingOperationSourceAtop)
            tinted.unlockFocus()
        _SYMBOLS[key] = tinted
    return _SYMBOLS[key]


def _draw_symbol_button(r, name: str, fallback: str) -> None:
    x, y, w, h = r
    _color((1, 1, 1), 0.18).setFill()
    AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(x, y, w, h), 7, 7).fill()
    tinted = _symbol(name, 14 if w >= BTN else 11)
    if tinted is not None:
        sz = tinted.size()
        tinted.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
            NSMakeRect(x + (w - sz.width) / 2, y + (h - sz.height) / 2, sz.width, sz.height), AppKit.NSZeroRect,
            AppKit.NSCompositingOperationSourceOver, 1.0, True, None)
    else:
        _text(fallback, x + w / 2 - 6, y + h / 2 - 9, 13, (1, 1, 1), bold=True)


def _draw_ring(cx: float, cy: float, r: float, progress: float, rgb, width: float = 3.0) -> None:
    _color((1, 1, 1), 0.25).setStroke()
    ring = AppKit.NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - r, cy - r, 2 * r, 2 * r))
    ring.setLineWidth_(width)
    ring.stroke()
    if progress > 0:
        arc = AppKit.NSBezierPath.bezierPath()
        arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
            NSMakePoint(cx, cy), r, -90, -90 + 360 * min(1.0, progress), False)
        arc.setLineWidth_(width)
        _color(rgb).setStroke()
        arc.stroke()


def _draw_zoom_bar(s, bx: float, by: float, bw: float, bh: float) -> None:
    """ズームの量: 中央が 0、上がズームイン、下がアウト。発火の閾値の目盛り、発火直後の黄色、再武装前は暗く。"""
    _color((0, 0, 0), 0.55).setFill()
    AppKit.NSBezierPath.fillRect_(NSMakeRect(bx, by, bw, bh))
    lv = max(-1.0, min(1.0, s.get("zoom_level", 0.0)))
    if s.get("zoom_flash"):
        _color((1.0, 0.95, 0.4)).setFill()
    elif not s.get("zoom_armed", True):
        _color((0.3, 0.9, 0.8), 0.35).setFill()
    else:
        _color((0.3, 0.9, 0.8), 0.6 + 0.4 * s.get("zoom_progress", 0.0)).setFill()
    mid = by + bh / 2
    h = abs(lv) * bh / 2
    AppKit.NSBezierPath.fillRect_(NSMakeRect(bx, mid - h if lv > 0 else mid, bw, h))
    _color((1, 1, 1), 0.6).setFill()
    AppKit.NSBezierPath.fillRect_(NSMakeRect(bx - 2, mid - 0.5, bw + 4, 1))
    fl = s.get("zoom_fire_level")
    if fl is not None:
        _color((1, 1, 1), 0.35).setFill()
        for yy in (mid - fl * bh / 2, mid + fl * bh / 2):
            AppKit.NSBezierPath.fillRect_(NSMakeRect(bx, yy - 0.5, bw, 1))


class OverlayView(AppKit.NSView):
    def initWithFrame_(self, frame):
        self = objc.super(OverlayView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.get_state = None
        self.state = None  # 直近の状態（Overlay が毎回取って入れる）
        self.buttons = []  # 表示中のボタン [(rect, callback)]
        self.on_button = None  # カメラのボタン
        self.on_collapse = None  # 畳む／広げるのボタン
        self.on_settings = None  # 設定画面を開くボタン（ギア。None なら出さない）
        self.collapsed = False
        return self

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True  # フォーカスを取らずに、1回のクリックで押せるように

    def mouseDown_(self, event):
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        self.press(p.x, p.y)

    @objc.python_method
    def press(self, x: float, y: float) -> bool:
        """(x, y)（ビューの座標、上が 0）にあるボタンのコールバックを呼ぶ。"""
        for r, cb in self.buttons:
            if cb and r[0] <= x <= r[0] + r[2] and r[1] <= y <= r[1] + r[3]:
                cb()
                return True
        return False

    @objc.python_method
    def _add_buttons(self, rects: list[tuple[str, tuple]], collapsed: bool) -> None:
        """ボタンを描き、押せるように登録する。"""
        for name, r in rects:
            cb = {"camera": self.on_button, "settings": self.on_settings, "collapse": self.on_collapse}[name]
            if name == "settings" and cb is None:
                continue
            if name == "camera":
                _draw_camera_button(r, slashed=self.state.get("camera", "running") == "running")
            elif name == "settings":
                _draw_settings_button(r)
            else:
                _draw_collapse_button(r, collapsed=collapsed)
            self.buttons.append((r, cb))

    def drawRect_(self, rect):
        _color((0.08, 0.08, 0.1), 0.78).setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 10, 10).fill()
        s = self.state
        self.buttons = []
        if not s:
            return
        if s.get("camera", "running") != "running":  # カメラを止めている: No signal と、起動のボタンだけ
            msg = {"starting": "カメラを起動中…", "error": "カメラを起動できません"}.get(s["camera"], "No signal")
            sub = {"stopped": f"{s.get('camera_reason') or ''}停止・ボタンか有効化で再開"}.get(s["camera"], "")
            rects = small_buttons() if s["camera"] in ("stopped", "error") else []
            max_w = min(r[0] for _, r in rects) - 14 - 6 if rects else SMALL_W - 28
            _text_fit(msg, 14, 10, 15, (0.9, 0.9, 0.9), max_w, bold=True)
            _text_fit(sub, 14, 30, 10, (0.7, 0.7, 0.7), max_w)
            self._add_buttons(rects, collapsed=False)
            return
        if self.collapsed:
            self._draw_compact(s)
        else:
            self._draw_full(s)

    def _mode_label(self, s) -> str:
        if s["active"]:
            return s["mode"].upper()
        return f"{s['mode']}（{POWER_JA.get(s.get('power'), '待機')}）"

    def _draw_compact(self, s):
        """畳んだ帯: 小さい表示（ズームの棒・ピースの4領域・リング）、ジェスチャー名、左右ラベルとオン・オフ、設定。"""
        mode_rgb = MODE_COLORS.get(s["mode"], (1, 1, 1))
        cx, cy = 26, COMPACT_H / 2
        if s.get("mode") == "zoom":
            _draw_zoom_bar(s, cx - 5, 6, 10, COMPACT_H - 12)
        elif s.get("peace") is not None:
            _draw_peace(s["peace"], cx, cy, 16)
        elif s["pinch_progress"] > 0:
            _draw_ring(cx, cy, 14, s["pinch_progress"],
                       (1.0, 0.65, 0.1) if s["pinch_progress"] < 1 else (1.0, 0.3, 0.3))
        elif s.get("power") == "probe":  # パーの保持の経過
            _draw_ring(cx, cy, 14, s.get("wake_progress", 0.0), (0.3, 0.9, 0.5))
        else:
            _color(mode_rgb, 1.0 if s["active"] else 0.5).setFill()
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(cx - 7, cy - 7, 14, 14)).fill()
        tx = 52
        max_w = (min(r[0] for _, r in compact_buttons()) - 6 if not s["active"] else COMPACT_W - 8) - tx
        _text_fit(self._mode_label(s), tx, 5, 14, mode_rgb if s["active"] else (0.7, 0.7, 0.7), max_w)
        sub = s["warning"] if s.get("warning") else f"手 {s.get('hand_label', '-')}  {s.get('config_label', '')}"
        _text_fit(sub, tx, 29, 10, (1.0, 0.45, 0.4) if s.get("warning") else (0.8, 0.8, 0.8), max_w)
        if not s["active"]:  # 待機中だけボタンを出す（操作中はクリックを下のウィンドウへ通す）
            self._add_buttons(compact_buttons(), collapsed=True)

    def _draw_full(self, s):
        # カメラ画像（鏡像）内の操作範囲と検出位置
        ox, oy = PAD, PAD
        _color((1, 1, 1), 0.12).setFill()
        AppKit.NSBezierPath.fillRect_(NSMakeRect(ox, oy, CAM_W, CAM_H))
        x0, y0, rw, rh = s["roi"]
        _color((0.4, 0.7, 1.0), 0.9).setStroke()
        roi = AppKit.NSBezierPath.bezierPathWithRect_(NSMakeRect(ox + (1 - x0 - rw) * CAM_W, oy + y0 * CAM_H,
                                                                 rw * CAM_W, rh * CAM_H))
        roi.setLineWidth_(1.0)
        roi.stroke()
        mode_rgb = MODE_COLORS.get(s["mode"], (1, 1, 1))
        if s["landmarks"] is not None:
            _color((1, 1, 1), 0.7).setFill()
            for x, y, _ in s["landmarks"]:
                AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(ox + (1 - x) * CAM_W - 1.5, oy + y * CAM_H - 1.5, 3, 3)).fill()
        if s["palm"] is not None:
            px, py = ox + (1 - s["palm"][0]) * CAM_W, oy + s["palm"][1] * CAM_H
            _color(mode_rgb).setFill()
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(px - 5, py - 5, 10, 10)).fill()
        if s.get("mode") == "zoom":
            _draw_zoom_bar(s, ox + CAM_W - 24, oy + 8, 14, CAM_H - 16)
        if s.get("peace") is not None:
            _draw_peace(s["peace"], ox + CAM_W - 72, oy + 72, 64)

        # モードとプログレスリング（確認の窓では、パーの保持の経過）
        ty = oy + CAM_H + 10
        cx, cy, r = ox + 16, ty + 16, 13
        if s["pinch_progress"] > 0:
            _draw_ring(cx, cy, r, s["pinch_progress"], (1.0, 0.65, 0.1) if s["pinch_progress"] < 1 else (1.0, 0.3, 0.3),
                       4)
        else:
            _draw_ring(cx, cy, r, s.get("wake_progress", 0.0) if s.get("power") == "probe" else 0.0,
                       (0.3, 0.9, 0.5), 4)
        # 待機中は、ボタンの手前で切る（ギアの分、以前より 36pt 狭い。13.51）
        max_w = (min(r[0] for _, r in full_buttons(ty)) - 6 if not s["active"] else W - PAD) - (ox + 38)
        _text_fit(self._mode_label(s), ox + 38, ty + 2, 17, mode_rgb if s["active"] else (0.7, 0.7, 0.7), max_w, bold=True)
        pn = s["pinch_n"]
        _text_fit((f"pinch {pn:.2f}" if not math.isnan(pn) else "pinch -") + f"   {s.get('config_label', '')}",
                  ox + 38, ty + 22, 11, (0.85, 0.85, 0.85), max_w)
        _text(s["status"], ox, ty + 42, 11, (0.85, 0.85, 0.85))
        if s.get("warning"):
            _text(s["warning"], ox, ty + 58, 11, (1.0, 0.45, 0.4))
        if not s["active"]:  # 待機中だけ、カメラを止めるボタン、設定画面を開くボタン、畳むボタンを出す
            self._add_buttons(full_buttons(ty), collapsed=False)


def buttons_shown(s) -> bool:
    """ボタン（カメラ・設定・畳む）を出す状態: カメラが止まっている（No signal の停止・エラー）か、カメラが動いていて操作がオフ。"""
    if not s:
        return False
    if s.get("camera", "running") != "running":
        return s.get("camera") in ("stopped", "error")
    return not s.get("active", True)


class _Ticker(NSObject):
    def initWithCallback_(self, cb):
        self = objc.super(_Ticker, self).init()
        if self is None:
            return None
        self.cb = cb
        return self

    def tick_(self, timer):
        self.cb()


def _signature(s, collapsed: bool):
    """待機中に、描き直す必要があるかを決めるための、表示の要約（数値の細かい揺れと、毎回変わる status の文字列は除く）。"""
    if not s:
        return None
    out = [collapsed]
    for k in sorted(s):
        v = s[k]
        if k in ("status", "landmarks", "roi"):
            continue
        if isinstance(v, float):
            v = None if math.isnan(v) else round(v, 2)  # NaN は NaN と等しくならず、毎回描き直していた
        elif isinstance(v, tuple):
            v = tuple(round(x, 3) if isinstance(x, float) else x for x in v)
        elif not isinstance(v, (str, int, bool, type(None))):
            v = repr(getattr(v, "__dict__", v))
        out.append((k, v))
    return tuple(out)


class Overlay:
    def __init__(self, get_state: Callable[[], dict | None], tick: Callable[[], bool], hz: float = 30.0,
                 idle_hz: float = 10.0,
                 on_camera_button: Callable[[], None] | None = None, collapsed: bool = False,
                 on_collapse_change: Callable[[bool], None] | None = None,
                 on_settings_button: Callable[[], None] | None = None):
        """tick() は描画のたびに呼ばれ、False を返すとイベントループを止める。
        on_camera_button: カメラのボタンが押されたとき（メインスレッド）。ボタンを出している間だけクリックを受け取る。
        collapsed: 畳んだ状態で始める。on_collapse_change: 畳む／広げるのボタンで切り替えたとき（状態の保存に使う）。
        on_settings_button: ギアのボタン（設定画面を開く。メインスレッドから呼ぶので、すぐ戻ること）。カメラのボタンと同じときに出す。"""
        self.app = AppKit.NSApplication.sharedApplication()
        self.app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # Dock に出さず、フォーカスを取らない
        screen = AppKit.NSScreen.mainScreen().visibleFrame()
        rect = NSMakeRect(screen.origin.x + screen.size.width - W - 16,
                          screen.origin.y + screen.size.height - H - 16, W, H)
        style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel
        self.win = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False)
        self.win.setLevel_(AppKit.NSStatusWindowLevel)
        self.win.setIgnoresMouseEvents_(True)
        self.win.setOpaque_(False)
        self.win.setBackgroundColor_(AppKit.NSColor.clearColor())
        self.win.setHasShadow_(False)
        self.win.setHidesOnDeactivate_(False)
        self.win.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                        | AppKit.NSWindowCollectionBehaviorStationary
                                        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        self.view = OverlayView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        self.view.get_state = get_state
        self.view.on_button = on_camera_button
        self.view.on_settings = on_settings_button
        self.view.collapsed = collapsed

        def toggle_collapse():
            self.view.collapsed = not self.view.collapsed
            if on_collapse_change:
                on_collapse_change(self.view.collapsed)

        self.view.on_collapse = toggle_collapse
        self.win.setContentView_(self.view)
        self.win.orderFrontRegardless()
        self._tick = tick
        self._size = (W, H)
        self._last_sig = None
        self._last_draw = 0.0
        self._screen = screen

        def on_tick():
            # 待機中・カメラ停止中は、状態の取得と安全の確認（tick）も 3 回に 1 回にする（30Hz → 10Hz）。
            # オフ（1Hz）の CPU の目標 15% に対し、待機中のメインスレッドが 5〜6% 使っていた（2026-10-25 の記録）
            if not self._tick():
                self.stop()
                return
            s = get_state()
            self.view.state = s
            small = bool(s) and s.get("camera", "running") != "running"
            # カメラを止めたら小さく（No signal）、畳んだら帯に、それ以外は元の大きさに
            size = (SMALL_W, SMALL_H) if small else (COMPACT_W, COMPACT_H) if self.view.collapsed else (W, H)
            if size != self._size:
                self._size = size
                w, h = size
                sc = self._screen
                self.win.setFrame_display_(NSMakeRect(sc.origin.x + sc.size.width - w - 16,
                                                      sc.origin.y + sc.size.height - h - 16, w, h), True)
                self.view.setFrame_(NSMakeRect(0, 0, w, h))
            # ボタンを出しているときだけクリックを受け取る（それ以外は下のウィンドウへ通す）
            clickable = bool(s) and (on_camera_button is not None or on_settings_button is not None) and \
                buttons_shown(s)
            if clickable == self.win.ignoresMouseEvents():
                self.win.setIgnoresMouseEvents_(not clickable)
            # 待機中・カメラ停止中は、タイマーそのものを idle_hz に下げる（30Hz のタイマーの呼び出しだけで CPU 約 4% だった）
            want_hz = hz if (s and s.get("active")) else min(hz, idle_hz)
            if want_hz != self._hz:
                self._schedule(want_hz)
            # 操作中は毎回描く。待機中・カメラ停止中は、表示が変わったときか 1 秒ごとだけ描く
            now = time.monotonic()
            if s and s.get("active"):
                self.view.setNeedsDisplay_(True)
                self._last_draw = now
            else:
                sig = _signature(s, self.view.collapsed)
                if sig != self._last_sig or now - self._last_draw >= 1.0:
                    self._last_sig, self._last_draw = sig, now
                    self.view.setNeedsDisplay_(True)

        self._ticker = _Ticker.alloc().initWithCallback_(on_tick)
        self._timer = None
        self._hz = None
        self._schedule(hz)

    def _schedule(self, hz: float) -> None:
        if self._timer is not None:
            self._timer.invalidate()
        self._hz = hz
        self._timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0 / hz, self._ticker, "tick:", None, True)
        AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(self._timer, AppKit.NSRunLoopCommonModes)

    def run(self) -> None:
        self.app.run()

    def stop(self) -> None:
        self._timer.invalidate()
        self.win.orderOut_(None)
        self.app.stop_(None)
        # stop_ はイベントを1つ処理した後に効くので、空のイベントを投げて起こす
        ev = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            AppKit.NSEventTypeApplicationDefined, NSMakePoint(0, 0), 0, 0, 0, None, 0, 0, 0)
        self.app.postEvent_atStart_(ev, True)
