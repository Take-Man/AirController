"""実カメラで、イベントログと印のリングバッファの有無によるソフトウェア遅延の差を測る（TASKS-20261025 1.2）。

カメラ → 推論 → ジェスチャー層（有効化中として扱う）→（B のときだけ）記録、までの時間を、A（記録なし）と B（記録あり）を
交互に seconds 秒ずつ rounds 回測って比べる。CGEvent は送らない（アクセシビリティの権限は要らない。カメラの権限は要る）。
手を映して動かしながら測ると、実際の使用に近い。

使い方: .venv/bin/python tools/latency_ab.py [--seconds 30] [--rounds 3]
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aircontroller.__main__ import _tracker_config, load_config  # noqa: E402
from aircontroller.eventlog import EventLogConfig, EventLogWriter, MarkRecorder, SessionLogger  # noqa: E402
from aircontroller.gesture import GestureConfig, GestureEngine  # noqa: E402
from aircontroller.input import CameraConfig, VisionPipeline  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()
    cfg = load_config(None)
    engine = GestureEngine(GestureConfig.from_toml(cfg))
    td = tempfile.mkdtemp(prefix="latency_ab-")
    w = EventLogWriter(td, EventLogConfig(max_files=0))
    slog = SessionLogger(w.put)
    marks = MarkRecorder(td, run_io=w.call)
    state = {"cond": "A"}
    lat: dict[str, list[float]] = {"A": [], "B": []}
    hands = {"A": 0, "B": 0}

    def on_frame(f):
        f = dataclasses.replace(f, active=True)
        ev = engine.update(f)
        c = state["cond"]
        if c == "B":
            slog.observe(f, ev, engine.state.mode, engine.primary_hand(f))
            marks.add_frame(f)
        lat[c].append((time.perf_counter_ns() - f.t_capture_ns) / 1e6)
        hands[c] += bool(f.hands)

    pipe = VisionPipeline(CameraConfig(**cfg["camera"]), _tracker_config(cfg), on_frame, lambda: 0.0)
    pipe.start()
    try:
        time.sleep(2.0)  # 温め
        lat["A"].clear(), lat["B"].clear()
        for r in range(args.rounds):
            for c in ("A", "B"):
                state["cond"] = c
                print(f"round {r + 1}/{args.rounds}: {c}（{'記録あり' if c == 'B' else '記録なし'}）{args.seconds:.0f}秒")
                if c == "B":
                    marks.mark()  # 区間ごとに 1 回、印を付ける（書き出しの負荷も含める）
                time.sleep(args.seconds)
    finally:
        pipe.stop()
        marks.flush()
        w.close()
    for c in ("A", "B"):
        a = np.array(lat[c])
        print(f"{c} {'記録あり' if c == 'B' else '記録なし'}: n={len(a)}  手の検出 {hands[c] / max(1, len(a)) * 100:.0f}%  "
              f"p50 {np.percentile(a, 50):.1f}ms  p95 {np.percentile(a, 95):.1f}ms  p99 {np.percentile(a, 99):.1f}ms  "
              f"max {a.max():.1f}ms")
    print(f"（記録の出力先: {td}。イベントログ {w.lines} 行、印 {len(marks.saved)} 個）")


if __name__ == "__main__":
    main()
