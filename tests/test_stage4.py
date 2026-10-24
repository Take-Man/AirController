"""段階4: ピースモード（姿勢の判定、スクロール、戻る・進む）。"""
from __future__ import annotations

import math

import numpy as np

from aircontroller.gesture import GestureConfig, GestureEngine, run, thumb_fold_metric
from aircontroller.peace import direction_angles
from aircontroller.types import Hand, LandmarkFrame

MS = 1_000_000


def make_peace(yaw=0.0, pitch=0.0, thumb_folded=True, palm=(0.5, 0.5)) -> Hand:
    """人差し指と中指を (yaw, pitch) の方向へ伸ばし、薬指・小指を畳んだ手（world 座標）。"""
    y_, p_ = math.radians(yaw), math.radians(pitch)
    d = np.array([-math.sin(y_) * math.cos(p_), -math.sin(p_), -math.cos(y_) * math.cos(p_)])
    w = np.zeros((21, 3))
    for mcp, x in {5: -0.03, 9: -0.01, 13: 0.01, 17: 0.03}.items():
        w[mcp] = [x, -0.09, 0]
        for k in range(1, 4):
            if mcp in (5, 9):
                w[mcp + k] = w[mcp] + d * 0.025 * k
            else:
                w[mcp + k] = w[mcp] + [[0, -0.025, 0], [0, -0.03, 0.02], [0, -0.015, 0.035]][k - 1]
    w[1:4] = [[-0.03, -0.02, 0], [-0.04, -0.04, 0.01], [-0.03, -0.06, 0.02]]
    w[4] = (w[5] + (w[17] - w[5]) * 0.5 + [0, 0.02, -0.02]) if thumb_folded else [-0.08, -0.07, 0]
    img = np.zeros((21, 3))
    img[:, 0] = palm[0] + w[:, 0] * 2.0
    img[:, 1] = palm[1] + w[:, 1] * 2.0 * 1280 / 720
    return Hand("Right", 0.95, img, w)


def frames(spec, dt_ms=33.0):
    out, i = [], 0
    for n, hand, active in spec:
        for _ in range(n):
            t = int(i * dt_ms * MS)
            out.append(LandmarkFrame(i + 1, t, t, (1280, 720), (hand,) if hand else (), active))
            i += 1
    return out


def cfg(**kw):
    return GestureConfig(peace_enabled=True, **kw)


def test_synthetic_hand_geometry():
    assert np.allclose(direction_angles(make_peace(20, 30)), (20, 30), atol=1e-6)
    assert thumb_fold_metric(make_peace()) > 0.25 > thumb_fold_metric(make_peace(thumb_folded=False))


def test_peace_mode_no_cursor_moves():
    ev = run(GestureEngine(cfg()), frames([(20, make_peace(), True)]))
    assert not [e for e in ev if e.kind in ("move", "scroll", "nav", "pinch_start")]


def test_thumb_out_is_not_peace():
    eng = GestureEngine(cfg())
    run(eng, frames([(20, make_peace(thumb_folded=False), True)]))
    assert eng.state.mode == "cursor"


def test_scroll_direction_and_speed():
    up = run(GestureEngine(cfg()), frames([(10, make_peace(), True), (30, make_peace(pitch=30), True)]))
    up_far = run(GestureEngine(cfg()), frames([(10, make_peace(), True), (30, make_peace(pitch=40), True)]))
    down = run(GestureEngine(cfg()), frames([(10, make_peace(), True), (30, make_peace(pitch=-30), True)]))
    s_up = sum(e.data["dy"] for e in up if e.kind == "scroll")
    s_far = sum(e.data["dy"] for e in up_far if e.kind == "scroll")
    s_down = sum(e.data["dy"] for e in down if e.kind == "scroll")
    assert s_far > s_up > 0 > s_down  # 上に向けるとページの先へ。大きく向けるほど速い


def test_neutral_offset():
    # 中立方向を 20° 内旋・40° 上に置くと、その方向は中央（スクロールしない）
    c = cfg(peace_neutral_yaw_deg=-20, peace_neutral_pitch_deg=40)
    ev = run(GestureEngine(c), frames([(40, make_peace(yaw=-20, pitch=40), True)]))
    assert not [e for e in ev if e.kind in ("scroll", "nav")]


