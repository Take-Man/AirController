"""段階1のジェスチャー層（カーソルモード、マッピング、クリック、有効化）の合成データによるテスト。"""
from __future__ import annotations

import numpy as np

from aircontroller.filters import OneEuroFilter
from aircontroller.gesture import GestureConfig, GestureEngine, run
from aircontroller.types import PALM, Hand, LandmarkFrame

MS = 1_000_000


def make_hand(index_up=True, pinch_m=0.08, palm=(0.5, 0.5), score=0.95, middle_pinch_m=None) -> Hand:
    """world landmarks を手で組み立てる。手首を原点に、指は -y 方向（上）へ伸びる。"""
    w = np.zeros((21, 3))
    mcp_x = {5: -0.03, 9: -0.01, 13: 0.01, 17: 0.03}
    for mcp, x in mcp_x.items():
        w[mcp] = [x, -0.09, 0]
        straight = index_up if mcp == 5 else False
        for k in range(1, 4):
            if straight:
                w[mcp + k] = w[mcp] + [0, -0.025 * k, 0]
            else:  # 曲げた指: 手のひら側へ折り返す
                w[mcp + k] = w[mcp] + [[0, -0.025, 0], [0, -0.03, 0.02], [0, -0.015, 0.035]][k - 1]
    w[1:4] = [[-0.03, -0.02, 0], [-0.05, -0.04, 0], [-0.06, -0.06, 0]]
    index_tip = w[8]
    w[4] = index_tip + [-pinch_m, 0, 0]  # 親指先は人差し指先から pinch_m 離す（中指先とは離れる向き）
    if middle_pinch_m is not None:  # 親指先を中指先の近くに置く（右クリック）
        w[4] = w[12] + [0, 0, -middle_pinch_m]
        w[4, 0] += middle_pinch_m
    # 画像座標: world をそのまま縮小して貼る（1280x720 の画像で、手首→中指付け根が約 0.09*2 = 画像幅の 18%）
    img = np.zeros((21, 3))
    img[:, 0] = palm[0] + w[:, 0] * 2.0
    img[:, 1] = palm[1] + w[:, 1] * 2.0 * 1280 / 720
    img[:, :2] -= img[list(PALM), :2].mean(axis=0) - np.asarray(palm)  # 手のひら中心を palm に合わせる
    return Hand("Right", score, img, w)


REF_M = 0.09  # make_hand の手首→中指付け根


def frames(spec, dt_ms=33.0, start=0):
    """spec: [(フレーム数, Hand または None, active), ...]"""
    out, i = [], start
    for n, hand, active in spec:
        for _ in range(n):
            t = int(i * dt_ms * MS)
            out.append(LandmarkFrame(i + 1, t, t + 15 * MS, (1280, 720), (hand,) if hand else (), active))
            i += 1
    return out


def kinds(events):
    return [e.kind for e in events if e.kind != "move"]


def test_cursor_moves_only_while_active():
    up = make_hand()
    ev = run(GestureEngine(), frames([(10, up, False), (10, up, True), (10, up, False)]))
    moves = [e for e in ev if e.kind == "move"]
    assert len(moves) == 10  # 姿勢は待機中に成立済みなので、有効化した最初のフレームから動く
    assert kinds(ev) == ["hand_acquired", "release_all"]


def test_no_cursor_without_index_up():
    ev = run(GestureEngine(), frames([(20, make_hand(index_up=False), True)]))
    assert not [e for e in ev if e.kind == "move"]


def test_mapping_mirror_and_center():
    eng = GestureEngine(GestureConfig(screen_w=1440, screen_h=900))
    assert np.allclose(eng.map_to_screen((0.5, 0.5), (1280, 720)), (720, 450))
    x0, y0, w, h = eng.roi((1280, 720))
    assert abs(w * 1280 / (h * 720) - 1440 / 900) < 1e-9  # 操作範囲のアスペクト比 = 画面
    # 画像の左端（利用者から見て右）→ 画面の右端
    assert np.allclose(eng.map_to_screen((x0, y0), (1280, 720)), (1440, 0))


def test_short_pinch_clicks_on_release():
    up, pinch = make_hand(), make_hand(pinch_m=0.01)
    ev = run(GestureEngine(), frames([(10, up, True), (5, pinch, True), (5, up, True)]))
    assert kinds(ev) == ["hand_acquired", "pinch_start", "click"]
    # ピンチ中はカーソルを固定（move を出さない）
    t_start = next(e.t_ns for e in ev if e.kind == "pinch_start")
    t_click = next(e.t_ns for e in ev if e.kind == "click")
    assert not [e for e in ev if e.kind == "move" and t_start <= e.t_ns < t_click]


