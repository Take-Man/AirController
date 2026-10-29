"""アプリのアイコンを、作者の元画像から作る（TASKS-20261029-play-icon 第3節、SPEC 13.66）。

icon-source/AirController-icon-1024.png（ChatGPT で作った正方形の絵。今のものは 1254×1254）を、1024×1024 に縮めてから
macOS のアイコンの形に整える: 1024 の中に 824 の角丸四角（角丸の半径 185）、外側は透明。絵は切り取らず、全体を 824 に縮める。
元画像の四隅がすでに透明なら（角丸・余白がある絵）、形は整えずにそのまま使う。
出力:
  icon-source/AirController-icon-macos.png  形を整えた 1024×1024（確認用、アプリの中で出すとき用）
  pkg/AirController.icns                     iconutil で icon.iconset（16・32・128・256・512 の 1x と 2x）から
使い方: .venv/bin/python tools/make_icon.py [元画像]
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "icon-source" / "AirController-icon-1024.png"
SHAPED = ROOT / "icon-source" / "AirController-icon-macos.png"
ICNS = ROOT / "pkg" / "AirController.icns"
ICONSET = ROOT / "pkg" / "build" / "icon" / "AirController.iconset"

SIZE, BODY, RADIUS = 1024, 824, 185  # macOS のアイコンの格子（Big Sur 以降）
SS = 4  # 角丸の縁は 4 倍で描いて縮める（ギザギザを消す）


def has_margin(im: Image.Image) -> bool:
    """四隅が透明なら、すでに角丸・余白のある絵。"""
    if im.mode != "RGBA":
        return False
    w, h = im.size
    return all(im.getpixel(p)[3] < 16 for p in [(2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3)])


def shape(im: Image.Image) -> Image.Image:
    body = im.convert("RGB").resize((BODY, BODY), Image.LANCZOS)
    mask = Image.new("L", (BODY * SS, BODY * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, BODY * SS - 1, BODY * SS - 1], radius=RADIUS * SS, fill=255)
    mask = mask.resize((BODY, BODY), Image.LANCZOS)
    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    off = (SIZE - BODY) // 2
    out.paste(body, (off, off), mask)
    return out


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else SRC
    im = Image.open(src)
    print(f"元画像: {src.relative_to(ROOT) if src.is_relative_to(ROOT) else src}  {im.size[0]}×{im.size[1]}  {im.mode}")
    if im.size[0] != im.size[1]:
        print("正方形ではありません")
        return 1
    if has_margin(im):
        icon = im.convert("RGBA").resize((SIZE, SIZE), Image.LANCZOS)
        print("四隅が透明なので、形は整えずにそのまま使います")
    else:
        icon = shape(im)
        print(f"全面に絵があるので、{BODY} の角丸四角（半径 {RADIUS}）に整えました")
    icon.save(SHAPED)
    shutil.rmtree(ICONSET, ignore_errors=True)
    ICONSET.mkdir(parents=True)
    for s in [16, 32, 128, 256, 512]:
        icon.resize((s, s), Image.LANCZOS).save(ICONSET / f"icon_{s}x{s}.png")
        icon.resize((s * 2, s * 2), Image.LANCZOS).save(ICONSET / f"icon_{s}x{s}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICNS)], check=True)
    print(f"出力: {SHAPED.relative_to(ROOT)}  {ICNS.relative_to(ROOT)}（{ICNS.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