def test_nav_fires_once_and_rearms_at_center():
    center, left, right = make_peace(), make_peace(yaw=-35), make_peace(yaw=35)
    ev = run(GestureEngine(cfg()), frames([(10, center, True), (30, left, True), (10, center, True),
                                           (30, left, True), (10, center, True), (20, right, True)]))
    assert [e.data["dir"] for e in ev if e.kind == "nav"] == ["back", "back", "forward"]


def test_nav_swap():
    """nav_swap（TASKS-20261025 2.4、config.toml の既定）: 手を左へ → 進む、右へ → 戻る。"""
    center, left, right = make_peace(), make_peace(yaw=-35), make_peace(yaw=35)
    ev = run(GestureEngine(cfg(nav_swap=True)), frames([(10, center, True), (30, left, True), (10, center, True),
                                                         (20, right, True)]))
    assert [e.data["dir"] for e in ev if e.kind == "nav"] == ["forward", "back"]


def test_inactive_emits_nothing():
    ev = run(GestureEngine(cfg()), frames([(10, make_peace(), False), (30, make_peace(pitch=35), False),
                                           (30, make_peace(yaw=-35), False)]))
    assert not [e for e in ev if e.kind in ("scroll", "nav")]


def test_displacement_method():
    c = cfg(peace_method="displacement")
    base = make_peace(pitch=60, palm=(0.5, 0.5))
    raised = make_peace(pitch=60, palm=(0.5, 0.25))  # 手ごと上へ（手首→中指付け根の長さの約 0.8 倍）
    ev = run(GestureEngine(c), frames([(10, base, True), (30, raised, True)]))
    assert sum(e.data["dy"] for e in ev if e.kind == "scroll") > 0


def test_nav_dwell_requires_hold():
    c = cfg(nav_dwell_ms=400)
    center, left = make_peace(), make_peace(yaw=-35)
    short = run(GestureEngine(c), frames([(10, center, True), (8, left, True), (10, center, True)]))  # 264ms
    long = run(GestureEngine(c), frames([(10, center, True), (20, left, True)]))  # 660ms
    assert not [e for e in short if e.kind == "nav"]
    assert [e.data["dir"] for e in long if e.kind == "nav"] == ["back"]


def test_peace_kept_when_thumb_and_ring_change():
    # 入った後は、親指が開き薬指が伸びて見えても（指先を下に向けたとき）、人差し指と中指が立っていれば保つ
    eng = GestureEngine(cfg())
    run(eng, frames([(10, make_peace(), True), (10, make_peace(pitch=-30, thumb_folded=False), True)]))
    assert eng.state.mode == "peace"


def make_fist(palm=(0.5, 0.5)) -> Hand:
    """4本の指をすべて畳んだ手。"""
    h = make_peace(palm=palm)
    w = h.world.copy()
    for mcp in (5, 9):
        for k in range(1, 4):
            w[mcp + k] = w[mcp] + [[0, -0.025, 0], [0, -0.03, 0.02], [0, -0.015, 0.035]][k - 1]
    img = h.landmarks.copy()
    img[:, 0] = palm[0] + w[:, 0] * 2.0
    img[:, 1] = palm[1] + w[:, 1] * 2.0 * 1280 / 720
    return Hand("Right", 0.95, img, w)


def test_fist_does_nothing():
    from tests.test_stage1 import make_hand
    c = GestureConfig(fist_enabled=True, peace_enabled=True)
    eng = GestureEngine(c)
    # カーソル操作 → グーで手を動かしても何も出ない → 指を立てると再びカーソルが動く
    ev = run(eng, frames([(10, make_hand(), True), (10, make_fist(), True), (10, make_fist(palm=(0.3, 0.3)), True),
                          (10, make_hand(palm=(0.3, 0.3)), True)]))
    t_fist0, t_fist1 = 10 * 33 * MS, 30 * 33 * MS
    assert not [e for e in ev if t_fist0 + 34 * MS <= e.t_ns < t_fist1 and e.kind in ("move", "scroll", "pinch_start")]
    assert [e for e in ev if e.t_ns >= t_fist1 + 200 * MS and e.kind == "move"]


