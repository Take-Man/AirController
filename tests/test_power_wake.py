"""操作のオン・オフ（TASKS-20261025 第3節）: 手の不在による自動オフと、パーでのオン。"""
from __future__ import annotations

import dataclasses

import numpy as np

from aircontroller.gesture import palm_facing
from aircontroller.power import PowerConfig, PowerController, is_wake_pose
from tests.test_stage1 import make_hand
from tests.test_stage4 import make_fist
from tests.test_stage6 import make_back

S = 1_000_000_000


def make_palm():
    """手のひらをカメラに向け、5本の指を開いた手（make_back の左右を反転）。"""
    b = make_back()
    w = b.world.copy()
    w[:, 0] *= -1
    img = b.landmarks.copy()
    img[:, 0] = 1.0 - img[:, 0]
    return dataclasses.replace(b, world=w, landmarks=img)


def test_wake_pose():
    c = PowerConfig()
    assert palm_facing(make_palm()) < -0.9 and is_wake_pose(make_palm(), c)
    for h in (make_back(), make_hand(), make_fist()):  # ズームの手（手の甲）、カーソル、グーはパーではない
        assert not is_wake_pose(h, c)


def feed(pc, seq, t0=0, dt=1 / 30):
    """seq: (秒数, 手, active, 間隔) の列。返り値: (時刻(秒), 動作) の列。"""
    out, t = [], t0
    for dur, hand, active, step in seq:
        n = int(round(dur / step))
        for _ in range(n):
            a = pc.update(int(t * S), active, hand)
            if a:
                out.append((round(t, 2), a))
            t += step
    return out, t


def test_auto_off_after_absence():
    pc = PowerController(PowerConfig(auto_off_after_s=10))
    pc.activated(0)
    out, _ = feed(pc, [(5, make_hand(), True, 1 / 30), (9.9, None, True, 1 / 30)])
    assert out == []
    out, _ = feed(pc, [(0.2, None, True, 1 / 30)], t0=14.9)
    assert out and out[0][1] == "auto_off"


def test_wake_with_palm_via_probe():
    """1Hz の検出で手が見つかると確認の窓（毎フレーム）に入り、パーを 0.5 秒保つとオン。"""
    pc = PowerController(PowerConfig())
    pc.deactivated()
    pc._prev_present = False
    assert pc.min_interval() == 1.0
    out, t = feed(pc, [(3, None, False, 1.0)])  # 手なし: 1Hz のまま
    assert out == [] and pc.state == "off"
    out, t = feed(pc, [(1, make_palm(), False, 1.0)], t0=t)  # 1Hz の検出でパー → 窓
    assert pc.state == "probe" and pc.min_interval() == 0.0
    out, t = feed(pc, [(0.6, make_palm(), False, 1 / 30)], t0=t - 1 + 1 / 30)
    assert out and out[0][1] == "wake"


def test_probe_times_out_without_palm():
    pc = PowerController(PowerConfig(wake_probe_s=2.0))
    pc.deactivated()
    pc._prev_present = False
    feed(pc, [(1, make_hand(), False, 1.0)])
    assert pc.state == "probe"
    out, t = feed(pc, [(2.1, make_hand(), False, 1 / 30)], t0=1 / 30)
    assert out == [] and pc.state == "off"
    # 同じ手が映ったまま（パーでない）なら、窓を開け直さない（1Hz のまま）
    out, t = feed(pc, [(5, make_hand(), False, 1.0)], t0=t)
    assert out == [] and pc.state == "off"
    # パーにすれば、次の 1Hz の検出で窓が開く
    feed(pc, [(1, make_palm(), False, 1.0)], t0=t)
    assert pc.state == "probe"


def test_short_palm_does_not_wake():
    pc = PowerController(PowerConfig(wake_hold_s=0.5))
    pc.deactivated()
    pc._prev_present = False
    feed(pc, [(1, make_palm(), False, 1.0)])
    # 窓に入ると、次のフレームは 1/30 秒後に来る
    out, _ = feed(pc, [(0.3, make_palm(), False, 1 / 30), (1.0, make_hand(), False, 1 / 30)], t0=1 / 30)
    assert out == []
