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


def rss_mb() -> float | None:
    """今の常駐サイズ（MB）。mach の task_info（MACH_TASK_BASIC_INFO）で取る。取れなければ None。"""
    import ctypes
    import ctypes.util

    class TimeValue(ctypes.Structure):
        _fields_ = [("seconds", ctypes.c_int), ("microseconds", ctypes.c_int)]

    class MachTaskBasicInfo(ctypes.Structure):
        _pack_ = 4
        _fields_ = [("virtual_size", ctypes.c_uint64), ("resident_size", ctypes.c_uint64),
                    ("resident_size_max", ctypes.c_uint64), ("user_time", TimeValue), ("system_time", TimeValue),
                    ("policy", ctypes.c_int), ("suspend_count", ctypes.c_int)]

    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        libc.mach_task_self.restype = ctypes.c_uint
        info = MachTaskBasicInfo()
        count = ctypes.c_uint(ctypes.sizeof(info) // 4)
        kr = libc.task_info(libc.mach_task_self(), 20, ctypes.byref(info), ctypes.byref(count))  # 20 = MACH_TASK_BASIC_INFO
        return info.resident_size / 2**20 if kr == 0 else None
    except (OSError, AttributeError):
        return None