def test_right_click_cancelled_when_moving_to_peace():
    from tests.test_stage1 import make_hand
    c = cfg()
    up, rpinch = make_hand(), make_hand(middle_pinch_m=0.005)
    # 親指が中指に触れたまま、待ち時間の途中でピースになる → 取り消し（peace04 の誤った右クリック）
    ev = run(GestureEngine(c), frames([(10, up, True), (4, rpinch, True), (20, make_peace(), True)]))
    assert not [e for e in ev if e.kind == "click"]


def make_thumbsup(palm=(0.5, 0.5)) -> Hand:
    """いいね: 拳を立て（小指側が下）、親指を上へ立てた手。グーの手を画像の面内で 90° 回し、親指を付け替える。"""
    h = make_fist(palm=palm)
    w = h.world.copy()
    w[:, :2] = np.stack([-w[:, 1], w[:, 0]], axis=1)  # (x, y) → (-y, x): 人差し指の付け根が上、小指の付け根が下
    w[2:5] = [[0.07, -0.05, 0], [0.07, -0.08, 0], [0.07, -0.11, 0]]
    img = h.landmarks.copy()
    img[:, 0] = palm[0] + w[:, 0] * 2.0
    img[:, 1] = palm[1] + w[:, 1] * 2.0 * 1280 / 720
    return Hand("Right", 0.95, img, w)


def tu_cfg(**kw):
    return GestureConfig(peace_enabled=True, thumbsup_enabled=True, fist_enabled=True, **kw)


def test_thumbsup_geometry():
    from aircontroller.gesture import thumb_far, thumb_up
    h = make_thumbsup()
    assert thumb_up(h) > 0.9 and thumb_fold_metric(h) < -0.3 and thumb_far(h, (1280, 720)) > 1.2
    assert thumb_far(make_fist(), (1280, 720)) < 1.2  # グーでは親指先が手のひらの近く
    assert thumb_fold_metric(make_fist()) > -0.3  # グーでは親指が手のひらの上


def test_thumbsup_mode_not_fist():
    eng = GestureEngine(tu_cfg())
    run(eng, frames([(20, make_thumbsup(), True)]))
    assert eng.state.mode == "thumbsup"
    eng = GestureEngine(tu_cfg())
    run(eng, frames([(20, make_fist(), True)]))
    assert eng.state.mode == "fist"


def test_thumbsup_fires_each_direction_once_with_dwell():
    seq = [(10, make_thumbsup(), True)]
    for palm in ((0.25, 0.5), (0.75, 0.5), (0.5, 0.15), (0.5, 0.75)):  # 画像上の左 = 利用者の右
        seq += [(25, make_thumbsup(palm=palm), True), (15, make_thumbsup(), True)]
    ev = run(GestureEngine(tu_cfg()), frames(seq))
    assert [e.data["dir"] for e in ev if e.kind == "thumbsup"] == ["right", "left", "up"]  # 下は使わない
    assert not [e for e in ev if e.kind in ("scroll", "nav", "move", "pinch_start")]


def test_thumbsup_short_visit_does_not_fire():
    ev = run(GestureEngine(tu_cfg()), frames([(10, make_thumbsup(), True), (8, make_thumbsup(palm=(0.35, 0.5)), True),
                                              (15, make_thumbsup(), True)]))
    assert not [e for e in ev if e.kind == "thumbsup"]


def test_thumbsup_rotation_weight():
    """ひねり（親指の角度の変化）だけでも、重みを付ければ発火する。"""
    import dataclasses as dc
    base = make_thumbsup()
    tilted = make_thumbsup()
    w = tilted.world.copy()
    w[4] = w[3] + [-0.03, -0.01, 0]  # 親指を利用者から見て右へ倒す（画像上は左）
    img = tilted.landmarks.copy()
    img[4, 0] = 0.5 + w[4, 0] * 2.0
    img[4, 1] = 0.5 + w[4, 1] * 2.0 * 1280 / 720
    tilted = dc.replace(tilted, world=w, landmarks=img)
    seq = [(10, base, True), (25, tilted, True)]
    assert not [e for e in run(GestureEngine(tu_cfg()), frames(seq)) if e.kind == "thumbsup"]
    ev = run(GestureEngine(tu_cfg(thumbsup_x_w_thumb=-2.0)), frames(seq))
    assert [e.data["dir"] for e in ev if e.kind == "thumbsup"] in (["right"], ["left"])
