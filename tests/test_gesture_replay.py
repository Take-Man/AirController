"""ジェスチャー層の再現テスト。

tests/fixtures/*.jsonl（ランドマークの記録）をジェスチャー層に流し、
同名の *.events.json（期待するイベント列）と一致することを確認する。
期待値の作り直し: python -m tests.make_golden tests/fixtures/xxx.jsonl
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np
import pytest

from aircontroller.gesture import GestureConfig, GestureEngine, run
from aircontroller.recording import Recorder, iter_frames
from aircontroller.types import Hand, LandmarkFrame

FIXTURES = Path(__file__).parent / "fixtures"
MS = 1_000_000


def events_to_json(events) -> list[dict]:
    return [{"kind": e.kind, "t_ns": e.t_ns, "data": e.data} for e in events]


def _hand(score=0.9) -> Hand:
    rng = np.random.default_rng(0)
    return Hand("Right", score, rng.random((21, 3)), rng.random((21, 3)) * 0.1)


def _frames(pattern: str, dt_ms: float = 33.0):
    """pattern の1文字が1フレーム。'H' = 手あり、'.' = 手なし。"""
    return [LandmarkFrame(i + 1, int(i * dt_ms * MS), int(i * dt_ms * MS) + 10 * MS, (1280, 720),
                          (_hand(),) if c == "H" else ()) for i, c in enumerate(pattern)]


def test_acquire_after_hold():
    ev = run(GestureEngine(GestureConfig(acquire_hold_ms=120, lose_hold_ms=150)), _frames("....HHHHHH"))
    # 5フレーム目(132ms)で検出開始、120ms 後の 9フレーム目(264ms)で成立
    assert [(e.kind, e.t_ns // MS) for e in ev] == [("hand_acquired", 264)]


def test_short_flicker_is_ignored():
    # 獲得後、2フレーム（66ms）だけ見失っても hand_lost は出ない（ヒステリシス）
    ev = run(GestureEngine(), _frames("HHHHHHH..HHHH"))
    assert [e.kind for e in ev] == ["hand_acquired"]


def test_lose_after_hold():
    ev = run(GestureEngine(), _frames("HHHHHH........"))
    assert [e.kind for e in ev] == ["hand_acquired", "hand_lost"]


def test_low_score_counts_as_absent():
    frames = [LandmarkFrame(i, i * 33 * MS, i * 33 * MS, (1280, 720), (_hand(score=0.2),)) for i in range(20)]
    assert run(GestureEngine(GestureConfig(min_score=0.5)), frames) == []


def test_recording_roundtrip(tmp_path):
    frames = _frames("HH.H")
    rec = Recorder(tmp_path / "r.jsonl", {"note": "test"})
    for f in frames:
        rec.write(f)
    rec.close()
    back = list(iter_frames(tmp_path / "r.jsonl"))
    assert len(back) == len(frames)
    for a, b in zip(frames, back):
        assert (a.seq, a.t_capture_ns, a.t_infer_ns, a.image_size) == (b.seq, b.t_capture_ns, b.t_infer_ns, b.image_size)
        assert len(a.hands) == len(b.hands)
        for ha, hb in zip(a.hands, b.hands):
            np.testing.assert_allclose(ha.landmarks, hb.landmarks, atol=1e-6)
            np.testing.assert_allclose(ha.world, hb.world, atol=1e-6)


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.jsonl")), ids=lambda p: p.name)
def test_replay_matches_golden(path: Path):
    golden = path.with_suffix(".events.json")
    assert golden.exists(), f"期待値がありません: python -m tests.make_golden {path}"
    expected = json.loads(golden.read_text())
    known = {f.name for f in dataclasses.fields(GestureConfig)}
    expected["config"] = {k: v for k, v in expected["config"].items() if k in known}  # 廃止した設定は無視
    got1 = events_to_json(run(GestureEngine(GestureConfig(**expected["config"])), iter_frames(path)))
    got2 = events_to_json(run(GestureEngine(GestureConfig(**expected["config"])), iter_frames(path)))
    assert got1 == got2, "同じ記録で結果が変わった（非決定的）"
    assert got1 == expected["events"]
