"""段階6: ズームモード（手の甲を向け、指先の前後でズーム）。"""
from __future__ import annotations

import math

import numpy as np

from aircontroller.gesture import GestureConfig, GestureEngine, palm_facing, run
from aircontroller.types import Hand
from tests.test_stage4 import frames, make_fist, make_peace
from tests.test_stage1 import make_hand

MS = 1_000_000


def make_back(tilt_deg=0.0, palm=(0.5, 0.5)) -> Hand:
    """手の甲をカメラに向け、4本の指を上へ伸ばした手。tilt_deg > 0 で指先がカメラから遠ざかる（奥へ倒れる）。"""
    a = math.radians(tilt_deg)
    d = np.array([0.0, -math.cos(a), math.sin(a)])  # world: y 下、z 奥
    w = np.zeros((21, 3))
    for mcp, x in {5: -0.03, 9: -0.01, 13: 0.01, 17: 0.03}.items():
        w[mcp] = [x, -0.09, 0]
        for k in range(1, 4):
            w[mcp + k] = w[mcp] + d * 0.025 * k
    w[1:5] = [[-0.03, -0.02, 0], [-0.05, -0.04, 0], [-0.07, -0.05, 0], [-0.09, -0.06, 0]]
    img = np.zeros((21, 3))
    img[:, 0] = palm[0] + w[:, 0] * 2.0
    img[:, 1] = palm[1] + w[:, 1] * 2.0 * 1280 / 720
    return Hand("Right", 0.95, img, w)


def zcfg(**kw):
    return GestureConfig(zoom_enabled=True, fist_enabled=True, peace_enabled=True, thumbsup_enabled=True, **kw)


def test_back_of_hand_facing():
    assert palm_facing(make_back()) > 0.9


def test_enter_needs_hold_then_zoom_in_and_out():
    eng = GestureEngine(zcfg())
    run(eng, frames([(5, make_back(), True)]))  # 165ms では入らない
    assert eng.state.mode != "zoom"
    ev = run(GestureEngine(zcfg()), frames([(15, make_back(), True), (40, make_back(tilt_deg=40), True)]))
    ins = [e for e in ev if e.kind == "zoom"]
    assert ins and all(e.data["dir"] == "in" for e in ins)
    ev = run(GestureEngine(zcfg()), frames([(15, make_back(), True), (40, make_back(tilt_deg=-40), True)]))
    outs = [e for e in ev if e.kind == "zoom"]
    assert outs and all(e.data["dir"] == "out" for e in outs)


def test_steady_hand_does_not_zoom_and_faster_with_more_tilt():
    still = run(GestureEngine(zcfg()), frames([(60, make_back(), True)]))
    assert not [e for e in still if e.kind == "zoom"]
    small = run(GestureEngine(zcfg()), frames([(15, make_back(), True), (60, make_back(tilt_deg=25), True)]))
    big = run(GestureEngine(zcfg()), frames([(15, make_back(), True), (60, make_back(tilt_deg=60), True)]))
    assert len([e for e in big if e.kind == "zoom"]) > len([e for e in small if e.kind == "zoom"]) > 0


def test_other_gestures_do_not_enter_zoom():
    for hand in (make_hand(), make_fist(), make_peace()):
        eng = GestureEngine(zcfg())
        run(eng, frames([(30, hand, True)]))
        assert eng.state.mode != "zoom"


def test_fist_leaves_zoom():
    ev_eng = GestureEngine(zcfg())
    run(ev_eng, frames([(15, make_back(), True), (15, make_fist(), True)]))
    assert ev_eng.state.mode == "fist"


def test_closing_thumb_leaves_zoom():
    """指は伸ばしたままでも、親指を握る（手のひら側へ畳む）とズームを抜ける。"""
    import dataclasses as dc
    closed = make_back()
    w = closed.world.copy()
    w[4] = [0.0, -0.07, -0.02]  # 親指先を手のひらの上（人差し指と小指の付け根の間）へ
    img = closed.landmarks.copy()
    img[4, 0] = 0.5 + w[4, 0] * 2.0
    img[4, 1] = 0.5 + w[4, 1] * 2.0 * 1280 / 720
    closed = dc.replace(closed, world=w, landmarks=img)
    eng = GestureEngine(zcfg())
    run(eng, frames([(15, make_back(), True), (15, closed, True)]))
    assert eng.state.mode != "zoom"
    eng = GestureEngine(zcfg())
    run(eng, frames([(15, closed, True)]))
    assert eng.state.mode != "zoom"  # 親指を握ったままでは入らない
