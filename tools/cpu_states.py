"""状態ごとのプロセスの CPU 使用率を測る（TASKS-20261025 3.2、第5節の準備）。管理者権限は要らない。

カメラと推論を、推論の頻度だけを変えて、各状態 seconds 秒ずつ動かし、プロセスの CPU 時間（os.times の user+sys）の
増分 / 経過時間 を「1コアに対する %」で出す。カメラの取得は常に 30fps（内蔵カメラは 30fps 固定で、下げられない）。
  - 30Hz: 操作中と同じく毎フレーム推論する
  - 1Hz : オフの間（off_detect_hz）
  - camera only: 推論しない（取得だけの負荷）
ジェスチャー層・オーバーレイ・CGEvent は含まない（それらは 1ms 未満/フレーム）。

使い方: .venv/bin/python tools/cpu_states.py [--seconds 60]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aircontroller.__main__ import _tracker_config, load_config  # noqa: E402
from aircontroller.input import CameraConfig, VisionPipeline  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60.0)
    args = ap.parse_args()
    cfg = load_config(None)
    interval = [0.0]
    count = [0]

    def on_frame(f):
        count[0] += 1

    pipe = VisionPipeline(CameraConfig(**cfg["camera"]), _tracker_config(cfg), on_frame, lambda: interval[0])
    pipe.start()
    time.sleep(3.0)
    rows = []
    try:
        for name, iv in (("30Hz（操作中）", 0.0), ("1Hz（オフ）", 1.0), ("推論なし（取得だけ）", 3600.0)):
            interval[0] = iv
            time.sleep(2.0)
            c0, t0, n0 = os.times(), time.monotonic(), count[0]
            print(f"{name} を {args.seconds:.0f}秒測っています…")
            time.sleep(args.seconds)
            c1, t1, n1 = os.times(), time.monotonic(), count[0]
            cpu = (c1.user - c0.user + c1.system - c0.system) / (t1 - t0) * 100
            rows.append((name, cpu, (n1 - n0) / (t1 - t0)))
    finally:
        pipe.stop()
    print("\n状態                  CPU（1コアに対する %）  推論の回数/秒")
    for name, cpu, hz in rows:
        print(f"{name:20s} {cpu:8.1f}%  {hz:8.1f}")


if __name__ == "__main__":
    main()
