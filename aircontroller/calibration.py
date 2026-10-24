"""校正の計算（カメラや OS に依存しない部分）。

ピンチの閾値: 普段のカーソル操作の姿勢（親指を楽にした状態）と、摘まんだ状態の距離を記録し、
その間に成立・解除の閾値を置く。「指先が近いが触れていない」姿勢の距離は人によって違うので、
固定の閾値（0.15 / 0.22）では誤認が出ていた（SPEC 13.9）。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class PinchThresholds:
    on: float
    off: float
    drag_off: float
    pinch_hi: float  # 摘まんだ状態の上側（p90）
    rest_lo: float  # 普段の姿勢の下側（p2）
    separated: bool  # 2つの分布が分かれているか

    def summary(self, name: str) -> str:
        state = "分離できた" if self.separated else "分布が重なっている（既定値を使う）"
        return (f"{name}: 摘まんだ状態 p90 {self.pinch_hi:.3f} / 普段の姿勢 p2 {self.rest_lo:.3f} → {state}  "
                f"成立 {self.on:.3f}  解除 {self.off:.3f}" + (f"  ドラッグ中の解除 {self.drag_off:.3f}" if self.drag_off else ""))


def derive_pinch_thresholds(rest: list[float], pinch: list[float], default_on: float, default_off: float,
                            default_drag_off: float = 0.0) -> PinchThresholds:
    """摘まんだ状態の p90 と、普段の姿勢の p2 の間を区切る。

    成立は間の 40%、解除は 75%、ドラッグ中の解除は 95% の位置。間が広いときは 0.3 として扱う
    （大きく開かないと離したことにならない、を防ぐ）。分布が重なっているときは既定値を返す。
    """
    r = np.asarray([v for v in rest if np.isfinite(v)])
    p = np.asarray([v for v in pinch if np.isfinite(v)])
    if len(r) < 10 or len(p) < 10:
        raise ValueError(f"データが足りません（普段の姿勢 {len(r)}、摘まんだ状態 {len(p)} フレーム）")
    hi, lo = float(np.percentile(p, 90)), float(np.percentile(r, 2))
    if hi >= lo:
        return PinchThresholds(default_on, default_off, default_drag_off, hi, lo, False)
    gap = min(lo - hi, 0.3)
    return PinchThresholds(round(hi + 0.40 * gap, 4), round(hi + 0.75 * gap, 4),
                           round(hi + 0.95 * gap, 4) if default_drag_off else 0.0, hi, lo, True)


def derive_thumb_fold(cursor: list[float], peace: list[float], default_on: float, default_off: float) -> PinchThresholds:
    """親指を畳んだ判定の閾値。こちらは値が大きいほど「畳んだ」なので、符号を反転して同じ計算をする。"""
    th = derive_pinch_thresholds([-v for v in cursor], [-v for v in peace], -default_on, -default_off)
    return PinchThresholds(-th.on, -th.off, 0.0, -th.pinch_hi, -th.rest_lo, th.separated)


def neutral_direction(angles: list[tuple[float, float]]) -> tuple[float, float]:
    """ピースで楽に正面を指したときの (方位角, 仰角) の中央値。"""
    a = np.asarray(angles, dtype=np.float64)
    if len(a) < 10:
        raise ValueError(f"データが足りません（{len(a)} フレーム）")
    return float(np.median(a[:, 0])), float(np.median(a[:, 1]))


@dataclass
class LearnResult:
    params: dict
    report: str


def learn_thumbsup(frames, segments: list[dict]) -> LearnResult:
    """いいねモードの学習（SPEC 13.18）。frames はラベル付きの記録、segments は区間（label, t_start_ns, t_end_ns）。

    - 姿勢: 親指先と手のひら中心の距離（thumb_far）の閾値を、いいねと間違えやすい形（neg_*）の間に置く。
    - 方向: 各フレームの (右への変位, 上への変位, 親指の角度の変化, 付け根の線の角度の変化) を正面（center の中央値）
      から求め、軸ごとに「変位 + 重み × 角度/45」の重みを格子探索で選ぶ。選ぶ基準は、正面の揺れ（p95）を単位にした
      両方向の余裕（小さい方）から、反対の軸へのはみ出しを引いたもの（重みを大きくして全体を拡大しても得をしないように）。
      閾値は、正面の p95 と、その方向の p20 の中間に置く。
    """
    from .gesture import finger_straightness, thumb_far, thumb_fold_metric, thumb_up
    from .peace import _wrap, hand_angles, palm_position
    from .types import INDEX_MCP

    def label_of(t):
        return next((s["label"] for s in segments if s["t_start_ns"] <= t < s["t_end_ns"]), None)

    rows = []
    for f in frames:
        lab = label_of(f.t_capture_ns)
        if lab is None or not f.hands:
            continue
        h = max(f.hands, key=lambda h: h.score)
        x, y, ref = palm_position(h, f.image_size)
        at, ak = hand_angles(h, f.image_size)
        rows.append({"label": lab, "_t": f.t_capture_ns, "x": x, "y": y, "ref": ref, "at": at, "ak": ak,
                     "index": finger_straightness(h.world, INDEX_MCP), "up": thumb_up(h),
                     "fold": thumb_fold_metric(h), "far": thumb_far(h, f.image_size)})
    by = {}
    for r in rows:
        by.setdefault(r["label"], []).append(r)
    need = ["center", "up", "left", "right"]  # 下は使わない（SPEC 13.19）
    missing = [k for k in need if len(by.get(k, [])) < 5]
    if missing:
        raise ValueError(f"区間のフレームが足りません: {', '.join(missing)}")
    lines = []

    # --- 姿勢: thumb_far
    tu = [r for k in need for r in by[k]]
    # 親指の距離で区別したいのは、親指が隠れたり握られたりしたグー。カーソル操作の形は人差し指の条件で除ける
    neg = [r for k, v in by.items() if k.startswith("neg_fist") for r in v]
    far_pos = np.array([r["far"] for r in tu])
    params: dict = {}
    if neg:
        far_neg = np.array([r["far"] for r in neg])
        lo, hi = float(np.percentile(far_pos, 5)), float(np.percentile(far_neg, 98))
        if lo > hi:
            params["thumbsup_thumb_far_min"] = round((lo + hi) / 2, 3)
            sep = "分離できた"
        else:
            params["thumbsup_thumb_far_min"] = round(float((np.percentile(far_pos, 10) + np.percentile(far_neg, 95)) / 2), 3)
            sep = "分布が重なっている（間を取った）"
        params["thumbsup_stay_thumb_far_min"] = round(min(params["thumbsup_thumb_far_min"] * 0.8,
                                                          float(np.percentile(far_pos, 2))), 3)
        lines.append(f"親指先と手のひら中心の距離: いいね p5 {lo:.2f} / 間違えやすい形 p98 {hi:.2f} → {sep}。"
                     f"入る {params['thumbsup_thumb_far_min']:.2f}、保つ {params['thumbsup_stay_thumb_far_min']:.2f}")
        for k in sorted(k for k in by if k.startswith("neg_")):
            v = np.array([r["far"] for r in by[k]])
            lines.append(f"  {k}: 中央値 {np.median(v):.2f}  p98 {np.percentile(v, 98):.2f}")
    c = by["center"]
    for name, key, cond in (("人差し指が曲がっている", "index", lambda v: v < 0.80),
                            ("親指が上を向いている", "up", lambda v: v > 0.60),
                            ("親指が手のひらの外", "fold", lambda v: v < -0.30)):
        lines.append(f"入る条件「{name}」を満たす割合（正面）: {np.mean([cond(r[key]) for r in c]) * 100:.0f}%")

    # --- 方向: 各方向は、その直前の正面の区間（中央値）を基準に測る（実際の操作でも、止まった場所を正面に取り直す）
    seg_rows = []  # (label, [rows]) を時間順に
    for sgm in segments:
        rs = [r for r in rows if r.get("_t") is not None and sgm["t_start_ns"] <= r["_t"] < sgm["t_end_ns"]]
        seg_rows.append((sgm["label"], rs))
    F = {k: [] for k in need}
    origin = None
    for lab, rs in seg_rows:
        if not rs:
            continue
        if lab == "center":
            origin = (np.median([r["x"] for r in rs]), np.median([r["y"] for r in rs]), np.median([r["ref"] for r in rs]),
                      np.median([r["at"] for r in rs]), np.median([r["ak"] for r in rs]))
        if lab in F and origin is not None:
            ox, oy, oref, oat, oak = origin
            F[lab] += [[-(r["x"] - ox) / oref, -(r["y"] - oy) / oref, _wrap(r["at"] - oat), _wrap(r["ak"] - oak)]
                       for r in rs]
    F = {k: np.array(v) for k, v in F.items()}
    if any(len(F[k]) < 5 for k in need):
        raise ValueError("正面の後に記録した方向の区間が足りません")
    grid = np.arange(-2.0, 2.01, 0.25)
    best = {}
    for axis, (neg_d, pos_d, other) in {"x": ("left", "right", ("up",)), "y": (None, "up", ("left", "right"))}.items():
        j = 0 if axis == "x" else 1

        def evaluate(wt, wk):
            def sig(A):
                return A[:, j] + (wt * A[:, 2] + wk * A[:, 3]) / 45.0
            sc, sp = sig(F["center"]), sig(F[pos_d])
            sn = -sig(F[neg_d]) if neg_d else None
            cpos, cneg = float(np.percentile(sc, 95)), float(np.percentile(-sc, 95))
            scale = max(float(np.percentile(np.abs(sc), 95)), 0.05)
            margin = (np.percentile(sp, 20) - cpos) if sn is None else \
                min(np.percentile(sp, 20) - cpos, np.percentile(sn, 20) - cneg)
            margin /= scale
            leak = max(float(np.percentile(np.abs(sig(F[o])), 80)) for o in other)
            return margin - 0.5 * leak / scale, (wt, wk, cpos, cneg, float(np.percentile(sp, 20)),
                                                 float(np.percentile(sn, 20)) if sn is not None else 0.0, leak)
        base_score, base = evaluate(0.0, 0.0)
        cand = max((evaluate(wt, wk) for wt in grid for wk in grid), key=lambda x: x[0])
        # 回転は、変位だけより 2割以上よくなるときだけ使う（1回分の記録への当てはめすぎを防ぐ）
        best[axis] = cand[1] if cand[0] > base_score + 0.2 * abs(base_score) + 0.3 else base
    (xwt, xwk, xcp, xcn, xr20, xl20, xleak), (ywt, ywk, ycp, ycn, yu20, yd20, yleak) = best["x"], best["y"]
    params.update(thumbsup_x_w_thumb=float(xwt), thumbsup_x_w_knuckle=float(xwk),
                  thumbsup_y_w_thumb=float(ywt), thumbsup_y_w_knuckle=float(ywk))
    fire = {"right": xcp + 0.5 * (xr20 - xcp), "left": xcn + 0.5 * (xl20 - xcn),
            "up": ycp + 0.5 * (yu20 - ycp)}
    # 下限: 不感帯 0.40、発火 0.60（作者の所見: 学習した不感帯 0.17 では正面が狭すぎた）
    dead = max(0.40, min(max(xcp, xcn, ycp, ycn) * 1.2, 0.7 * min(fire.values())))
    params.update({f"thumbsup_fire_{d}": round(float(max(v, dead * 1.5, 0.60)), 3) for d, v in fire.items()})
    params.update(thumbsup_dead_disp=round(float(dead), 3), thumbsup_rearm_disp=round(float(dead * 0.7), 3),
                  thumbsup_max_disp=round(float(max(params[f"thumbsup_fire_{d}"] for d in fire) * 1.6), 3))
    lines.append("")
    for axis, (wt, wk, leak) in (("左右", (xwt, xwk, xleak)), ("上下", (ywt, ywk, yleak))):
        how = "変位のみ" if wt == 0 and wk == 0 else f"変位 + 親指の角度×{wt:+.2f} + 付け根の線の角度×{wk:+.2f}（/45°）"
        lines.append(f"{axis}: {how}  反対の軸へのはみ出し p80 {leak:.2f}")
    lines.append("発火の閾値: " + "  ".join(f"{d} {params[f'thumbsup_fire_{d}']:.2f}" for d in ("up", "left", "right"))
                 + f"  不感帯 {params['thumbsup_dead_disp']:.2f}")

    # --- 学習した値で、各区間の判定を確かめる（とどまった領域の割合）
    def region(A):
        sx = A[:, 0] + (xwt * A[:, 2] + xwk * A[:, 3]) / 45.0
        sy = A[:, 1] + (ywt * A[:, 2] + ywk * A[:, 3]) / 45.0
        out = []
        for a, b in zip(sx, sy):
            if max(abs(a), abs(b)) < dead:
                out.append("center")
            elif abs(b) >= abs(a):
                out.append("up" if b > 0 and b >= params["thumbsup_fire_up"] else "between")
            else:
                out.append("right" if a > 0 and a >= params["thumbsup_fire_right"] else
                           "left" if a < 0 and -a >= params["thumbsup_fire_left"] else "between")
        return out
    lines.append("学習した値での判定（各区間のフレームのうち、その方向の発火の閾値を超えた割合 / 別の方向と判定された割合）:")
    for k in need:
        reg = region(F[k])
        right = np.mean([r == k for r in reg]) * 100
        wrong = np.mean([r not in (k, "center", "between") for r in reg]) * 100
        lines.append(f"  {k:6s}: {right:3.0f}% / {wrong:3.0f}%")
    return LearnResult(params, "\n".join(lines))


def learn_zoom(frames, segments: list[dict]) -> LearnResult:
    """ズームモードの学習（SPEC 13.20）。区間のラベル: "neutral"（手の甲を向けて指を伸ばし止める）、"in"（手招きのように
    指先を遠ざける）、"out"（指先を近づける）。各 in / out は直前の neutral を基準に測る。

    指先の前後の測り方の候補（zoom.zoom_signals）ごとに、in と out が反対向きに、正面の揺れより十分大きく動くかを比べ、
    いちばん分かれるものを選ぶ。符号はズームインが正になるようにする。"""
    from .gesture import finger_straightness, palm_facing
    from .types import INDEX_MCP, MIDDLE_MCP, RING_MCP
    from .zoom import zoom_signals

    seq = []
    for sgm in segments:
        rs = []
        for f in frames:
            if sgm["t_start_ns"] <= f.t_capture_ns < sgm["t_end_ns"] and f.hands:
                h = max(f.hands, key=lambda h: h.score)
                sig = zoom_signals(h, f.image_size)
                sig["_back"] = palm_facing(h)
                sig["_open"] = min(finger_straightness(h.world, m) for m in (INDEX_MCP, MIDDLE_MCP, RING_MCP))
                rs.append(sig)
        seq.append((sgm["label"], rs))
    names = ["depth", "curl", "length"]
    rel = {k: {n: [] for n in names} for k in ("neutral", "in", "out")}
    origin = None
    for lab, rs in seq:
        if not rs:
            continue
        if lab == "neutral":
            origin = {n: float(np.median([r[n] for r in rs])) for n in names}
        if lab in rel and origin is not None:
            for n in names:
                rel[lab][n] += [r[n] - origin[n] for r in rs]
    for k in ("neutral", "in", "out"):
        if len(rel[k]["depth"]) < 5:
            raise ValueError(f"区間のフレームが足りません: {k}")
    lines = ["指先の前後の測り方の比較（正面からの変化。中央値 [p20〜p80]）:"]
    best = None
    for n in names:
        ne, i, o = (np.array(rel[k][n]) for k in ("neutral", "in", "out"))
        sign = 1.0 if np.median(i) >= np.median(o) else -1.0
        noise = max(float(np.percentile(np.abs(ne), 95)), 1e-3)
        both = np.median(i) * sign > 0 and np.median(o) * sign < 0
        margin = min(np.percentile(i * sign, 20), np.percentile(-o * sign, 20)) - noise
        score = margin / noise if both else -np.inf
        lines.append(f"  {n:6s}: 正面 ±{noise:.3f}  in {np.median(i):+.3f} [{np.percentile(i, 20):+.3f}〜"
                     f"{np.percentile(i, 80):+.3f}]  out {np.median(o):+.3f} [{np.percentile(o, 20):+.3f}〜"
                     f"{np.percentile(o, 80):+.3f}]  → " + (f"余裕 {score:.1f}（正面の揺れ比）" if both else "in と out が同じ向き（使えない）"))
        if best is None or score > best[0]:
            best = (score, n, sign, noise, float(np.median(i) * sign), float(np.median(-o * sign)))
    score, n, sign, noise, mi, mo = best
    if not np.isfinite(score) or score <= 0:
        raise ValueError("どの測り方でも、ズームイン・アウトと正面を区別できませんでした\n" + "\n".join(lines))
    dead = max(noise * 1.3, 0.35 * min(mi, mo))  # 正面の揺れだけでなく、動きの 35% までは反応しない
    params = {"zoom_signal": n, "zoom_sign": sign, "zoom_dead": round(dead, 4),
              "zoom_max": round(max(min(mi, mo), dead * 1.5), 4)}
    lines.append(f"→ 「{n}」を使う（符号 {sign:+.0f}）。不感帯 {params['zoom_dead']:.3f}、最大速度になる変化 {params['zoom_max']:.3f}")
    # 姿勢が保てているか（入った後は手の甲の向きだけを見る）
    for k in ("neutral", "in", "out"):
        rs = [r for lab, rr in seq if lab == k for r in rr]
        lines.append(f"  {k:7s}: 手の甲の向き > 0.20 の割合 {np.mean([r['_back'] > 0.20 for r in rs]) * 100:.0f}%"
                     + (f"、入る条件（手の甲 > 0.50 かつ指を伸ばす）{np.mean([r['_back'] > 0.5 and r['_open'] > 0.8 for r in rs]) * 100:.0f}%"
                        if k == "neutral" else ""))
    return LearnResult(params, "\n".join(lines))
