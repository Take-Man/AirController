"""使う手の選択（TASKS-20261025 2.5）: 左右ラベルでフィルタし、一時的な反転はヒステリシスで無視する。"""
from __future__ import annotations

import dataclasses

from aircontroller.gesture import GestureConfig, GestureEngine, run
from tests.test_stage1 import make_hand
from tests.test_stage4 import frames


def L(h):
    return dataclasses.replace(h, handedness="Left")


def kinds(ev):
    return [e.kind for e in ev if e.kind != "move"]


def test_both_keeps_old_behavior():
    ev = run(GestureEngine(GestureConfig(hand="both")), frames([(20, L(make_hand()), True)]))
    assert "hand_acquired" in kinds(ev)


def test_other_hand_is_absent():
    eng = GestureEngine(GestureConfig(hand="right"))
    ev = run(eng, frames([(30, L(make_hand()), True)]))
    assert kinds(ev) == [] and not eng.hand_present


def test_short_flip_is_ignored():
    """1〜3 フレームの反転では、見失いにならない（カーソルも止まらない）。"""
    h = make_hand()
    ev = run(GestureEngine(GestureConfig(hand="right", hand_switch_frames=5)),
             frames([(10, h, True), (3, L(h), True), (10, h, True), (1, L(h), True), (10, h, True)]))
    assert kinds(ev) == ["hand_acquired"]
    plain = run(GestureEngine(GestureConfig(hand="right")), frames([(34, h, True)]))
    assert sum(e.kind == "move" for e in ev) == sum(e.kind == "move" for e in plain)  # 反転の間もカーソルは動く


def test_long_flip_switches_hand():
    """別のラベルが続き、そのとき手が跳んでいたら別の手とみなし、選んだ手でなければ見失いになる。"""
    h, other = make_hand(palm=(0.4, 0.5)), L(make_hand(palm=(0.85, 0.5)))
    eng = GestureEngine(GestureConfig(hand="right", hand_switch_frames=5))
    ev = run(eng, frames([(10, h, True), (20, other, True)]))
    assert kinds(ev) == ["hand_acquired", "hand_lost"] and eng.stable_label == "Left"


def test_long_flip_in_place_is_same_hand():
    """位置が続いたままラベルだけが長く反転しても、同じ手として使い続ける（thumbsup01 などで 8〜175 フレーム）。"""
    h = make_hand()
    eng = GestureEngine(GestureConfig(hand="right", hand_switch_frames=5, hand_switch_jump=0.15))
    ev = run(eng, frames([(40, h, True), (40, L(h), True)]))  # 30 フレーム以上続いた Right は守られる
    assert kinds(ev) == ["hand_acquired"] and eng.stable_label == "Right"
    eng = GestureEngine(GestureConfig(hand="right", hand_switch_frames=5, hand_switch_jump=0))
    assert kinds(run(eng, frames([(40, h, True), (40, L(h), True)]))) == ["hand_acquired", "hand_lost"]


def test_no_switch_during_drag():
    """ドラッグの途中では、ラベルが反転し続けても手を切り替えない（ボタンが解除されない）。"""
    open_, pinched = make_hand(pinch_m=0.08), make_hand(pinch_m=0.0)
    eng = GestureEngine(GestureConfig(hand="right", hand_switch_frames=5))
    ev = run(eng, frames([(10, open_, True), (25, pinched, True), (20, L(pinched), True), (10, pinched, True)]))
    k = kinds(ev)
    assert "drag_start" in k and "drag_end" not in k and "hand_lost" not in k and eng.state.mode == "drag"


def test_label_forgotten_after_absence():
    """見失いが続いた後は、次に見えた手のラベルをそのまま使う（左手が去った後に右手を出すと、すぐ使える）。"""
    h = make_hand()
    eng = GestureEngine(GestureConfig(hand="right"))
    ev = run(eng, frames([(20, L(h), True), (10, None, True), (6, h, True)]))
    assert kinds(ev) == ["hand_acquired"]


def test_wrong_label_on_entry_is_corrected():
    """入ってきた直後のラベルの誤り（use03: 画像の端から入った右手の最初の数フレームが Left）は、跳びがなくても直る。"""
    h = make_hand()
    eng = GestureEngine(GestureConfig(hand="right", hand_switch_frames=5, hand_settle_frames=30))
    ev = run(eng, frames([(4, L(h), True), (20, h, True)]))
    assert kinds(ev) == ["hand_acquired"] and eng.stable_label == "Right"


def _two(a, b, n):
    """1フレームに 2つの手が映ったフレーム列（num_hands = 2）。"""
    from aircontroller.types import LandmarkFrame
    return [LandmarkFrame(i + 1, int(i * 33 * 1e6), int(i * 33 * 1e6), (1280, 720), (a, b), True) for i in range(n)]


def test_two_hands_use_preferred_immediately():
    """左手がずっと映っていても、右手を出せば、左手を外さなくても右手で操作できる（作者の要望）。"""
    left = L(make_hand(palm=(0.8, 0.5), score=0.99))
    right = make_hand(palm=(0.4, 0.5), score=0.9)
    eng = GestureEngine(GestureConfig(hand="right"))
    seq = frames([(40, left, True)])
    t0 = seq[-1].t_capture_ns
    two = [f.__class__(**{**f.__dict__, "t_capture_ns": f.t_capture_ns + t0 + 33_000_000, "seq": f.seq + 40})
           for f in _two(left, right, 20)]
    ev = run(eng, seq + two)
    assert kinds(ev) == ["hand_acquired"] and eng.state.mode == "cursor" and eng.stable_label == "Right"
    moves = [e for e in ev if e.kind == "move"]
    assert moves and eng.state.palm[0] < 0.6  # 右手（画像の左側）の位置で動く
    # 左手が去っても、右手のまま続けて使える
    one = frames([(10, right, True)])
    t1 = two[-1].t_capture_ns + 33_000_000
    one = [f.__class__(**{**f.__dict__, "t_capture_ns": f.t_capture_ns + t1, "seq": f.seq + 60}) for f in one]
    for f in one:
        eng.update(f)
    assert eng.hand_present and eng.state.mode == "cursor"


def test_preferred_hand_helper():
    from aircontroller.gesture import preferred_hand
    left, right = L(make_hand(score=0.99)), make_hand(score=0.8)
    assert preferred_hand((left, right), "right") is right
    assert preferred_hand((left, right), "both") is left
    assert preferred_hand((left,), "right") is left  # 無ければ信頼度の高い手（校正・表示用）
