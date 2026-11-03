"""カメラのプレビューのウィンドウ（AppKit。SPEC 13.68）: 測定（calibrate）と lab・learn-* の記録で、映像に骨格・目印・指示を重ねて出す。

OpenCV の imshow / waitKey の代わり。描くのは Pillow（draw_* と put_jp）、出すのは NSWindow の NSImageView。
show() は画像を出してからイベントを処理し、押されたキー（q・Esc など。無ければ -1）を返す。ウィンドウを閉じたら Esc と同じに扱う。
呼ぶのはメインスレッドから（AppKit の決まり）。
"""
from __future__ import annotations

import numpy as np

ESC = 27
_JP_FONT = "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc"


class Preview:
    def __init__(self, title: str):
        import AppKit

        self._ak = AppKit
        self.app = AppKit.NSApplication.sharedApplication()
        self.app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
        self.app.finishLaunching()
        self.title = title
        self.window = None
        self.view = None
        self._closed = False

    def _open(self, w: int, h: int) -> None:
        ak = self._ak
        style = ak.NSWindowStyleMaskTitled | ak.NSWindowStyleMaskClosable | ak.NSWindowStyleMaskMiniaturizable
        win = ak.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(((0, 0), (w, h)), style,
                                                                               ak.NSBackingStoreBuffered, False)
        win.setTitle_(self.title)
        win.setReleasedWhenClosed_(False)
        view = ak.NSImageView.alloc().initWithFrame_(((0, 0), (w, h)))
        view.setImageScaling_(ak.NSImageScaleProportionallyUpOrDown)
        win.setContentView_(view)
        win.center()
        win.makeKeyAndOrderFront_(None)
        self.app.activateIgnoringOtherApps_(True)
        self.window, self.view = win, view

    def show(self, rgb: np.ndarray, wait_s: float = 0.001) -> int:
        """RGB の画像（h × w × 3、uint8）を出す。押されたキーのコード（q は 113、Esc は 27）を返す。無ければ -1。"""
        import Quartz

        ak = self._ak
        h, w = rgb.shape[:2]
        if self.window is None:
            self._open(w, h)
        data = np.ascontiguousarray(rgb, dtype=np.uint8).tobytes()
        provider = Quartz.CGDataProviderCreateWithCFData(ak.NSData.dataWithBytes_length_(data, len(data)))
        cg = Quartz.CGImageCreate(w, h, 8, 24, w * 3, Quartz.CGColorSpaceCreateDeviceRGB(), Quartz.kCGImageAlphaNone,
                                  provider, None, False, Quartz.kCGRenderingIntentDefault)
        self.view.setImage_(ak.NSImage.alloc().initWithCGImage_size_(cg, (w, h)))
        return self._pump(wait_s)

    def _pump(self, wait_s: float) -> int:
        ak = self._ak
        key = -1
        until = ak.NSDate.dateWithTimeIntervalSinceNow_(wait_s)
        while True:
            ev = self.app.nextEventMatchingMask_untilDate_inMode_dequeue_(ak.NSEventMaskAny, until,
                                                                           ak.NSDefaultRunLoopMode, True)
            if ev is None:
                break
            until = ak.NSDate.distantPast()
            if ev.type() == ak.NSEventTypeKeyDown and ev.window() == self.window:
                chars = ev.charactersIgnoringModifiers()
                if chars:
                    key = ord(chars[0])
                continue  # 送らない（ビープを鳴らさない）
            self.app.sendEvent_(ev)
        self.app.updateWindows()
        if self.window is not None and not self.window.isVisible():
            self._closed = True
        return ESC if self._closed else key

    def close(self) -> None:
        if self.window is not None:
            self.window.orderOut_(None)
            self.window.close()
            self._pump(0.0)
            self.window = None


def mirror(rgb: np.ndarray) -> np.ndarray:
    """表示だけ鏡像にする（記録・ジェスチャー層は非反転の座標）。"""
    return np.ascontiguousarray(rgb[:, ::-1])


def to_pil(rgb: np.ndarray):
    from PIL import Image

    return Image.fromarray(rgb)


def draw_hand(draw, size: tuple[int, int], hand) -> None:
    """骨格（線は緑、点は赤）。鏡像表示に合わせて x を反転する。"""
    from mediapipe.tasks.python import vision

    w, h = size
    pts = [(int((1.0 - x) * w), int(y * h)) for x, y, _ in hand.landmarks]
    for c in vision.HandLandmarksConnections.HAND_CONNECTIONS:
        draw.line([pts[c.start], pts[c.end]], fill=(0, 255, 0), width=2)
    for x, y in pts:
        draw.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(255, 0, 0))


def draw_circle(draw, center: tuple[int, int], r: int, color: tuple[int, int, int], width: int = 0) -> None:
    """width が 0 なら塗りつぶし。"""
    x, y = center
    box = [x - r, y - r, x + r, y + r]
    if width:
        draw.ellipse(box, outline=color, width=width)
    else:
        draw.ellipse(box, fill=color)


def put_jp(img, lines: list[str], size: int = 30) -> None:
    """画像（Pillow）の上部に日本語の文字を描く。1 行目を大きく、2 行目からは小さく。"""
    from PIL import ImageDraw, ImageFont

    try:
        font = ImageFont.truetype(_JP_FONT, size)
        small = ImageFont.truetype(_JP_FONT, int(size * 0.7))
    except OSError:
        font = small = ImageFont.load_default()
    draw = ImageDraw.Draw(img)
    y = 12
    for i, line in enumerate(lines):
        f = font if i == 0 else small
        draw.text((14, y), line, font=f, fill=(255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0))
        y += int((size if i == 0 else size * 0.7) * 1.45)
