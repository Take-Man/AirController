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
                            default_drag_off: float = 0.0, on_frac: float = 0.40,
                            off_frac: float = 0.75) -> PinchThresholds:
    """摘まんだ状態の p90 と、普段の姿勢の p2 の間を区切る。

    成立は間の on_frac（既定 40%）、解除は off_frac（75%）、ドラッグ中の解除は 95% の位置。間が広いときは 0.3 として扱う
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
    return PinchThresholds(round(hi + on_frac * gap, 4), round(hi + off_frac * gap, 4),
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


def canonical_hand(h, hand: str = "both"):
    """校正・学習で使う手を、右手の形（正準）にそろえる（SPEC 13.53）。左手として扱う手は左右反転する。
    返り値は (そろえた手, 左手だったか)。左右は、設定の手（right / left）があればそれ、both ならフレームのラベル（gesture.hand_side）。"""
    from .gesture import hand_side, mirror_hand

    left = hand_side(h, hand) == "Left"
    return (mirror_hand(h) if left else h), left


def _swap_lr(label: str) -> str:
    """左手の記録の、方向の区間のラベルを右手の形に読み替える（左手の「左へ」は、右手の「右へ」に当たる）。"""
    return {"left": "right", "right": "left"}.get(label, label)


def learn_thumbsup(frames, segments: list[dict], hand: str = "both") -> LearnResult:
    """いいねモードの学習（SPEC 13.18）。frames はラベル付きの記録、segments は区間（label, t_start_ns, t_end_ns）。

    - 姿勢: 親指先と手のひら中心の距離（thumb_far）の閾値を、いいねと間違えやすい形（neg_*）の間に置く。
    - 方向: 各フレームの (右への変位, 上への変位, 親指の角度の変化, 付け根の線の角度の変化) を正面（center の中央値）
      から求め、軸ごとに「変位 + 重み × 角度/45」の重みを格子探索で選ぶ。選ぶ基準は、正面の揺れ（p95）を単位にした
      両方向の余裕（小さい方）から、反対の軸へのはみ出しを引いたもの（重みを大きくして全体を拡大しても得をしないように）。
      閾値は、正面の p95 と、その方向の p20 の中間に置く。
    - 左手で記録したときは、手を左右反転し、左右の区間を入れ替えて、右手の形で学習する（保存する値は右手の形。SPEC 13.53）。
    """
    from .gesture import finger_straightness, preferred_hand, thumb_far, thumb_fold_metric, thumb_up
    from .peace import _wrap, hand_angles, palm_position
    from .types import INDEX_MCP

    def label_of(t):
        return next((s["label"] for s in segments if s["t_start_ns"] <= t < s["t_end_ns"]), None)

    rows = []
    for f in frames:
        lab = label_of(f.t_capture_ns)
        if lab is None or not f.hands:
            continue
        h, left = canonical_hand(preferred_hand(f.hands, hand), hand)
        x, y, ref = palm_position(h, f.image_size)
        at, ak = hand_angles(h, f.image_size)
        rows.append({"label": _swap_lr(lab) if left else lab, "_t": f.t_capture_ns, "left": left,
                     "x": x, "y": y, "ref": ref, "at": at, "ak": ak,
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
        left = sum(r["left"] for r in rs) * 2 > len(rs)  # 区間の大半が左手なら、右手の形の向きに読み替える
        seg_rows.append((_swap_lr(sgm["label"]) if left else sgm["label"], rs))
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
    (xwt, xwk, xcp, xcn, xr20, xl20, xleak), (ywt, ywk, ycp, ycn, yu20, _yd20, yleak) = best["x"], best["y"]
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
    if rows and sum(r["left"] for r in rows) * 2 > len(rows):
        lines.append("左手で記録した: 手を左右反転し、左右の区間を入れ替えて、右手の形で学習した（上の左・右は右手の形での向き）")
    return LearnResult(params, "\n".join(lines))


def learn_zoom(frames, segments: list[dict], hand: str = "both") -> LearnResult:
    """ズームモードの学習（SPEC 13.20）。区間のラベル: "neutral"（手の甲を向けて指を伸ばし止める）、"in"（手招きのように
    指先を遠ざける）、"out"（指先を近づける）。各 in / out は直前の neutral を基準に測る。

    指先の前後の測り方の候補（zoom.zoom_signals）ごとに、in と out が反対向きに、正面の揺れより十分大きく動くかを比べ、
    いちばん分かれるものを選ぶ。符号はズームインが正になるようにする。"""
    from .gesture import finger_straightness, hand_side, palm_facing, preferred_hand
    from .types import INDEX_MCP, MIDDLE_MCP, RING_MCP
    from .zoom import zoom_signals

    seq = []
    for sgm in segments:
        rs = []
        for f in frames:
            if sgm["t_start_ns"] <= f.t_capture_ns < sgm["t_end_ns"] and f.hands:
                h = preferred_hand(f.hands, hand)
                sig = zoom_signals(h, f.image_size)
                sig["_back"] = palm_facing(h, hand_side(h, hand))
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


# ---------------------------------------------------------------- 測定ユニット（設定画面の第3節、SPEC 13.44）
# 各ユニットは、画面の指示に従って姿勢を記録する段階（ラベル, 秒, 指示）の並び。ラベル None は待ち時間。
# 各段階の最初の SETTLE 秒は、前の姿勢から移る途中なので集計しない（calib-034725 で動作が約 1.5 秒遅れていた）。
# 「普段の姿勢」（rest）は U2〜U4 で共通。まとめて測るときは 1 回だけ記録する。
SETTLE = 1.5
UNIT_ORDER = ("range", "pinch", "right", "peace", "thumbsup", "zoom")
UNIT_TITLES = {"range": "U1 操作範囲", "pinch": "U2 左ピンチ", "right": "U3 右クリック", "peace": "U4 ピース",
               "thumbsup": "U5 いいね", "zoom": "U6 ズーム"}
REST_UNITS = ("pinch", "right", "peace")
PREP_STEP = (None, 3.0, "準備: 楽な姿勢で手をカメラに映してください")
REST_STEPS = [(None, 2.5, "次: 普段カーソルを動かすときの手の形（人差し指を立て、親指は楽に）"),
              ("rest", 8.0 + SETTLE, "普段のカーソル操作の手の形で、画面のあちこちを指す（摘ままない）")]
_TU = "いいね（親指を立て、ほかの指は握る）"
THUMBSUP_STEPS = [
    ("neg_fist_side", 4.0 + SETTLE, "グーで、小指側をカメラに向ける（親指は隠す）。手を少し動かす"),
    (None, 2.0, "次: 普通のグー"),
    ("neg_fist", 4.0 + SETTLE, "普通のグー（親指は握った指の上）。手を少し動かす"),
    (None, 2.0, "次: カーソル操作の形"),
    ("neg_cursor", 4.0 + SETTLE, "人差し指だけを立てて、あちこちを指す"),
    (None, 3.0, f"次: {_TU}。これから上・左・右を1回ずつ"),
    ("center", 3.0 + SETTLE, f"{_TU}で、正面に止める"),
]
for _d, _dj in (("up", "上"), ("left", "左"), ("right", "右")):
    THUMBSUP_STEPS += [(_d, 3.0 + SETTLE, f"いいねのまま、{_dj}へ（動かす・ひねる、やりやすい方法で）して止める"),
                       ("center", 2.0 + SETTLE, "いいねのまま、正面に戻して止める")]
_BACK = "手の甲をカメラに向け、指を伸ばす"
ZOOM_STEPS = []
for _ in range(2):
    ZOOM_STEPS += [("neutral", 2.5 + SETTLE, f"{_BACK}。楽な姿勢で止める"),
                   ("in", 2.5 + SETTLE, "手招きのように、指先をカメラから遠ざけて止める（ズームイン）"),
                   ("neutral", 2.0 + SETTLE, f"{_BACK}。元の姿勢に戻して止める"),
                   ("out", 2.5 + SETTLE, "指先をカメラに近づけて止める（ズームアウト）")]
ZOOM_STEPS += [("neutral", 2.0 + SETTLE, f"{_BACK}。元の姿勢に戻して止める")]


def unit_steps(units, range_s: float = 10.0) -> list[tuple[str | None, float, str]]:
    """選んだユニットの段階を、決まった順に並べる（普段の姿勢は 1 回だけ）。"""
    own = {
        "range": [(None, 2.0, "次: 楽な姿勢のまま、届く範囲の端まで手を動かします"),
                  ("range", range_s + SETTLE, "上下左右の端まで、ゆっくり手のひらを動かす")],
        "pinch": [(None, 2.5, "次: 親指と人差し指で摘まんだままにします"),
                  ("pinch", 6.0 + SETTLE, "親指と人差し指で摘まんだまま、手を動かす・横に向ける")],
        "right": [(None, 2.5, "次: 親指と中指で摘まんだままにします"),
                  ("rpinch", 5.0 + SETTLE, "親指と中指で摘まんだまま、手を動かす・横に向ける")],
        "peace": [(None, 2.5, "次: ピースの形（人差し指と中指を立て、親指を畳む）"),
                  ("peace", 5.0 + SETTLE, "ピースの形で、楽に正面を指したまま止める")],
        "thumbsup": [(None, 2.5, "次: いいねのユニット。まず間違えやすい形を記録します")] + THUMBSUP_STEPS,
        "zoom": [(None, 2.5, f"次: ズームのユニット。{_BACK}")] + ZOOM_STEPS,
    }
    steps = [PREP_STEP]
    rest_done = False
    for u in UNIT_ORDER:
        if u not in units:
            continue
        if u in REST_UNITS and not rest_done:
            steps += REST_STEPS
            rest_done = True
        steps += own[u]
    return steps


def collect_samples(frames, segments: list[dict], hand: str = "both") -> dict[str, list]:
    """ラベルの区間ごとに、校正に使う値を集める（以前の calibrate の on_frame と同じ値）。
    frames_<label> は区間のフレーム数、seen_<label> はそのうち手が見えたフレーム数。"""
    from .gesture import image_pinch_metrics, preferred_hand, thumb_fold_metric
    from .peace import direction_angles
    from .types import PALM

    want = {"range", "rest", "pinch", "rpinch", "peace"}
    d: dict[str, list] = {k: [] for k in ("range", "rest_ti", "rest_tm", "rest_thumb", "pinch", "rpinch",
                                          "peace_thumb", "peace_dir")}
    count: dict[str, list] = {}
    segs = [s for s in segments if s["label"] in want]
    for f in frames:
        lab = next((s["label"] for s in segs if s["t_start_ns"] <= f.t_capture_ns < s["t_end_ns"]), None)
        if lab is None:
            continue
        c = count.setdefault(lab, [0, 0])
        c[0] += 1
        h = preferred_hand(f.hands, hand) if f.hands else None
        if h is None:
            continue
        c[1] += 1
        if lab == "range":
            d["range"].append(tuple(float(v) for v in h.landmarks[list(PALM), :2].mean(axis=0)))
            continue
        m = image_pinch_metrics(h, f.image_size)
        if lab == "rest":
            d["rest_ti"].append(m["thumb_index_n"])
            d["rest_tm"].append(m["thumb_middle_n"])
            d["rest_thumb"].append(thumb_fold_metric(h))
        elif lab == "pinch":
            d["pinch"].append(m["thumb_index_n"])
        elif lab == "rpinch":
            d["rpinch"].append(m["thumb_middle_n"])
        elif lab == "peace":
            d["peace_thumb"].append(thumb_fold_metric(h))
            d["peace_dir"].append(direction_angles(canonical_hand(h, hand)[0]))  # 中立の方位角は右手の形で（左手は符号が逆）
    for lab, (n, seen) in count.items():
        d[f"frames_{lab}"], d[f"seen_{lab}"] = [n], [seen]
    return d


@dataclass
class UnitResult:
    unit: str
    params: dict | None  # 候補 {節: {キー: 値}}。分離できない・データ不足なら None
    stats: dict
    warnings: list
    report: str

    def to_json(self) -> dict:
        return {"unit": self.unit, "title": UNIT_TITLES[self.unit], "candidate": self.params, "stats": self.stats,
                "warnings": self.warnings, "report": self.report}


# 段階ごとの、警告を出すサンプル数（30fps の記録時間の約半分）
MIN_SAMPLES = {"range": 30, "rest": 120, "pinch": 90, "rpinch": 75, "peace": 75}


def _pct(a, q) -> float:
    return round(float(np.percentile(np.asarray(a, dtype=np.float64), q)), 4)


def _finite(v) -> list[float]:
    return [float(x) for x in v if np.isfinite(x)]


def _seen_warnings(d: dict, labels, warnings: list) -> dict:
    stats = {}
    for lab in labels:
        n, seen = (d.get(f"frames_{lab}") or [0])[0], (d.get(f"seen_{lab}") or [0])[0]
        stats[f"{lab}_frames"], stats[f"{lab}_seen"] = n, seen
        if n == 0:
            warnings.append(f"{lab}: 区間のフレームがない")
        elif seen < 0.8 * n:
            warnings.append(f"{lab}: 手が見えたフレームが {seen / n * 100:.0f}%（80% 未満）")
        if seen < MIN_SAMPLES[lab]:
            warnings.append(f"{lab}: サンプル不足（{seen} フレーム。目安 {MIN_SAMPLES[lab]} 以上）")
    return stats


def _pair_unit(unit: str, rest: list, op: list, op_label: str, default_on, default_off, default_drag_off=0.0,
               on_frac=0.40, off_frac=0.75):
    """ピンチ型（距離が小さいほど操作）の閾値の候補と、統計。"""
    r, p = _finite(rest), _finite(op)
    stats = {"rest_n": len(r), "op_n": len(p)}
    if len(r) >= 1:
        stats.update(rest_p2=_pct(r, 2), rest_p10=_pct(r, 10), rest_median=_pct(r, 50))
    if len(p) >= 1:
        stats.update(op_p50=_pct(p, 50), op_p90=_pct(p, 90))
    try:
        th = derive_pinch_thresholds(r, p, default_on, default_off, default_drag_off, on_frac, off_frac)
    except ValueError as e:
        stats["separated"] = False
        return None, stats, [f"{op_label}: {e}"]
    stats.update(separated=th.separated, gap=round(th.rest_lo - th.pinch_hi, 4))
    warnings = []
    if not th.separated:
        warnings.append(f"{op_label}: 分布が重なっている（操作の p90 {th.pinch_hi:.3f} ≥ 普段の姿勢の p2 {th.rest_lo:.3f}）。候補なし")
        return None, stats, warnings
    if th.rest_lo - th.pinch_hi < 0.03:
        warnings.append(f"{op_label}: 分布の間が狭い（{th.rest_lo - th.pinch_hi:.3f}）")
    # 分布の間が 0.3 より広いときは 0.3 として閾値を置く（derive の中）。警告は出さない（作者の指示: 利用者がすることはない）
    return th, stats, warnings


def compute_units(units, frames, segments: list[dict], gdef, click_cfg: dict, image_size: tuple[int, int],
                  screen_size: tuple[float, float], hand: str = "both") -> dict[str, UnitResult]:
    """記録（frames と区間）から、ユニットごとの候補・統計・警告を出す。gdef は config.toml だけの GestureConfig
    （既定値と上限）、click_cfg は [click]（calib_on_frac / calib_off_frac、calibration.toml を重ねた値）。"""
    d = collect_samples(frames, segments, hand)
    out: dict[str, UnitResult] = {}
    if "range" in units:
        warnings: list = []
        stats = _seen_warnings(d, ["range"], warnings)
        params = None
        if len(d["range"]) < 30:
            warnings.append(f"範囲: データが足りない（{len(d['range'])} フレーム）。候補なし")
            lines = [warnings[-1]]
        else:
            iw, ih = image_size
            sw, sh = screen_size
            P = np.array(d["range"])
            x0, x1 = np.percentile(P[:, 0], [5, 95])
            y0, y1 = np.percentile(P[:, 1], [5, 95])
            # 操作範囲は画面と同じアスペクト比。測った範囲（5〜95%）がちょうど収まる幅にする
            w = max(x1 - x0, (y1 - y0) * ih * sw / (iw * sh))
            lines = []
            if w > gdef.roi_width_max:  # 範囲の外でも手が画角に収まる余白を残す（SPEC 13.36）
                warnings.append(f"測った幅 {w:.2f} は上限 {gdef.roi_width_max:.2f} を超えるので、上限にした（中心はそのまま）")
                w = gdef.roi_width_max
            params = {"cursor": {"roi_cx": round(float((x0 + x1) / 2), 4), "roi_cy": round(float((y0 + y1) / 2), 4),
                                 "roi_width": round(float(w), 4)}}
            stats.update(x_p5=round(float(x0), 4), x_p95=round(float(x1), 4), y_p5=round(float(y0), 4),
                         y_p95=round(float(y1), 4))
            lines.append(f"操作範囲 中心 ({(x0 + x1) / 2:.2f}, {(y0 + y1) / 2:.2f}) 幅 {w:.2f}")
        out["range"] = UnitResult("range", params, stats, warnings, "\n".join(lines))
    if "pinch" in units:
        warnings = []
        seen = _seen_warnings(d, ["rest", "pinch"], warnings)
        th, stats, w2 = _pair_unit("pinch", d["rest_ti"], d["pinch"], "親指-人差し指", gdef.pinch_on, gdef.pinch_off,
                                   gdef.drag_off, click_cfg.get("calib_on_frac", 0.40), click_cfg.get("calib_off_frac", 0.75))
        params = {"click": {"pinch_on": th.on, "pinch_off": th.off, "drag_off": th.drag_off}} if th else None
        out["pinch"] = UnitResult("pinch", params, {**seen, **stats}, warnings + w2,
                                  th.summary("親指-人差し指") if th else "\n".join(w2))
    if "right" in units:
        warnings = []
        seen = _seen_warnings(d, ["rest", "rpinch"], warnings)
        th, stats, w2 = _pair_unit("right", d["rest_tm"], d["rpinch"], "親指-中指", gdef.right_on, gdef.right_off)
        # 右クリックの「直前に離れていた」条件（right_arm）は、解除の閾値より十分大きくしておく
        params = {"click": {"right_on": th.on, "right_off": th.off,
                            "right_arm": round(max(gdef.right_arm, th.off + 0.08), 4)}} if th else None
        out["right"] = UnitResult("right", params, {**seen, **stats}, warnings + w2,
                                  th.summary("親指-中指") if th else "\n".join(w2))
    if "peace" in units:
        warnings = []
        stats = _seen_warnings(d, ["rest", "peace"], warnings)
        r, p = _finite(d["rest_thumb"]), _finite(d["peace_thumb"])
        stats.update(rest_n=len(r), op_n=len(p))
        if r:
            stats.update(rest_p98=_pct(r, 98), rest_p90=_pct(r, 90), rest_median=_pct(r, 50))
        if p:
            stats.update(op_p10=_pct(p, 10), op_p50=_pct(p, 50))
        params, lines = None, []
        try:
            thumb = derive_thumb_fold(r, p, gdef.thumb_fold_on, gdef.thumb_fold_off)
            yaw, pitch = neutral_direction(d["peace_dir"])
            stats.update(separated=thumb.separated, gap=round(thumb.pinch_hi - thumb.rest_lo, 4),
                         neutral_yaw=round(yaw, 2), neutral_pitch=round(pitch, 2))
            lines.append(f"親指を畳んだ判定: ピース p10 {thumb.pinch_hi:.3f} / 普段の姿勢 p98 {thumb.rest_lo:.3f} → "
                         f"{'分離できた' if thumb.separated else '分布が重なっている（候補なし）'}  成立 {thumb.on:.3f}  解除 {thumb.off:.3f}")
            lines.append(f"ピースの中立方向: 方位角 {yaw:+.1f}°（右が正）  仰角 {pitch:+.1f}°（上が正）")
            # 中立方向は分離とは関係なく出す（以前の calibrate と同じ）
            params = {"peace": {"peace_neutral_yaw_deg": round(yaw, 2), "peace_neutral_pitch_deg": round(pitch, 2)}}
            if thumb.separated:
                params["peace"].update(thumb_fold_on=round(thumb.on, 4), thumb_fold_off=round(thumb.off, 4))
            else:
                warnings.append("親指の畳み: 分布が重なっている。親指の閾値の候補なし")
        except ValueError as e:
            stats["separated"] = False
            warnings.append(f"ピース: {e}")
            lines.append(warnings[-1])
        out["peace"] = UnitResult("peace", params, stats, warnings, "\n".join(lines))
    for unit, fn, section in (("thumbsup", learn_thumbsup, "thumbsup"), ("zoom", learn_zoom, "zoom")):
        if unit in units:
            try:
                r = fn(frames, segments, hand)
                out[unit] = UnitResult(unit, {section: r.params}, {}, [], r.report)
            except ValueError as e:
                out[unit] = UnitResult(unit, None, {}, [f"学習できなかった: {e}"], str(e))
    return out


def reconstruct_old_segments(frames) -> list[dict]:
    """ラベルのない以前の calibrate の記録（2026-10-24 04:05 以降の手順）から、段階の区間を時刻で復元する。
    摘まんでいる段階（pinch, rpinch）は active = True で記録されているので、それを起点に、決まった段階の長さで前後をたどる。
    手順: [準備 3, 手を開く 5.5, 待ち 2, 範囲 11.5, 待ち 2.5]（--pinch-only では 準備 3 だけ）, 待ち 2.5, 普段の姿勢 9.5, 待ち 2.5,
    摘まむ 7.5, 待ち 2.5, 中指で摘まむ 6.5, 待ち 2.5, ピース 6.5（秒。各段階の最初の 1.5 秒は集計しない）。"""
    runs, prev = [], False
    for f in frames:
        if f.active and not prev:
            runs.append([f.t_capture_ns, None])
        if prev and not f.active:
            runs[-1][1] = f.t_capture_ns
        prev = f.active
    if len(runs) != 2 or runs[1][1] is None:
        raise ValueError("摘まんでいる段階（active）が 2 つ見つからない。手順が違う記録")
    s = 1e9
    (p0, p1), (r0, r1) = runs
    if abs((p1 - p0) / s - 7.5) > 0.3 or abs((r1 - r0) / s - 6.5) > 0.3 or abs((r0 - p1) / s - 2.5) > 0.3:
        raise ValueError(f"段階の長さが今の手順と違う（摘まむ {(p1 - p0) / s:.1f} 秒、中指 {(r1 - r0) / s:.1f} 秒）。以前の手順の記録")

    def seg(label, start, dur):
        return {"label": label, "t_start_ns": int(start + SETTLE * s), "t_end_ns": int(start + dur * s)}
    rest0 = p0 - (2.5 + 9.5) * s
    segs = [seg("rest", rest0, 9.5), seg("pinch", p0, 7.5), seg("rpinch", r0, 6.5), seg("peace", r1 + 2.5 * s, 6.5)]
    t_first = frames[0].t_capture_ns
    range0 = rest0 - (2.5 + 11.5) * s
    if range0 - (3.0 + 5.5 + 2.0) * s >= t_first - 0.5 * s:  # 範囲を測った記録（--pinch-only でない）
        segs.insert(0, seg("range", range0, 11.5))
    return segs


# ---------------------------------------------------------------- 設定画面の分布（SPEC 13.46）
def thumbsup_points(frames, segments: list[dict], hand: str = "both", weights=(0.0, 0.0, 0.0, 0.0)) -> dict:
    """いいねの記録（learn-thumbsup / calibrate --unit thumbsup）の、各方向の区間の方向の量（右・上が正、単位は
    手首→中指付け根の長さ）と、親指の離れ（thumb_far）。方向の量は learn_thumbsup と同じく、直前の正面の区間の中央値から測る。
    weights は (左右: 親指, 左右: 付け根, 上下: 親指, 上下: 付け根) の回転の重み。
    左手の記録は、learn_thumbsup と同じく右手の形にそろえて出す（設定画面は右手操作の場合を表示する。SPEC 13.53）。"""
    from .gesture import preferred_hand, thumb_far
    from .peace import _wrap, hand_angles, palm_position

    xwt, xwk, ywt, ywk = weights
    points, far = [], {"thumbsup": [], "neg": []}
    origin = None
    for sgm in segments:
        lab = sgm["label"]
        rs, lefts = [], []
        for f in frames:
            if sgm["t_start_ns"] <= f.t_capture_ns < sgm["t_end_ns"] and f.hands:
                h, left = canonical_hand(preferred_hand(f.hands, hand), hand)
                x, y, ref = palm_position(h, f.image_size)
                at, ak = hand_angles(h, f.image_size)
                rs.append((x, y, ref, at, ak, thumb_far(h, f.image_size)))
                lefts.append(left)
        if not rs:
            continue
        if sum(lefts) * 2 > len(lefts):
            lab = _swap_lr(lab)
        if lab.startswith("neg_fist"):
            far["neg"] += [r[5] for r in rs]
        elif lab in ("center", "up", "left", "right", "down"):
            far["thumbsup"] += [r[5] for r in rs]
        if lab == "center":
            origin = tuple(float(np.median([r[i] for r in rs])) for i in range(5))
        if lab in ("center", "up", "left", "right", "down") and origin is not None:
            ox, oy, oref, oat, oak = origin
            for x, y, _ref, at, ak, _far in rs:
                dat, dak = _wrap(at - oat), _wrap(ak - oak)
                points.append({"label": lab, "x": round(-(x - ox) / oref + (xwt * dat + xwk * dak) / 45.0, 3),
                               "y": round(-(y - oy) / oref + (ywt * dat + ywk * dak) / 45.0, 3)})
    return {"points": points, "far": {k: [round(v, 3) for v in vs] for k, vs in far.items()}}


def zoom_points(frames, segments: list[dict], hand: str = "both", signal: str = "depth", sign: float = 1.0) -> list[dict]:
    """ズームの記録（learn-zoom / calibrate --unit zoom）の、各区間の指先の前後の変化（ズームインが正）。
    learn_zoom と同じく、直前の neutral の区間の中央値から測る。"""
    from .gesture import preferred_hand
    from .zoom import zoom_signals

    out, origin = [], None
    for sgm in segments:
        lab = sgm["label"]
        if lab not in ("neutral", "in", "out"):
            continue
        vs = [zoom_signals(preferred_hand(f.hands, hand), f.image_size)[signal] for f in frames
              if sgm["t_start_ns"] <= f.t_capture_ns < sgm["t_end_ns"] and f.hands]
        if not vs:
            continue
        if lab == "neutral":
            origin = float(np.median(vs))
        if origin is not None:
            out += [{"label": lab, "v": round(sign * (v - origin), 4)} for v in vs]
    return out
