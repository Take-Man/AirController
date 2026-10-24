"""イベントログと印のリングバッファが、推論スレッド（ホットパス）に足す時間を測る（TASKS-20261025 1.2）。

記録を流して、1フレームあたりの処理時間を
  A: ジェスチャー層だけ（engine.update）
  B: A + SessionLogger.observe + MarkRecorder.add_frame（書き込みは別スレッド）
で比べる。実機の遅延（p95 13〜20ms）に対して、足した時間がどれだけかを見る。

使い方: .venv/bin/python tools/bench_eventlog.py [tests/fixtures/*.jsonl ...]
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aircontroller.__main__ import load_config  # noqa: E402
from aircontroller.eventlog import EventLogConfig, EventLogWriter, MarkRecorder, SessionLogger  # noqa: E402
from aircontroller.gesture import GestureConfig, GestureEngine  # noqa: E402
from aircontroller.recording import iter_frames  # noqa: E402


def main(paths: list[str]) -> None:
    cfg = GestureConfig.from_toml(load_config(None))
    frames = [f for p in paths for f in iter_frames(p)]
    frames = [f.__class__(**{**f.__dict__, "active": True}) for f in frames]  # 有効化中として流す（最も重い）
    a_ms, b_ms, extra_ms = [], [], []
    with tempfile.TemporaryDirectory() as td:
        for rep in range(2):
            eng_a, eng_b = GestureEngine(cfg), GestureEngine(cfg)
            w = EventLogWriter(td, EventLogConfig(max_files=0), stamp=f"bench{rep}")
            slog = SessionLogger(w.put)
            marks = MarkRecorder(td, run_io=w.call)  # 本番と同じく、書き出しは別スレッド
            for i, f in enumerate(frames):
                t0 = time.perf_counter_ns()
                eng_a.update(f)
                t1 = time.perf_counter_ns()
                ev = eng_b.update(f)
                t2 = time.perf_counter_ns()
                slog.observe(f, ev, eng_b.state.mode, eng_b.primary_hand(f))
                marks.add_frame(f)
                if i % 900 == 450:
                    marks.mark(f.t_capture_ns)  # 30 秒に 1 回、印を付ける
                t3 = time.perf_counter_ns()
                if rep == 1:  # 1 回目は温め
                    a_ms.append((t1 - t0) / 1e6)
                    b_ms.append((t3 - t1) / 1e6)
                    extra_ms.append((t3 - t2) / 1e6)
            marks.flush()
            w.close()
            lines = w.lines
    a, b, x = map(np.array, (a_ms, b_ms, extra_ms))
    print(f"フレーム {len(a)}（{len(paths)} 本の記録、有効化中として流した）、イベントログ {lines} 行")
    for name, v in (("A ジェスチャー層だけ", a), ("B ジェスチャー層＋記録", b), ("  うち記録の分（B − ジェスチャー層）", x)):
        print(f"{name}: 平均 {v.mean() * 1000:.0f}µs  p50 {np.percentile(v, 50) * 1000:.0f}µs  "
              f"p95 {np.percentile(v, 95) * 1000:.0f}µs  p99 {np.percentile(v, 99) * 1000:.0f}µs  "
              f"max {v.max():.2f}ms")


if __name__ == "__main__":
    main(sys.argv[1:] or [str(p) for p in sorted((ROOT / "tests" / "fixtures").glob("use0*.jsonl"))])
