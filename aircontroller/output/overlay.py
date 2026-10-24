"""オーバーレイ表示（常に手前、クリックを通す、フォーカスを奪わない）。

段階1の内容: カメラ画像内の検出位置と操作範囲、現在のモード、ピンチのプログレスリング、
フレームレート、検出信頼度、遅延。描画はメインスレッドの NSTimer から行う（推論スレッドとは独立）。
"""
from __future__ import annotations

import math
from typing import Callable

import AppKit
import objc
from Foundation import NSObject, NSMakeRect, NSMakePoint

W, H = 280, 236
SMALL_W, SMALL_H = 210, 52  # カメラを止めている間（No signal）の大きさ
COMPACT_W, COMPACT_H = 236, 52  # 畳んだ状態（TASKS-20261025 第4節）。No signal の帯と同程度
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


def _text_fit(s, x, y, size, rgb, max_w: float) -> None:
    """幅 max_w に収まるよう、末尾を … で切って描く。"""
    font = AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(size, 0.0)
    attrs = {AppKit.NSFontAttributeName: font}

    def width(t):
        return AppKit.NSString.stringWithString_(t).sizeWithAttributes_(attrs).width

    if width(s) > max_w:
        while s and width(s + "…") > max_w:
            s = s[:-1]
        s += "…"
    _text(s, x, y, size, rgb)


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


def _draw_symbol_button(r, name: str, fallback: str) -> None:
    x, y, w, h = r
    _color((1, 1, 1), 0.18).setFill()
    AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(x, y, w, h), 7, 7).fill()
    img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, name)
    if img is not None:
        conf = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(14 if w >= BTN else 11, 0.0)
        img = img.imageWithSymbolConfiguration_(conf) or img
        tinted = AppKit.NSImage.alloc().initWithSize_(img.size())
        tinted.lockFocus()
        img.drawAtPoint_fromRect_operation_fraction_(NSMakePoint(0, 0), AppKit.NSZeroRect, AppKit.NSCompositingOperationSourceOver, 1.0)
        _color((1, 1, 1)).set()
        AppKit.NSRectFillUsingOperation(NSMakeRect(0, 0, img.size().width, img.size().height),
                                        AppKit.NSCompositingOperationSourceAtop)
        tinted.unlockFocus()
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
        self.collapsed = False
        return self

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True  # フォーカスを取らずに、1回のクリックで押せるように

    def mouseDown_(self, event):
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        for r, cb in self.buttons:
            if cb and r[0] <= p.x <= r[0] + r[2] and r[1] <= p.y <= r[1] + r[3]:
                cb()
                return

    def drawRect_(self, rect):
        _color((0.08, 0.08, 0.1), 0.78).setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 10, 10).fill()
        s = self.state
        self.buttons = []
        if not s:
            return
        if s.get("camera", "running") != "running":  # カメラを止めている: No signal と、起動のボタンだけ
            msg = {"starting": "カメラを起動中…", "error": "カメラを起動できません"}.get(s["camera"], "No signal")
            _text(msg, 14, 10, 15, (0.9, 0.9, 0.9), bold=True)
            sub = {"stopped": f"{s.get('camera_reason') or ''}停止・ボタンか有効化で再開"}.get(s["camera"], "")
            _text(sub, 14, 30, 10, (0.7, 0.7, 0.7))
            if s["camera"] in ("stopped", "error"):
                r = (SMALL_W - BTN - 11, (SMALL_H - BTN) / 2, BTN, BTN)
                _draw_camera_button(r, slashed=False)
                self.buttons.append((r, self.on_button))
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
        max_w = COMPACT_W - tx - (2 * SBTN + 18 if not s["active"] else 8)
        _text_fit(self._mode_label(s), tx, 5, 14, mode_rgb if s["active"] else (0.7, 0.7, 0.7), max_w)
        sub = s["warning"] if s.get("warning") else f"手 {s.get('hand_label', '-')}  {s.get('config_label', '')}"
        _text_fit(sub, tx, 29, 10, (1.0, 0.45, 0.4) if s.get("warning") else (0.8, 0.8, 0.8), max_w)
        if not s["active"]:  # 待機中だけボタンを出す（操作中はクリックを下のウィンドウへ通す）
            y = (COMPACT_H - SBTN) / 2
            r_cam = (COMPACT_W - SBTN - 8, y, SBTN, SBTN)
            r_col = (COMPACT_W - 2 * SBTN - 13, y, SBTN, SBTN)
            _draw_camera_button(r_cam, slashed=True)
            _draw_collapse_button(r_col, collapsed=True)
            self.buttons += [(r_cam, self.on_button), (r_col, self.on_collapse)]

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
        _text(self._mode_label(s), ox + 38, ty + 2, 17, mode_rgb if s["active"] else (0.7, 0.7, 0.7), bold=True)
        pn = s["pinch_n"]
        _text((f"pinch {pn:.2f}" if not math.isnan(pn) else "pinch -") + f"   {s.get('config_label', '')}",
              ox + 38, ty + 22, 11, (0.85, 0.85, 0.85))
        _text(s["status"], ox, ty + 42, 11, (0.85, 0.85, 0.85))
        if s.get("warning"):
            _text(s["warning"], ox, ty + 58, 11, (1.0, 0.45, 0.4))
        if not s["active"]:  # 待機中だけ、カメラを止めるボタンと、畳むボタンを出す
            r_cam = (W - BTN - 10, ty, BTN, BTN)
            r_col = (W - 2 * BTN - 16, ty, BTN, BTN)
            _draw_camera_button(r_cam, slashed=True)
            _draw_collapse_button(r_col, collapsed=False)
            self.buttons += [(r_cam, self.on_button), (r_col, self.on_collapse)]


class _Ticker(NSObject):
    def initWithCallback_(self, cb):
        self = objc.super(_Ticker, self).init()
        if self is None:
            return None
        self.cb = cb
        return self

    def tick_(self, timer):
        self.cb()


class Overlay:
    def __init__(self, get_state: Callable[[], dict | None], tick: Callable[[], bool], hz: float = 30.0,
                 on_camera_button: Callable[[], None] | None = None, collapsed: bool = False,
                 on_collapse_change: Callable[[bool], None] | None = None):
        """tick() は描画のたびに呼ばれ、False を返すとイベントループを止める。
        on_camera_button: カメラのボタンが押されたとき（メインスレッド）。ボタンを出している間だけクリックを受け取る。
        collapsed: 畳んだ状態で始める。on_collapse_change: 畳む／広げるのボタンで切り替えたとき（状態の保存に使う）。"""
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
        self._screen = screen

        def on_tick():
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
            clickable = bool(s) and on_camera_button is not None and (
                (small and s.get("camera") in ("stopped", "error")) or (not small and not s.get("active", True)))
            if clickable == self.win.ignoresMouseEvents():
                self.win.setIgnoresMouseEvents_(not clickable)
            self.view.setNeedsDisplay_(True)

        self._ticker = _Ticker.alloc().initWithCallback_(on_tick)
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
