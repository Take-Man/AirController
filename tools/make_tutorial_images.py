"""チュートリアルの画像を、作者の手の写真から作る（TASKS-20261029-tutorial-images、SPEC 13.60）。

ref-image/ref-<名前>.jpg（本人の目線の右手）から MediaPipe で骨格（ワールド座標）を取り出し、設定画面のライブの骨格と
同じ見た目（骨・関節・指先の色）で描く。動き（移動・矢印・集中線）は、基本の形をずらして線を足すだけで作る。
  - 大きさ: 画像の高さ = SPAN_M（0.22 m）に固定（1 枚ごとの拡大・縮小はしない）。中心は手の重心。向きは写真のまま。
  - 形ごとの回転（見やすさの調整用）は ROTATE、どの画像に何を描くかは PLAN で変える。効果は draw_* の関数。
  - 奥行き: 奥の骨から順に描き、手前の骨・関節を少し太く・大きくする。
出力: aircontroller/web/tutorial/img/<id>-<番号>.png と <id>-<番号>-dark.png（960×720、背景は透明。ライト・ダークの色）と、
確認用のコンタクトシート logs/tutorial-images-sheet.png・tutorial-images-sheet-dark.png。
使い方: .venv/bin/python tools/make_tutorial_images.py [id ...]（id を渡すと、その操作だけ作り直す）
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent.parent
REF_DIR = ROOT / "ref-image"
MODEL = ROOT / "models" / "hand_landmarker.task"
OUT_DIR = ROOT / "aircontroller" / "web" / "tutorial" / "img"
SHEET = ROOT / "logs" / "tutorial-images-sheet.png"

W, H = 960, 720
SS = 2  # 縦横 2 倍で描いて縮める（線のギザギザを消す）
SPAN_M = 0.22  # 画像の高さに入る長さ（m）。0.30 では手が小さく見えたので詰めた
PX_PER_M = H / SPAN_M

# 色は設定画面（app.css の .fig .bone / .joint / .tip、ライトの値）に合わせる
# 背景は透明（円やカードの背景 --panel・--soft がそのまま見える）。テーマごとに色を変えた 2 組を作る（ダークは <id>-<番号>-dark.png）
THEMES = {
    "": {"bone": "#1f6feb", "joint": "#1d1d1b", "tip": "#c2410c",  # --accent / --fg / --measured（ライト）
         "effect": "#8db4f5",  # 効果の線: 骨の色を背景に寄せた薄い青
         "desk": "#77776f", "key": "#ffffff", "key_shadow": "#e2e1dc", "sheet": "#efeee9"},  # --saved / --panel / --line / --soft
    "-dark": {"bone": "#5b9bff", "joint": "#ecebe6", "tip": "#fb923c",  # 同じ変数のダークの値
              "effect": "#3a64a8", "desk": "#a8a8a0", "key": "#1f1f1d", "key_shadow": "#34342f", "sheet": "#2a2a27"},
}
C = THEMES[""]  # 今描いているテーマの色
BONE_W, JOINT_R, TIP_R, EFFECT_W = 9, 7, 12, 6  # 960×720 での太さ・半径（px）
DEPTH_GAIN = 3.0  # 重心より 1 cm 手前で 3% 太く・大きく
DEPTH_CLAMP = (0.75, 1.3)

BONES = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14),
         (14, 15), (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17)]  # aircontroller/web/app.js と同じ
TIPS = (4, 8, 12, 16, 20)

# 形ごとの回転（度）。pitch = 左右の軸（+ で指先が奥へ倒れる）、yaw = 上下の軸、roll = 画面に垂直な軸
ROTATE: dict[str, dict[str, float]] = {
    "click": {"pitch": -35, "yaw": 20},  # つまんだ輪と、曲げた指が分かれて見える
    "fist": {"pitch": 60},  # 手の甲側から。指の付け根の並びが見える
    "thumbsup": {"yaw": 20},
    "peace-down": {"roll": -30, "pitch": -40},  # 横から撮った写真。2 本の指が下向きに分かれて見える
}

# 移動の量（画像の幅・高さに対する割合）
MOVE = {"右上": (0.20, -0.17), "左下": (-0.20, 0.17), "右": (0.22, 0.0), "左": (-0.22, 0.0), "上": (0.0, -0.05), "上寄せ": (0.0, -0.04), "下": (0.0, 0.22)}

# 画像ごとの内容: (手の写真, 移動, 効果のリスト)。効果: ("streak",) 移動の流線、("burst", a, b) 指先 a と b の中点に集中線、
# ("arrow", "up"|"down") 縦の矢印、("depth", "toward"|"away") 指先の上の山なりの矢印（ズーム）、("desk",) 机の天板、("command",) Command キー（手なし）
PLAN: dict[str, list[tuple[str | None, str | None, list[tuple]]]] = {
    "cursor": [("cursor", None, []), ("cursor", "右上", [("streak",)]), ("cursor", "左下", [("streak",)])],
    "click": [("cursor", None, []), ("click", None, [("burst", 4, 8)]), ("cursor", None, [])],
    "drag": [("cursor", None, []), ("click", None, []), ("click", "右上", [("streak",)])],
    "right_click": [("cursor", None, []), ("rc-pre", None, []), ("rc", None, [("burst", 4, 12)])],
    "scroll": [("peace", None, []), ("peace-up", None, [("arrow", "up")]), ("peace-down", None, [("arrow", "down")])],
    "nav": [("peace", None, []), ("peace", "右", [("streak",)]), ("peace", "左", [("streak",)])],
    "desktop": [("thumbsup", None, []), ("thumbsup", "左", [("streak",)]), ("thumbsup", "右", [("streak",)])],
    "mission": [("thumbsup", None, []), ("thumbsup", "上", [("streak",)])],
    "zoom": [("zoom-normal", "上寄せ", []), ("zoom-beckon", None, [("depth", "toward")]), ("zoom-push", "上寄せ", [("depth", "away")])],
    "wake": [("palm", None, []), ("fist", None, []), ("fist", "下", [("desk",), ("streak",)]), (None, None, [("command",)])],
}


# ---- 骨格の取り出し ----

def _landmarker():
    opt = vision.HandLandmarkerOptions(base_options=BaseOptions(model_asset_path=str(MODEL)), running_mode=vision.RunningMode.IMAGE,
                                       num_hands=1, min_hand_detection_confidence=0.1, min_hand_presence_confidence=0.1,
                                       min_tracking_confidence=0.1)
    return vision.HandLandmarker.create_from_options(opt)


def detect(det, name: str) -> np.ndarray:
    """写真から手のワールド座標（21×3、m。x 右、y 下、z 奥）を返す。見つからなければ左右反転して試し、それでも無ければ止める。"""
    img = ImageOps.exif_transpose(Image.open(REF_DIR / f"ref-{name}.jpg")).convert("RGB")
    for flip in (False, True):
        im = ImageOps.mirror(img) if flip else img
        r = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.asarray(im)))
        if r.hand_world_landmarks:
            p = np.array([[q.x, q.y, q.z] for q in r.hand_world_landmarks[0]])
            if flip:
                p[:, 0] *= -1
            return p
    raise SystemExit(f"ref-{name}.jpg: 手が見つかりません")


def rotate(p: np.ndarray, rot: dict[str, float]) -> np.ndarray:
    c = p.mean(axis=0)
    a, b, g = (math.radians(rot.get(k, 0.0)) for k in ("pitch", "yaw", "roll"))
    rx = np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
    ry = np.array([[math.cos(b), 0, math.sin(b)], [0, 1, 0], [-math.sin(b), 0, math.cos(b)]])
    rz = np.array([[math.cos(g), -math.sin(g), 0], [math.sin(g), math.cos(g), 0], [0, 0, 1]])
    return (p - c) @ (rz @ ry @ rx).T + c


# ---- 描画 ----

class Canvas:
    """SS 倍で描く。座標・太さは 960×720 の単位で受け取る。"""

    def __init__(self):
        self.im = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.im)

    def line(self, a, b, color, width):
        w = width * SS
        pa, pb = (a[0] * SS, a[1] * SS), (b[0] * SS, b[1] * SS)
        self.d.line([pa, pb], fill=color, width=round(w))
        for x, y in (pa, pb):  # 端を丸く
            self.d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=color)

    def dot(self, c, r, color):
        x, y = c[0] * SS, c[1] * SS
        self.d.ellipse([x - r * SS, y - r * SS, x + r * SS, y + r * SS], fill=color)

    def polygon(self, pts, color):
        self.d.polygon([(x * SS, y * SS) for x, y in pts], fill=color)

    def result(self) -> Image.Image:
        return self.im.resize((W, H), Image.LANCZOS)


def project(p: np.ndarray, offset=(0.0, 0.0)) -> tuple[np.ndarray, np.ndarray]:
    """画像上の点（px）と、奥行きの倍率（手前ほど大きい）を返す。手の重心が画像の中心 + offset に来る。"""
    c = p.mean(axis=0)
    xy = (p[:, :2] - c[:2]) * PX_PER_M + np.array([W / 2 + offset[0] * W, H / 2 + offset[1] * H])
    s = np.clip(1 - (p[:, 2] - c[2]) * DEPTH_GAIN, *DEPTH_CLAMP)  # z は m。1 cm 手前で DEPTH_GAIN %
    return xy, s


def draw_hand(cv: Canvas, xy: np.ndarray, s: np.ndarray, z: np.ndarray, alpha_mask=None):
    """奥の骨から順に描き、関節・指先も奥から順に重ねる。"""
    items = [((z[a] + z[b]) / 2, "bone", (a, b)) for a, b in BONES] + [(z[i] + 1e-4, "joint", i) for i in range(21)]
    for _, kind, k in sorted(items, key=lambda t: -t[0]):
        if kind == "bone":
            a, b = k
            cv.line(xy[a], xy[b], C["bone"], BONE_W * (s[a] + s[b]) / 2)
        else:
            cv.dot(xy[k], (TIP_R if k in TIPS else JOINT_R) * s[k], C["tip"] if k in TIPS else C["joint"])


def hand_extent(xy: np.ndarray, direction) -> float:
    """手の重心から direction（単位ベクトル）の向きに、手がどこまで広がっているか（px）。"""
    c = xy.mean(axis=0)
    return float(max((xy - c) @ np.asarray(direction)))


def draw_streak(cv: Canvas, xy: np.ndarray, move):
    """流線: 動いてきた側（移動の逆向き）へ、手の端から平行な線を 4 本。長さは手の大きさの 1/2 程度で、少しずつずらす。"""
    d = np.array(move, dtype=float) * (W, H)
    d /= np.linalg.norm(d)
    back, perp = -d, np.array([-d[1], d[0]])
    c = xy.mean(axis=0)
    size = float(np.ptp(xy, axis=0).max())
    start = hand_extent(xy, back) + 26
    for k, (off, ln) in enumerate(zip((-1.5, -0.5, 0.5, 1.5), (0.40, 0.55, 0.50, 0.35))):
        p0 = c + back * (start + 10 * (k % 2)) + perp * off * 34
        cv.line(p0, p0 + back * size * ln, C["effect"], EFFECT_W)


def draw_burst(cv: Canvas, center, r_in=36, r_out=66, n=8, phase=math.pi / 8):
    """集中線: 点から少し離して、短い線を放射状に n 本。"""
    for k in range(n):
        t = phase + 2 * math.pi * k / n
        u = np.array([math.cos(t), math.sin(t)])
        cv.line(np.asarray(center) + u * r_in, np.asarray(center) + u * r_out, C["tip"], EFFECT_W)


def draw_arrow(cv: Canvas, tail, head, w_tail, w_head, color=None):
    """太さが変わる矢印（先端の三角を含む）。"""
    color = color or C["effect"]
    tail, head = np.asarray(tail, float), np.asarray(head, float)
    d = (head - tail) / np.linalg.norm(head - tail)
    n = np.array([-d[1], d[0]])
    hw = max(w_head * 1.3, 20)  # 矢じりの半幅（細い先でも見えるように）
    hl = hw * 1.7  # 矢じりの長さ
    neck = head - d * hl
    cv.polygon([tail + n * w_tail / 2, neck + n * w_head / 2, neck - n * w_head / 2, tail - n * w_tail / 2], color)
    cv.polygon([neck + n * hw, head, neck - n * hw], color)


def draw_vertical_arrow(cv: Canvas, xy: np.ndarray, way: str):
    """スクロール: 手の右横に縦の矢印。"""
    x = hand_extent(xy, (1, 0)) + xy.mean(axis=0)[0] + 60
    x = min(x, W - 70)
    y0, y1 = (H / 2 + 120, H / 2 - 120) if way == "up" else (H / 2 - 120, H / 2 + 120)
    draw_arrow(cv, (x, y0), (x, y1), 18, 18)


def draw_depth_arrow(cv: Canvas, xy: np.ndarray, way: str):
    """ズーム: 人差し指から小指の指先（8・12・16・20）の上に、山なりの矢印。指先は左奥と右手前の向きに動くので、
    左端（奥）は細く、右端（手前）は太くする。toward = 右手前へ（手招き、イン）、away = 左奥へ（画面側へ、アウト）。"""
    tips = xy[[8, 12, 16, 20]]
    x0, x1 = tips[:, 0].min() - 50, tips[:, 0].max() + 70
    y = max(tips[:, 1].min() - 55, 120)
    p0, ctrl, p1 = np.array([x0, y + 10]), np.array([(x0 + x1) / 2, y - 110]), np.array([x1, y + 10])
    w0, w1 = 6, 30  # 左端（奥）と右端（手前）の太さ
    hw = 26 if way == "toward" else 18  # 矢じりの半幅
    hl = hw * 1.6
    ts = np.linspace(0, 1, 60)
    pts = np.array([(1 - t) ** 2 * p0 + 2 * (1 - t) * t * ctrl + t ** 2 * p1 for t in ts])
    seg = np.cumsum(np.r_[0, np.linalg.norm(np.diff(pts, axis=0), axis=1)])
    keep = seg <= seg[-1] - hl if way == "toward" else seg >= hl  # 矢じりの分だけ胴を短く
    body, wid = pts[keep], np.interp(ts[keep], (0, 1), (w0, w1))
    tan = np.gradient(body, axis=0)
    nrm = np.stack([-tan[:, 1], tan[:, 0]], axis=1) / np.linalg.norm(tan, axis=1)[:, None]
    cv.polygon(list(body + nrm * wid[:, None] / 2) + list((body - nrm * wid[:, None] / 2)[::-1]), C["effect"])
    end, neck = (p1, body[-1]) if way == "toward" else (p0, body[0])
    d = (end - neck) / np.linalg.norm(end - neck)
    n = np.array([-d[1], d[0]])
    cv.polygon([neck + n * hw, end, neck - n * hw], C["effect"])


def draw_desk(cv: Canvas, y: float):
    cv.line((90, y), (W - 90, y), C["desk"], 10)


def draw_command_key(cv: Canvas):
    """Command キー: 角丸の四角のキーの中に ⌘。右上の角にタップの集中線。"""
    size, r = 300, 46
    x0, y0 = (W - size) / 2, (H - size) / 2
    box = [x0 * SS, y0 * SS, (x0 + size) * SS, (y0 + size) * SS]
    cv.d.rounded_rectangle([box[0] + 10 * SS, box[1] + 14 * SS, box[2] + 10 * SS, box[3] + 14 * SS], r * SS, fill=C["key_shadow"])  # 影
    cv.d.rounded_rectangle(box, r * SS, fill=C["key"], outline=C["bone"], width=9 * SS)
    font = ImageFont.truetype("/System/Library/Fonts/Apple Symbols.ttf", 190 * SS)
    cv.d.text(((x0 + size / 2) * SS, (y0 + size / 2 - 18) * SS), "⌘", font=font, fill=C["joint"], anchor="mm")
    small = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 34 * SS)
    cv.d.text(((x0 + size - 26) * SS, (y0 + size - 22) * SS), "command", font=small, fill=C["joint"], anchor="rs")
    draw_burst(cv, (x0 + size / 2, y0 + size / 2), r_in=size * 0.62, r_out=size * 0.62 + 50, n=8)


def render(p: np.ndarray | None, move: str | None, effects: list[tuple]) -> Image.Image:
    cv = Canvas()
    if p is None:
        for e in effects:
            if e[0] == "command":
                draw_command_key(cv)
        return cv.result()
    off = MOVE[move] if move else (0.0, 0.0)
    xy, s = project(p, off)
    desk_y = H / 2 + 40
    for e in effects:  # 手より奥に描く効果
        if e[0] == "streak":
            draw_streak(cv, xy, off)
        elif e[0] == "arrow":
            draw_vertical_arrow(cv, xy, e[1])
        elif e[0] == "depth":
            draw_depth_arrow(cv, xy, e[1])
    draw_hand(cv, xy, s, p[:, 2])
    for e in effects:  # 手より手前に描く効果
        if e[0] == "burst":
            draw_burst(cv, (xy[e[1]] + xy[e[2]]) / 2)
        elif e[0] == "desk":
            # 天板より下は、不透明度を下げて薄くする（机の下に入った手）
            y = round(desk_y * SS)
            below = cv.im.crop((0, y, W * SS, H * SS))
            below.putalpha(below.getchannel("A").point(lambda a: round(a * 0.28)))
            cv.im.paste(below, (0, y))
            draw_desk(cv, desk_y)
    return cv.result()


def contact_sheet(paths: list[Path], out: Path):
    """テーマの背景（--soft）の上に並べる。"""
    tw, th, cols, pad, lab = 320, 240, 6, 10, 26
    rows = math.ceil(len(paths) / cols)
    sheet = Image.new("RGB", (cols * (tw + pad) + pad, rows * (th + lab + pad) + pad), C["sheet"])
    d = ImageDraw.Draw(sheet)
    font = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 18)
    for i, f in enumerate(paths):
        x, y = pad + (i % cols) * (tw + pad), pad + (i // cols) * (th + lab + pad)
        im = Image.open(f).resize((tw, th), Image.LANCZOS)
        sheet.paste(im, (x, y), im)
        d.rectangle([x, y, x + tw - 1, y + th - 1], outline=C["key_shadow"])
        d.text((x + 2, y + th + 3), f.stem, font=font, fill=C["joint"])
    out.parent.mkdir(exist_ok=True)
    sheet.save(out)
    print(f"→ {out.relative_to(ROOT)}")


def main(ids: list[str]):
    global C
    det = _landmarker()
    cache: dict[str, np.ndarray] = {}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for sid in ids or PLAN:
        for k, (name, move, effects) in enumerate(PLAN[sid], 1):
            p = None
            if name:
                if name not in cache:
                    cache[name] = rotate(detect(det, name), ROTATE.get(name, {}))
                p = cache[name]
            for suffix, C in THEMES.items():
                out = OUT_DIR / f"{sid}-{k}{suffix}.png"
                render(p, move, effects).save(out)
            print(f"{sid}-{k}(-dark).png  ref-{name or '-'} {move or ''} {' '.join(e[0] for e in effects)}")
    for suffix, C in THEMES.items():
        contact_sheet([OUT_DIR / f"{sid}-{k}{suffix}.png" for sid in PLAN for k in range(1, len(PLAN[sid]) + 1)],
                      SHEET.with_name(f"{SHEET.stem}{suffix}.png"))


if __name__ == "__main__":
    main(sys.argv[1:])
