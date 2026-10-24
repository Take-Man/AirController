"""スレッドごとの CPU 時間（TASKS-20261025 第5節）。各ループが mark(name) を呼ぶと、そのスレッドの累積 CPU 時間
（time.thread_time）を記録する。スレッドが作り直されても（カメラの再起動など）、名前ごとに合計する。
MediaPipe の内部スレッドや AVFoundation のスレッドは数えられないので、プロセス全体との差を「その他」とする。"""
from __future__ import annotations

import threading
import time

_cpu: dict[tuple[str, int], float] = {}


def mark(name: str) -> None:
    _cpu[(name, threading.get_native_id())] = time.thread_time()


def totals() -> dict[str, float]:
    out: dict[str, float] = {}
    for (name, _), v in list(_cpu.items()):
        out[name] = out.get(name, 0.0) + v
    return out
