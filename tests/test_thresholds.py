"""位置依存の入口（aircontroller/thresholds.py、SPEC 13.48）。今は位置を無視して、いつも設定の値を返す。"""
import dataclasses
import json
from pathlib import Path

import pytest

from aircontroller.gesture import GestureConfig, GestureEngine, run
from aircontroller.params import PARAMS
from aircontroller.recording import iter_frames
from aircontroller.thresholds import POSITION_KEYS, PositionThresholds, cell, validate_grid
from tests.test_gesture_replay import events_to_json

FIX = Path(__file__).parent / "fixtures"
ONES = [[1.0, 1.0, 1.0], [1.0, 1.0, 1.0], [1.0, 1.0, 1.0]]


def test_keys_match_ledger():
    """位置依存の候補は、台帳の印（position = True）と同じ。"""
    assert sorted(POSITION_KEYS) == sorted(p.key for p in PARAMS if p.position)


def test_at_ignores_position_for_now():
    cfg = GestureConfig(pinch_on=0.21, position_scale={"pinch_on": [[1, 1, 1], [1, 1, 1], [1, 1, 2]]})
    th = PositionThresholds(cfg)
    roi = (0.2, 0.2, 0.6, 0.6)
    for palm in [(0.1, 0.1), (0.5, 0.5), (0.9, 0.95), None]:
        assert th.at(palm, roi) == th.base and th.at(palm, roi).pinch_on == 0.21


def test_validate_grid():
    assert validate_grid(None) == {} and validate_grid({}) == {}
    assert validate_grid({"pinch_on": ONES})["pinch_on"][2] == (1.0, 1.0, 1.0)
    for bad in ({"drag_ms": ONES}, {"pinch_on": [[1, 1], [1, 1]]}, {"pinch_on": [[1, 1, 1], [1, 1, 1], [1, 1, 0]]},
                {"pinch_on": [[1, 1, 1], [1, 1, 1], [1, 1, True]]}, ["pinch_on"]):
        with pytest.raises(ValueError):
            validate_grid(bad)


def test_cell_is_user_view_and_clamped():
    roi = (0.2, 0.3, 0.6, 0.3)  # x0, y0, w, h（画像座標）
    assert cell((0.5, 0.45), roi) == (1, 1)  # 真ん中
    assert cell((0.75, 0.32), roi) == (0, 0)  # 画像の右上 = 利用者から見て左上
    assert cell((0.25, 0.58), roi) == (2, 2)  # 画像の左下 = 利用者から見て右下
    assert cell((-0.5, 2.0), roi) == (2, 2)  # 範囲の外は、いちばん近いマス


def test_position_table_is_read_and_checked():
    cfg = {"click": {"pinch_on": 0.2}, "position": {"pinch_on": ONES}}
    g = GestureConfig.from_toml(cfg)
    assert g.position_scale == {"pinch_on": ONES}
    GestureEngine(g)
    with pytest.raises(ValueError):
        GestureEngine(GestureConfig.from_toml({"position": {"pinch_on": [[1, 1, 1]]}}))


def test_live_swap_rebuilds_thresholds():
    eng = GestureEngine(GestureConfig(pinch_on=0.15))
    eng.apply_config(GestureConfig(pinch_on=0.2))
    assert eng._th.at((0.5, 0.5), eng.roi((1280, 720))).pinch_on == 0.2


@pytest.mark.parametrize("name", ["use03", "peace04", "right01"])
def test_position_table_does_not_change_events_yet(name):
    """[position] があっても、今は位置を無視するので、イベント列は期待値のまま。"""
    g = json.loads((FIX / f"{name}.events.json").read_text())
    known = {f.name for f in dataclasses.fields(GestureConfig)}
    cfg = GestureConfig(**{k: v for k, v in g["config"].items() if k in known})
    cfg = dataclasses.replace(cfg, position_scale={k: [[0.5, 1, 2], [1, 1, 1], [2, 1, 0.5]] for k in POSITION_KEYS})
    assert events_to_json(run(GestureEngine(cfg), iter_frames(FIX / f"{name}.jsonl"))) == g["events"]