def test_long_pinch_drags():
    up = make_hand(palm=(0.5, 0.5))
    pinch = make_hand(pinch_m=0.01, palm=(0.5, 0.5))
    moved = make_hand(pinch_m=0.01, palm=(0.45, 0.55))  # 鏡像なので画面では右下へ
    ev = run(GestureEngine(GestureConfig(cursor_min_cutoff=1000)),  # フィルタをほぼ素通しにする
             frames([(10, up, True), (20, pinch, True), (10, moved, True), (5, up, True)]))
    assert kinds(ev) == ["hand_acquired", "pinch_start", "drag_start", "drag_end"]
    start = next(e for e in ev if e.kind == "drag_start")
    assert (start.t_ns - next(e for e in ev if e.kind == "pinch_start").t_ns) / MS >= 500
    t_end = next(e.t_ns for e in ev if e.kind == "drag_end")
    drags = [e for e in ev if e.kind == "move" and start.t_ns < e.t_ns < t_end]
    assert drags and drags[-1].data["x"] > start.data["x"] and drags[-1].data["y"] > start.data["y"]
    # ドラッグの最初の移動は、押した位置から跳ばない
    assert abs(drags[0].data["x"] - start.data["x"]) < 1 and abs(drags[0].data["y"] - start.data["y"]) < 1


def test_drag_released_after_hand_lost():
    up, pinch = make_hand(), make_hand(pinch_m=0.01)
    ev = run(GestureEngine(), frames([(10, up, True), (20, pinch, True), (30, None, True)]))
    end = next(e for e in ev if e.kind == "drag_end")
    assert end.data["reason"] == "hand_lost"
    last_seen = 29 * 33  # 最後に手が見えたフレームの時刻(ms)
    assert 500 <= end.t_ns / MS - last_seen < 540


def test_drag_survives_short_dropout():
    up, pinch = make_hand(), make_hand(pinch_m=0.01)
    ev = run(GestureEngine(), frames([(10, up, True), (20, pinch, True), (8, None, True), (5, pinch, True),
                                      (5, up, True)]))
    ends = [e for e in ev if e.kind == "drag_end"]
    assert len(ends) == 1 and ends[0].data["reason"] == "released"  # 264ms の見失いでは離さない


def test_deactivate_mid_drag_releases():
    up, pinch = make_hand(), make_hand(pinch_m=0.01)
    ev = run(GestureEngine(), frames([(10, up, True), (20, pinch, True), (3, pinch, False)]))
    assert kinds(ev)[-2:] == ["drag_end", "release_all"]


def test_right_click():
    # 親指と中指を付けたまま right_hold_ms（400ms）待つと、離す前に右クリック。待つ間と、離すまではカーソル固定
    up, rpinch = make_hand(), make_hand(middle_pinch_m=0.005)
    ev = run(GestureEngine(), frames([(10, up, True), (16, rpinch, True), (5, up, True)]))
    assert kinds(ev) == ["hand_acquired", "pinch_start", "click"]
    t0 = next(e.t_ns for e in ev if e.kind == "pinch_start")
    click = next(e for e in ev if e.kind == "click")
    assert click.data["button"] == "right" and 400 <= (click.t_ns - t0) / MS < 440
    t_release = 26 * 33 * MS
    assert not [e for e in ev if e.kind == "move" and t0 <= e.t_ns < t_release]


def test_short_right_pinch_does_nothing():
    up, rpinch = make_hand(), make_hand(middle_pinch_m=0.005)
    ev = run(GestureEngine(), frames([(10, up, True), (5, rpinch, True), (5, up, True)]))  # 165ms で離す
    assert kinds(ev) == ["hand_acquired", "pinch_start", "pinch_cancel"]
    assert next(e for e in ev if e.kind == "pinch_cancel").data["reason"] == "released_early"


def test_long_right_pinch_fires_once():
    up, rpinch = make_hand(), make_hand(middle_pinch_m=0.005)
    ev = run(GestureEngine(), frames([(10, up, True), (60, rpinch, True), (5, up, True)]))  # 2 秒
    assert kinds(ev) == ["hand_acquired", "pinch_start", "click"]
def test_pinch_hysteresis():
    c = GestureConfig()
    mid = make_hand(pinch_m=(c.pinch_on + c.pinch_off) / 2 * REF_M)  # 閾値の間（画像座標の比）
    up, pinch = make_hand(), make_hand(pinch_m=0.01)
    ev = run(GestureEngine(c), frames([(10, up, True), (3, pinch, True), (5, mid, True), (3, up, True)]))
    assert kinds(ev) == ["hand_acquired", "pinch_start", "click"]
    assert next(e for e in ev if e.kind == "click").data["duration_ms"] > 200  # 間の値ではまだ離していない扱い
    ev = run(GestureEngine(c), frames([(10, up, True), (10, mid, True)]))
    assert "pinch_start" not in kinds(ev)  # 間の値ではピンチが始まらない


