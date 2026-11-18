"""推論の結果の照合: 送った表に無い時刻の結果が届いても落ちない（掃除の後に遅れて届いた分。v0.2.0 で操作の終了の直後に KeyError で落ちた）。"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from aircontroller import input as inp


def _tracker(frames):
    t = inp.Tracker.__new__(inp.Tracker)  # MediaPipe の推論器は作らず、結果の受け取りだけを試す
    t._pending = {}
    t._done = threading.Event()
    t.error = None
    t.stale_results = 0
    t.on_frame = frames.append
    return t


EMPTY = SimpleNamespace(handedness=[], hand_landmarks=[], hand_world_landmarks=[])


def test_unknown_timestamp_is_dropped_not_fatal():
    frames = []
    t = _tracker(frames)
    t._pending[200] = (2, 200_000_000, (1280, 720))
    t._on_result(EMPTY, None, 100)  # 表に無い（掃除済み）
    assert t.error is None
    assert t.stale_results == 1
    assert frames == []
    assert not t._done.is_set()  # いま待っているフレーム（200）の待ちは解かない
    assert 200 in t._pending


def test_known_timestamp_is_delivered():
    frames = []
    t = _tracker(frames)
    t._pending[200] = (2, 200_000_000, (1280, 720))
    t._on_result(EMPTY, None, 200)
    assert t.error is None and t.stale_results == 0
    assert len(frames) == 1 and frames[0].seq == 2
    assert t._done.is_set()
    assert t._pending == {}
