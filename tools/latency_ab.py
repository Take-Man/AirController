"""実カメラで、イベントログと印のリングバッファの有無によるソフトウェア遅延の差を測る（TASKS-20261025 1.2）。

カメラ → 推論 → ジェスチャー層（有効化中として扱う）→（B のときだけ）記録、までの時間を、A（記録なし）と B（記録あり）を
短い区間（block 秒）ずつ交互に、合計 seconds × rounds × 2 秒測って比べる。
推論（取得 → 推論完了）と、推論後（推論完了 → 記録まで。記録の負荷はここに出る）を分けて出す。
推論の時間は手の有無で変わる（見失うと手のひらの検出をやり直すので遅い）ため、手が映っているフレームだけの値も出す。CGEvent は送らない（アクセシビリティの権限は要らない。カメラの権限は要る）。
手を映して動かしながら測ると、実際の使用に近い。

使い方: .venv/bin/python tools/latency_ab.py [--seconds 30] [--rounds 3] [--num-hands 1|2]
  --num-hands で、同時に検出する手の数（config の tracker.num_hands）を上書きする（1 と 2 の推論時間を比べる）
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
    ap.add_argument("--block", type=float, default=5.0, help="A と B を切り替える間隔（秒）")
    ap.add_argument("--num-hands", type=int, help="同時に検出する手の数を上書きする")
    args = ap.parse_args()
    cfg = load_config(None)
    if args.num_hands:
        cfg["tracker"]["num_hands"] = args.num_hands
    print(f"num_hands = {cfg['tracker']['num_hands']}")
    engine = GestureEngine(GestureConfig.from_toml(cfg))
    td = tempfile.mkdtemp(prefix="latency_ab-")
    w = EventLogWriter(td, EventLogConfig(max_files=0))
    slog = SessionLogger(w.put)
    marks = MarkRecorder(td, run_io=w.call)
    state = {"cond": "A"}
    rows: dict[str, list[tuple[float, float, bool]]] = {"A": [], "B": []}  # (全体, 推論後, 手あり)

    def on_frame(f):
        f = dataclasses.replace(f, active=True)
        ev = engine.update(f)
        c = state["cond"]
        if c == "B":
            slog.observe(f, ev, engine.state.mode, engine.primary_hand(f))
            marks.add_frame(f)
        t = time.perf_counter_ns()
        rows[c].append(((t - f.t_capture_ns) / 1e6, (t - f.t_infer_ns) / 1e6, bool(f.hands)))

    pipe = VisionPipeline(CameraConfig(**cfg["camera"]), _tracker_config(cfg), on_frame, lambda: 0.0)
    pipe.start()
    try:
        time.sleep(2.0)  # 温め
        rows["A"].clear(), rows["B"].clear()
        total = args.seconds * args.rounds * 2
        n_blocks = max(2, int(total / args.block))
        print(f"{total:.0f}秒、{args.block:.0f}秒ごとに A（記録なし）と B（記録あり）を切り替えます")
        for i in range(n_blocks):
            c = "AB"[i % 2]
            state["cond"] = c
            if c == "B" and i % 12 == 1:
                marks.mark()  # 1 分に 1 回ほど印を付ける（書き出しの負荷も含める）
            time.sleep(args.block)
    finally:
        pipe.stop()
        marks.flush()
        w.close()
    def pct(a):
        return (f"p50 {np.percentile(a, 50):.2f}  p95 {np.percentile(a, 95):.2f}  p99 {np.percentile(a, 99):.2f}  "
                f"max {a.max():.1f}ms" if len(a) else "なし")

    for c in ("A", "B"):
        r = np.array(rows[c])
        tot, post, hand = r[:, 0], r[:, 1], r[:, 2].astype(bool)
        print(f"{c} {'記録あり' if c == 'B' else '記録なし'}: n={len(r)}  手の検出 {hand.mean() * 100:.0f}%")
        print(f"   全体（取得→記録）      {pct(tot)}")
        print(f"   全体（手ありのみ）     {pct(tot[hand])}")
        print(f"   推論後（推論完了→記録）{pct(post)}")
        print(f"   推論（取得→推論完了）  {pct(tot - post)}")
    print(f"（記録の出力先: {td}。イベントログ {w.lines} 行、印 {len(marks.saved)} 個）")


if __name__ == "__main__":
    main()
