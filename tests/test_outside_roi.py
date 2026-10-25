"""操作範囲は手のひら中心と画面の写像にすぎない（2026-10-26 作者の指摘）。範囲の外でも、カメラの画角内で手が検出されていれば、
カーソルは最も近い画面の端に追従し、ジェスチャー（クリックなど）も使える。"""
from __future__ import annotations

from aircontroller.gesture import GestureConfig, GestureEngine, run
from tests.test_stage1 import make_hand
from tests.test_stage4 import frames, make_peace


def cfg():
    # 操作範囲は画像の中央の狭い範囲（幅 0.3）。手は範囲のずっと外（画像の端）に置く
    return GestureConfig(roi_cx=0.5, roi_cy=0.5, roi_width=0.3, peace_enabled=True, fist_enabled=True,
                         screen_w=1440, screen_h=900)


def test_cursor_clamps_to_nearest_edge():
    for palm, expect in (((0.05, 0.5), (1439, None)), ((0.95, 0.5), (0, None)), ((0.5, 0.05), (None, 0)),
                         ((0.5, 0.95), (None, 899))):
        ev = run(GestureEngine(cfg()), frames([(20, make_hand(palm=palm), True)]))
        moves = [e for e in ev if e.kind == "move"]
        assert moves, palm
        x, y = moves[-1].data["x"], moves[-1].data["y"]
        if expect[0] is not None:
            assert abs(x - expect[0]) < 1.0, (palm, x)  # 画像の左端 = 利用者から見て右（左右反転）
        if expect[1] is not None:
            assert abs(y - expect[1]) < 1.0, (palm, y)


def test_click_and_peace_outside_roi():
    open_, pinched = make_hand(palm=(0.05, 0.5), pinch_m=0.08), make_hand(palm=(0.05, 0.5), pinch_m=0.0)
    ev = run(GestureEngine(cfg()), frames([(10, open_, True), (4, pinched, True), (5, open_, True)]))
    assert [e.data["button"] for e in ev if e.kind == "click"] == ["left"]
    eng = GestureEngine(cfg())
    run(eng, frames([(20, make_peace(palm=(0.9, 0.2)), True)]))
    assert eng.state.mode == "peace"


def test_roi_width_capped_at_start():
    """calibration.toml に上限より広い幅が残っていても、起動時に上限に収める（SPEC 13.36）。"""
    eng = GestureEngine(GestureConfig(roi_width=0.74, roi_width_max=0.6))
    assert eng.roi_width == 0.6
