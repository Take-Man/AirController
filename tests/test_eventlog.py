"""常時の軽い記録（TASKS-20261025 第1節）: イベントログ、印の前後の記録、集計。"""
from __future__ import annotations

import json
from pathlib import Path

from aircontroller.eventlog import EventLogConfig, EventLogWriter, MarkConfig, MarkRecorder, SessionLogger
from aircontroller.gesture import GestureConfig, GestureEngine
from aircontroller.recording import iter_frames, read_header
from aircontroller.report import load, summarize
from aircontroller.types import Event, LandmarkFrame
from tests.test_gesture_replay import FIXTURES, _hand

MS = 1_000_000


def _frame(i, active=True, hand=True, dt_ms=33):
    return LandmarkFrame(i, i * dt_ms * MS, i * dt_ms * MS + 10 * MS, (1280, 720), (_hand(),) if hand else (),
                         active)


def test_writer_rotates_and_prunes(tmp_path):
    for i in range(3):  # 前回までのログ
        (tmp_path / f"events-2026010{i}-000000.jsonl").write_text("{}\n")
    w = EventLogWriter(tmp_path, EventLogConfig(max_bytes=2000, max_files=4), stamp="20261025-000000")
    for i in range(100):
        w.put({"t_ns": i, "type": "x", "pad": "a" * 50})
    w.close()
    files = sorted(tmp_path.glob("events-*.jsonl"))
    assert len(w.paths) > 1 and len(files) == 4  # 上限を超えたら新しいファイルに移り、古いものから消す
    assert all(p in files for p in w.paths[-3:])
    n = sum(len(p.read_text().splitlines()) - 1 for p in w.paths if p.exists())
    assert n <= 100 and all(json.loads(p.read_text().splitlines()[0])["format"] == "aircontroller-events"
                            for p in w.paths if p.exists())


def test_session_logger_modes_and_cancel():
    rows = []
    s = SessionLogger(rows.append, lambda t: "w")
    s.observe(_frame(1), [Event("hand_acquired", 1)], "cursor", _hand())
    s.observe(_frame(2), [Event("pinch_start", 2, {"button": "left"})], "pinch", _hand())
    s.observe(_frame(3), [Event("pinch_cancel", 3, {"reason": "too_short"})], "cursor", _hand())
    s.observe(_frame(4), [], "peace", _hand())
    s.observe(_frame(5), [Event("scroll", 5, {"dy": 12}), Event("scroll", 5, {"dy": 3})], "peace", _hand())
    s.observe(_frame(6), [], "cursor", _hand())
    s.observe(_frame(7, active=False), [], "cursor", _hand())  # 無効化したら今のモードを閉じる
    exits = [r for r in rows if r["type"] == "mode_exit"]
    assert [(r["mode"], r.get("canceled")) for r in exits] == [("cursor", None), ("pinch", True), ("cursor", None),
                                                               ("peace", False), ("cursor", None)]
    assert exits[3]["scroll_px"] == 15 and exits[-1]["to"] == "idle"
    assert [r["type"] for r in rows].count("move") == 0


def test_mark_keeps_before_and_after(tmp_path):
    saved = []
    m = MarkRecorder(tmp_path, MarkConfig(ring_s=10, pre_s=6, post_s=3), {"note": "t"},
                     on_saved=lambda n, p, k: saved.append((n, p, k)))
    for i in range(1, 451):  # 15 秒
        f = _frame(i, dt_ms=33)
        m.add_frame(f)
        if i == 300:  # 9.9 秒で印
            m.mark(f.t_capture_ns)
    assert len(saved) == 1
    n, path, k = saved[0]
    frames = list(iter_frames(path))
    span = (frames[-1].t_capture_ns - frames[0].t_capture_ns) / 1e9
    assert path.name.startswith("mark-") and 8.8 < span <= 9.0 and k == len(frames)
    assert read_header(path)["mark"]["n"] == 1  # 既存の記録と同じ形式（iter_frames で読める）
    assert len(m._ring) <= 10 * 31


def test_mark_saved_without_frames(tmp_path):
    """カメラが止まっていてフレームが来なくても、時刻で締めて保存する。"""
    m = MarkRecorder(tmp_path, MarkConfig(post_s=3))
    m.add_frame(_frame(1))
    m.mark(33 * MS)
    m.poll(33 * MS + 2 * 10**9)
    assert not m.saved
    m.poll(33 * MS + 3 * 10**9)
    assert len(m.saved) == 1 and len(list(iter_frames(m.saved[0]))) == 1


def test_report_on_recording(tmp_path):
    """記録を流してイベントログを作り、集計できる（クリック数はジェスチャー層の出力と一致する）。"""
    path = FIXTURES / "use04.jsonl"
    engine = GestureEngine(GestureConfig(**json.loads(path.with_suffix(".events.json").read_text())["config"]))
    w = EventLogWriter(tmp_path, EventLogConfig(max_files=0), stamp="use04")
    s = SessionLogger(w.put)
    clicks = 0
    prev = False
    for f in iter_frames(path):
        if f.active != prev:
            s.record(f.t_capture_ns, "activation", on=f.active, reason="key")
            prev = f.active
        ev = engine.update(f)
        clicks += sum(1 for e in ev if e.kind == "click" and e.data["button"] == "left")
        s.observe(f, ev, engine.state.mode, engine.primary_hand(f))
    s.close(f.t_capture_ns)
    w.close()
    r = summarize(load(w.paths))
    assert r["clicks_left"] == clicks > 0 and r["active_s"] > 60
    assert r["mode_exits"]["pinch"] >= r["mode_canceled"].get("pinch", 0)
