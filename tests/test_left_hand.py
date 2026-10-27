"""左手・両手の対応（SPEC 13.53）: 設定は右手の形で持ち、左手で操作しているときは、左右に依存する検出の値を内部で反転する。

右手の記録の画像座標を左右反転し、ラベルを Left にした記録を作って流す。
- モードの並び（カーソル・ピンチ・ピース・いいね・ズーム・グー）とクリックなどは、元の記録と同じ
- 方向のイベント（戻る・進む、デスクトップ移動）は、利用者から見た絶対方向なので、左右が入れ替わる
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from aircontroller.calibration import collect_samples, learn_thumbsup, thumbsup_points
from aircontroller.gesture import GestureConfig, GestureEngine, mirror_hand, palm_facing
from aircontroller.recording import iter_frames
from aircontroller.thresholds import cell
from tests.test_gesture_replay import events_to_json
from tests.test_stage1 import make_hand

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests/fixtures"
REC = ROOT / "recordings"
# 方向のイベントがある記録（ピース・いいね・ズーム・クリック）
NAMES = ("thumbsup01", "peace04", "zoom02", "zoom03", "use04", "use05")
# 左右に依存する値を、左右で違う値にして流す（同じ値だと、反転し忘れても通ってしまう）
SIDED = dict(thumbsup_fire_left=1.30, thumbsup_fire_right=0.95, thumbsup_y_w_thumb=0.5, thumbsup_y_w_knuckle=-0.25)
SWAP = {"left": "right", "right": "left", "back": "forward", "forward": "back"}


def mirror_frame(f):
    return dataclasses.replace(f, hands=tuple(mirror_hand(h) for h in f.hands))


def golden_cfg(name: str, **kw) -> GestureConfig:
    cfg = json.loads((FIX / f"{name}.events.json").read_text())["config"]
    return GestureConfig(**{**cfg, **SIDED, **kw})


def trace(cfg: GestureConfig, frames) -> tuple[list[str], list[dict]]:
    """フレームごとのモードと、move 以外のイベント。カーソルの x は除く（操作範囲は画像上の絶対位置で、中心が 0.5 でなく、
    自動補正もかかるので、左右反転した手では鏡像にならない）。"""
    eng = GestureEngine(cfg)
    eng.reset()
    modes, events = [], []
    for f in frames:
        events += [{**e, "data": {k: v for k, v in e["data"].items() if k != "x"}}
                   for e in events_to_json(eng.update(f)) if e["kind"] != "move"]
        modes.append(eng.state.mode)
    return modes, events


def swapped(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        d = e["data"]
        if e["kind"] in ("nav", "thumbsup") and d.get("dir") in SWAP:
            e = {**e, "data": {**d, "dir": SWAP[d["dir"]]}}
        out.append(e)
    return out


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("hand", ["left", "both"])
def test_mirrored_recording(name, hand):
    """右手の記録（hand = right / both）と、それを左右反転した記録（hand = left / both）で、モードの並びは同じ、
    方向のイベントは左右が入れ替わる。"""
    path = FIX / f"{name}.jsonl"
    if not path.exists():
        pytest.skip("記録がない")
    frames = list(iter_frames(path))
    m1, e1 = trace(golden_cfg(name, hand="right" if hand == "left" else "both"), frames)
    m2, e2 = trace(golden_cfg(name, hand=hand), [mirror_frame(f) for f in frames])
    assert m1 == m2
    assert e2 == swapped(e1)
    assert any(e["kind"] in ("nav", "thumbsup") for e in e1) or name.startswith("zoom")  # 方向のイベントを確かめている


def test_left_without_flip_would_differ():
    """反転しなければ、左右で閾値の違ういいねの発火が変わる（上のテストが、反転を確かめていることの確認）。"""
    path = FIX / "thumbsup01.jsonl"
    if not path.exists():
        pytest.skip("記録がない")
    frames = [mirror_frame(f) for f in iter_frames(path)]
    import aircontroller.peace as peace

    orig = peace.PeaceController._fire_for
    try:
        peace.PeaceController._fire_for = lambda self, side: self.fire_by_dir.get(side, self.fire)
        _, e_noflip = trace(golden_cfg("thumbsup01", hand="left", thumbsup_fire_left=2.0, thumbsup_fire_right=0.7), frames)
    finally:
        peace.PeaceController._fire_for = orig
    _, e_flip = trace(golden_cfg("thumbsup01", hand="left", thumbsup_fire_left=2.0, thumbsup_fire_right=0.7), frames)
    assert e_noflip != e_flip


def test_peace_angle_neutral_yaw_is_mirrored():
    """angle 方式のピースでは、中立の方位角（右手の内旋 −37.6° など）を、左手では符号を反転して使う。"""
    path = FIX / "peace04.jsonl"
    if not path.exists():
        pytest.skip("記録がない")
    frames = list(iter_frames(path))
    kw = dict(peace_method="angle", peace_neutral_yaw_deg=-20.0)
    m1, e1 = trace(golden_cfg("peace04", hand="right", **kw), frames)
    m2, e2 = trace(golden_cfg("peace04", hand="left", **kw), [mirror_frame(f) for f in frames])
    assert m1 == m2 and e2 == swapped(e1)


def test_palm_facing_uses_operating_side():
    """手の甲の向きは、操作している手の左右で符号をそろえる（同じ手のラベルが一時的に反転しても、符号は変わらない）。"""
    h = make_hand()
    flipped_label = dataclasses.replace(h, handedness="Left")
    assert palm_facing(flipped_label, "Right") == pytest.approx(palm_facing(h))
    assert palm_facing(mirror_hand(h)) == pytest.approx(palm_facing(h))  # 本当の左手（鏡像）は、ラベルで符号をそろえる


def test_both_tracks_side_with_hysteresis():
    """both でも、操作している手の左右はヒステリシスをかけて決める（短いラベルの反転では切り替えない）。"""
    from tests.test_stage4 import frames as mk

    h = make_hand()
    eng = GestureEngine(GestureConfig(hand="both", hand_switch_frames=5))
    eng.reset()
    sides = []
    for f in mk([(10, h, True), (3, dataclasses.replace(h, handedness="Left"), True), (5, h, True)]):
        eng.update(f)
        sides.append(eng.side)
    assert set(sides) == {"Right"}
    eng.reset()
    for f in mk([(10, mirror_hand(h), True)]):
        eng.update(f)
    assert eng.side == "Left"


def test_position_cell_columns_swap_for_left():
    """位置の 3×3 の表は右手の形。左手では列（利用者から見た左・右）を入れ替えて引く（作者の決定）。"""
    roi = (0.2, 0.2, 0.6, 0.6)
    assert cell((0.75, 0.5), roi) == (1, 0)  # 画像の右 = 利用者の左
    assert cell((0.75, 0.5), roi, mirror=True) == (1, 2)
    assert cell((0.5, 0.5), roi, mirror=True) == (1, 1)


def _labeled(name):
    path = REC / f"{name}.jsonl"
    if not path.exists():
        pytest.skip("記録がない")
    segs = json.loads(path.with_suffix(".labels.json").read_text())["segments"]
    return list(iter_frames(path)), segs


def mirror_segments(segs):
    """左右反転した記録では、右手で「左へ」動かした区間は、左手で「右へ」動かした区間になる。"""
    return [{**s, "label": SWAP.get(s["label"], s["label"])} for s in segs]


def test_learn_thumbsup_saves_right_hand_form():
    """左手で記録した learn-thumbsup は、右手の形で保存する（右手の記録と同じ値になる）。"""
    frames, segs = _labeled("learn-thumbsup-20261024-052411")
    mframes, msegs = [mirror_frame(f) for f in frames], mirror_segments(segs)
    right = learn_thumbsup(frames, segs, "right").params
    left = learn_thumbsup(mframes, msegs, "left").params
    both = learn_thumbsup(mframes, msegs, "both").params
    for k, v in right.items():
        assert left[k] == pytest.approx(v, abs=1e-9), k
        assert both[k] == pytest.approx(v, abs=1e-9), k
    pr = thumbsup_points(frames, segs, "right", (0.0, 0.0, 0.5, 0.0))
    pl = thumbsup_points(mframes, msegs, "left", (0.0, 0.0, 0.5, 0.0))
    assert pl == pr  # 設定画面の分布も右手の形で出す


def test_calibrate_peace_neutral_in_right_hand_form():
    """calibrate のピースの中立方向（方位角）は、左手で測っても右手の形（符号を反転）で集める。"""
    for lab in sorted(REC.glob("calib-*.labels.json"), reverse=True):
        segs = json.loads(lab.read_text())["segments"]
        if any(s["label"] == "peace" for s in segs):
            break
    else:
        pytest.skip("ピースの区間がある記録がない")
    frames = list(iter_frames(lab.with_name(lab.name.replace(".labels.json", ".jsonl"))))
    a = collect_samples(frames, segs, "right")["peace_dir"]
    b = collect_samples([mirror_frame(f) for f in frames], segs, "left")["peace_dir"]
    c = collect_samples([mirror_frame(f) for f in frames], segs, "both")["peace_dir"]
    assert a and b == pytest.approx(a) and c == pytest.approx(a)