def test_release_key_mid_pinch_cancels():
    up, pinch = make_hand(), make_hand(pinch_m=0.01)
    ev = run(GestureEngine(), frames([(10, up, True), (4, pinch, True), (4, pinch, False), (4, up, False)]))
    assert kinds(ev) == ["hand_acquired", "pinch_start", "pinch_cancel", "release_all"]


def test_pinch_started_while_inactive_is_ignored():
    up, pinch = make_hand(), make_hand(pinch_m=0.01)
    # 摘まんだまま ¥ を押しても、クリックにしない
    ev = run(GestureEngine(), frames([(10, up, False), (2, pinch, False), (4, pinch, True), (4, up, True)]))
    assert "click" not in kinds(ev)
    # 押すのと同時に摘まみ始めた場合も、一度離れた指を見ていないのでクリックにしない
    ev = run(GestureEngine(), frames([(10, up, False), (4, pinch, True), (4, up, True)]))
    assert "click" not in kinds(ev)
    # 押してから摘まめばクリック
    ev = run(GestureEngine(), frames([(10, up, False), (1, up, True), (4, pinch, True), (4, up, True)]))
    assert "click" in kinds(ev)


def test_low_confidence_emits_nothing():
    ev = run(GestureEngine(), frames([(20, make_hand(score=0.3), True)]))
    assert ev == []


def test_hand_lost_mid_pinch_cancels():
    up, pinch = make_hand(), make_hand(pinch_m=0.01)
    ev = run(GestureEngine(), frames([(10, up, True), (3, pinch, True), (10, None, True)]))
    assert kinds(ev) == ["hand_acquired", "pinch_start", "hand_lost", "pinch_cancel"]


def test_one_euro_converges_and_smooths():
    f = OneEuroFilter(min_cutoff=1.0, beta=0.0)
    rng = np.random.default_rng(1)
    xs = [f(100 + rng.normal(0, 2), i / 30) for i in range(300)]
    assert abs(np.mean(xs[-60:]) - 100) < 1.0
    assert np.std(xs[-60:]) < 1.0  # 入力の標準偏差 2 より小さい


def test_drag_tolerates_brief_opening():
    c = GestureConfig()
    up, pinch = make_hand(), make_hand(pinch_m=0.01)
    loose = make_hand(pinch_m=(c.pinch_off + c.drag_off) / 2 * REF_M)  # 通常の解除閾値と、ドラッグ中の閾値の間
    open_ = make_hand(pinch_m=(c.drag_off + 0.05) * REF_M)
    # ドラッグ中に少し開いても離さない。ドラッグ用の閾値を超えても、2フレーム（66ms）なら離さない
    ev = run(GestureEngine(c), frames([(10, up, True), (20, pinch, True), (5, loose, True), (2, open_, True),
                                       (5, pinch, True), (6, up, True)]))
    assert kinds(ev) == ["hand_acquired", "pinch_start", "drag_start", "drag_end"]
    end = next(e for e in ev if e.kind == "drag_end")
    assert end.t_ns / MS >= (10 + 20 + 5 + 2 + 5) * 33 + c.drag_release_ms  # 最後に開いてから 100ms 後


def test_roi_adapts_toward_hand_outside():
    from aircontroller.types import LandmarkFrame as LF
    c = GestureConfig(roi_adapt=True, roi_width=0.3)
    eng = GestureEngine(c)
    x0, _, w, _ = eng.roi((1280, 720))
    inside = make_hand(palm=(0.5, 0.5))
    outside = make_hand(palm=(x0 + w + 0.1, 0.5))  # 画像の右へ 0.1 はみ出す
    run(eng, frames([(10, inside, True)]))
    assert (eng.roi_cx, eng.roi_width) == (0.5, 0.3)  # 範囲内なら変えない
    eng2 = GestureEngine(c)
    run(eng2, frames([(10, inside, True), (60, outside, True)]))
    nx0, _, nw, _ = eng2.roi((1280, 720))
    assert eng2.roi_width > 0.3 and nx0 + nw > x0 + w + 0.09  # 手の位置まで広がる
    assert nx0 > x0  # 反対側も少し同じ向きへずれる
    eng3 = GestureEngine(c)
    run(eng3, frames([(10, inside, False), (60, outside, False)]))
    assert eng3.roi_width == 0.3  # 有効化していないときは変えない
