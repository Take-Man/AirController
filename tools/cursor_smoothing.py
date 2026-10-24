"""カーソルの平滑化（One Euro Filter）の係数を、記録で比べる（TASKS-20261025 2.3）。

手のひら中心を画面へ写した生の位置（raw）に、係数の候補ごとにフィルタをかけ、有効化中のカーソルモードのフレームで
  - 静止時の揺れ: 手がほぼ止まっている（基準の速さ < still_speed pt/s）フレームで、出力から基準を引いた細かい揺れの
    1フレームの動き（pt）。ゆっくりした本当の動きは基準に入るので、揺れだけが残る
  - 追従の遅れ: 動いている（基準の速さ > move_speed pt/s）フレームで、出力が基準からどれだけ遅れているか（ms）
を測る。基準は raw を前後対称に平滑化したもの（位相の遅れがない「本当の位置」の近似）。
遅れ = 出力と基準の距離 / 基準の速さ（その時点で何 ms 前の位置にいるか）。
ほかに、ピンチを始めたときのカーソルのずれ（ピンチが成立する前の 150ms で、指を寄せる動きでカーソルが動いた量）も出す。

操作範囲は固定（自動補正を切る。calibration.toml の値）。画面 1440x900pt。

使い方: .venv/bin/python tools/cursor_smoothing.py [recordings/*.jsonl ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aircontroller.__main__ import load_config  # noqa: E402
from aircontroller.filters import OneEuroFilter2D  # noqa: E402
from aircontroller.gesture import GestureConfig, GestureEngine  # noqa: E402
from aircontroller.recording import iter_frames  # noqa: E402
from aircontroller.types import PALM  # noqa: E402

CANDIDATES = [  # (名前, min_cutoff, beta)
    ("現状 1.0/0.01", 1.0, 0.01),
    ("G 0.3/0.01", 0.3, 0.01),
    ("H 0.2/0.02", 0.2, 0.02),
    ("A 0.5/0.01", 0.5, 0.01),
    ("B 0.5/0.02", 0.5, 0.02),
    ("C 0.7/0.015", 0.7, 0.015),
    ("D 0.3/0.03", 0.3, 0.03),
    ("E 1.0/0.02", 1.0, 0.02),
    ("F 1.5/0.01", 1.5, 0.01),
    ("フィルタなし", None, None),
]
STILL, MOVE = float(__import__("os").environ.get("STILL", 40)), 300.0  # pt/s


def smooth_ref(t: np.ndarray, x: np.ndarray, half_s: float = 0.1) -> np.ndarray:
    """前後 half_s 秒の三角窓の平均（位相の遅れなし）。"""
    out = np.empty_like(x)
    for i in range(len(t)):
        m = np.abs(t - t[i]) <= half_s
        w = 1.0 - np.abs(t[m] - t[i]) / (half_s + 1e-9)
        out[i] = (x[m] * w[:, None]).sum(axis=0) / w.sum()
    return out


def collect(path: Path, gcfg: GestureConfig):
    """手が見えているフレームの (時刻, raw, カーソルモードか, セグメント番号) と、ピンチ開始の時刻。"""
    eng = GestureEngine(gcfg)
    ts, raws, cur, seg = [], [], [], []
    pinch_t = []
    s, last_t = 0, None
    for f in iter_frames(path):
        ev = eng.update(f)
        pinch_t += [e.t_ns / 1e9 for e in ev if e.kind == "pinch_start"]
        h = eng.primary_hand(f)
        if h is None:
            continue
        t = f.t_capture_ns / 1e9
        if last_t is not None and t - last_t > 0.2:
            s += 1  # 見失いをまたいだら別の区間（フィルタの状態は続くが、基準と速さは区間ごとに）
        last_t = t
        palm = tuple(h.landmarks[list(PALM), :2].mean(axis=0))
        ts.append(t)
        raws.append(eng.map_to_screen(palm, f.image_size))
        cur.append(f.active and eng.state.mode == "cursor")
        seg.append(s)
    return np.array(ts), np.array(raws), np.array(cur), np.array(seg), np.array(pinch_t)


def run_filter(t, raw, mc, beta, dc=1.0):
    if mc is None:
        return raw.copy()
    f = OneEuroFilter2D(mc, beta, dc)
    return np.array([f(x, y, tt) for tt, (x, y) in zip(t, raw)])


def main(paths: list[str]) -> None:
    cfg = load_config(None)
    gcfg = GestureConfig.from_toml(cfg, (1440.0, 900.0))
    gcfg.roi_adapt = False
    data = [collect(Path(p), gcfg) for p in paths]
    print(f"記録 {len(paths)} 本、操作範囲 幅 {gcfg.roi_width:.3f}（カメラの 1px ≒ 画面の "
          f"{1440 / (gcfg.roi_width * 1280):.2f}pt）")
    print(f"{'候補 min_cutoff/beta':16s} {'静止時の揺れ pt/フレーム':>24s} {'追従の遅れ ms':>18s} {'ピンチ開始前150msのずれ pt':>26s}")
    print(f"{'':16s} {'中央値':>8s} {'p95':>7s} {'RMS':>7s} {'中央値':>8s} {'p90':>8s} {'中央値':>10s} {'p90':>8s}")
    for name, mc, beta in CANDIDATES:
        jit, lag, pre = [], [], []
        for t, raw, cur, seg, pinch_t in data:
            out = run_filter(t, raw, mc, beta)
            for s in np.unique(seg):
                m = seg == s
                if m.sum() < 10:
                    continue
                ts, rs, os_, cs = t[m], raw[m], out[m], cur[m]
                ref = smooth_ref(ts, rs)
                v = np.gradient(ref, ts, axis=0)
                sp = np.linalg.norm(v, axis=1)
                resid = os_ - ref  # 細かい揺れ（基準からのずれ）
                step = np.r_[0.0, np.linalg.norm(np.diff(resid, axis=0), axis=1)]
                still = cs & (sp < STILL)
                still[0] = False
                jit += list(step[still])
                mv = cs & (sp > MOVE)
                lag += list(np.linalg.norm(os_[mv] - ref[mv], axis=1) / sp[mv] * 1000)
            for tp in pinch_t:  # ピンチ開始時刻の位置と、150ms 前の位置の差（このずれた位置でクリックされる）
                i1 = np.searchsorted(t, tp)
                i0 = np.searchsorted(t, tp - 0.15)
                if 0 < i0 < i1 < len(t):
                    pre.append(float(np.linalg.norm(out[i1 - 1] - out[i0])))
        jit, lag, pre = map(np.array, (jit, lag, pre))
        print(f"{name:16s} {np.median(jit):8.2f} {np.percentile(jit, 95):7.2f} {np.sqrt((jit ** 2).mean()):7.2f} "
              f"{np.median(lag):8.0f} {np.percentile(lag, 90):8.0f} {np.median(pre):10.1f} {np.percentile(pre, 90):8.1f}")
    print(f"（静止: 基準の速さ < {STILL:.0f}pt/s のフレーム {len(jit)}、動き: > {MOVE:.0f}pt/s のフレーム {len(lag)}、"
          f"ピンチ {len(pre)} 回）")


if __name__ == "__main__":
    main(sys.argv[1:] or [str(p) for p in sorted((ROOT / "recordings").glob("use0*.jsonl"))]
         + [str(ROOT / "recordings/abs01.jsonl"), str(ROOT / "recordings/abs02.jsonl")])
