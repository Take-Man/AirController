"""lab-position の記録を、位置ごとに集計する（SPEC 13.41: 位置によって判定が崩れる件の原因の特定。解決はしない）。

記録を今の設定のジェスチャー層に流し（全体を有効化中として扱う）、ラベルの区間（位置/ジェスチャー）ごとに次を出す。
  - 手の検出率、モードの内訳
  - カーソル・ピースの区間: 正しいモードの割合と、意図しないピンチ（クリック・ドラッグになりうる）の回数
  - クリックの区間: ピンチの成立の回数（クリック・ドラッグ・取り消しの内訳）
  - 指の特徴量の中央値: 人差し指・中指・薬指の真っすぐさ、親指の畳み、手のひらの向き（+ が手の甲）、親指-人差し指の距離
    （最小と中央値）、人差し指と中指の先の画像上の距離、人差し指と中指の先の奥行きの差（world、カメラ方向に重なると大きい）
使い方: .venv/bin/python tools/position_report.py recordings/labpos-....jsonl
  （lab-position --suite edge の記録では、move = 中央との往復、hold = 静止、peace_move = ピースで指を動かす。これらの区間の
  ピンチ・クリック・ドラッグは、すべて意図しないもの（「誤」と表示）。手を見失った区間に注意）
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aircontroller.__main__ import load_config  # noqa: E402
from aircontroller.gesture import (GestureConfig, GestureEngine, finger_straightness, image_pinch_metrics,  # noqa: E402
                                   palm_facing, thumb_fold_metric)
from aircontroller.recording import iter_frames  # noqa: E402
from aircontroller.types import INDEX_MCP, INDEX_TIP, MIDDLE_MCP, MIDDLE_TIP, MIDDLE_MCP as MM, RING_MCP, WRIST  # noqa: E402

# 区間のジェスチャー → 正しいモード（None はピンチの回数だけを見る）。edge の move・hold・peace_move では、ピンチ・クリック・ドラッグは
# すべて意図しないもの
EXPECT = {"cursor": "cursor", "peace": "peace", "click": None, "hold": "cursor", "move": "cursor", "peace_move": "peace"}
UNINTENDED = {"cursor", "peace", "hold", "move", "peace_move"}


def main(path: str) -> None:
    p = Path(path)
    segs = json.loads(p.with_suffix(".labels.json").read_text())["segments"]
    gcfg = GestureConfig.from_toml(load_config(None), (1440.0, 900.0))
    gcfg.roi_adapt = False
    eng = GestureEngine(gcfg)
    per: dict[str, dict] = defaultdict(lambda: {"n": 0, "hand": 0, "modes": Counter(), "ev": Counter(), "f": defaultdict(list)})
    misfires = []
    t_first = None
    for f in iter_frames(p):
        t_first = t_first if t_first is not None else f.t_capture_ns
        f = f.__class__(**{**f.__dict__, "active": True})
        ev = eng.update(f)
        lab = next((s["label"] for s in segs if s["t_start_ns"] <= f.t_capture_ns < s["t_end_ns"]), None)
        if lab is None:
            continue
        d = per[lab]
        d["n"] += 1
        d["modes"][eng.state.mode] += 1
        for e in ev:
            if e.kind in ("pinch_start", "click", "drag_start", "pinch_cancel"):
                d["ev"][e.kind + (":" + e.data.get("button", "") if e.kind != "drag_start" else "")] += 1
                if e.kind == "pinch_start" and lab.split("/")[-1] in UNINTENDED:
                    hh = eng.primary_hand(f)
                    m = image_pinch_metrics(hh, f.image_size) if hh is not None else {}
                    cur = eng.state.cursor or (float("nan"), float("nan"))
                    misfires.append((lab, round((f.t_capture_ns - t_first) / 1e9, 1), e.data.get("button"),
                                     round(cur[0]), round(cur[1]), round(m.get("thumb_index_n", float("nan")), 3),
                                     round(m.get("thumb_middle_n", float("nan")), 3)))
        h = eng.primary_hand(f)
        if h is None:
            continue
        d["hand"] += 1
        w = h.world
        px = h.landmarks[:, :2] * np.asarray(f.image_size)
        ref = float(np.linalg.norm(px[MM] - px[WRIST])) or 1.0
        F = d["f"]
        F["人"].append(finger_straightness(w, INDEX_MCP))
        F["中"].append(finger_straightness(w, MIDDLE_MCP))
        F["薬"].append(finger_straightness(w, RING_MCP))
        F["親畳"].append(thumb_fold_metric(h))
        F["甲"].append(palm_facing(h))
        F["親人"].append(image_pinch_metrics(h, f.image_size)["thumb_index_n"])
        F["人中先"].append(float(np.linalg.norm(px[INDEX_TIP] - px[MIDDLE_TIP])) / ref)
        F["奥差"].append(abs(float(w[INDEX_TIP, 2] - w[MIDDLE_TIP, 2])) / (float(np.linalg.norm(w[MM] - w[WRIST])) or 1.0))
        F["x"].append(float(h.landmarks[[0, 5, 9, 13, 17], 0].mean()))
        F["y"].append(float(h.landmarks[[0, 5, 9, 13, 17], 1].mean()))
    gestures = sorted({k.split("/")[1] for k in per if "/" in k}, key=lambda g: list(EXPECT).index(g) if g in EXPECT else 9)
    for g in gestures:
        print(f"\n== {g}" + (f"（正しいモード: {EXPECT[g]}）" if EXPECT.get(g) else "（ピンチの回数を見る）"))
        print(f"{'位置':10s} {'手':>4s} {'正しい':>6s} {'モードの内訳':34s} {'ピンチ・クリック':28s} "
              f"{'人':>5s}{'中':>5s}{'薬':>5s}{'親畳':>6s}{'甲':>6s}{'親人min':>8s}{'親人':>6s}{'人中先':>7s}{'奥差':>6s}  手のひら(x,y)")
        for lab, d in per.items():
            if not lab.endswith("/" + g):
                continue
            n = max(1, d["n"])
            exp = EXPECT.get(g)
            ok = f"{d['modes'][exp] / n * 100:5.0f}%" if exp else "    -"
            modes = " ".join(f"{m}{c / n * 100:.0f}%" for m, c in d["modes"].most_common(3))
            evs = " ".join(f"{k}{v}" for k, v in sorted(d["ev"].items())) or "なし"
            if g in UNINTENDED and d["ev"]:
                evs = "誤 " + evs  # 意図しないピンチ・クリック・ドラッグ
            F = d["f"]
            med = lambda k: float(np.median(F[k])) if F[k] else float("nan")  # noqa: E731
            mn = float(np.min(F["親人"])) if F["親人"] else float("nan")
            print(f"{lab.split('/')[0]:10s} {d['hand'] / n * 100:3.0f}% {ok:>6s} {modes:34s} {evs:28s} "
                  f"{med('人'):5.2f}{med('中'):5.2f}{med('薬'):5.2f}{med('親畳'):+6.2f}{med('甲'):+6.2f}{mn:8.2f}"
                  f"{med('親人'):6.2f}{med('人中先'):7.2f}{med('奥差'):6.2f}  ({med('x'):.2f}, {med('y'):.2f})")
    if misfires:
        print("\n[意図しないピンチ] 区間、記録の先頭からの秒、ボタン、そのときのカーソル（画面 pt、1440x900）、親指-人差し指、親指-中指の距離")
        for m in misfires:
            print("  ", m)
    print("\n列: 手 = 手の検出率、人・中・薬 = 指の真っすぐさ、親畳 = 親指の畳み（+ で畳む）、甲 = 手のひらの向き（+ が手の甲）、"
          "親人 = 親指-人差し指の距離（成立 %.3f）、人中先 = 人差し指と中指の先の画像上の距離、奥差 = その奥行きの差" % gcfg.pinch_on)


if __name__ == "__main__":
    main(sys.argv[1])
