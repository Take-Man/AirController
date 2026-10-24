"""ズームモード（段階6、SPEC 13.20）: 手の甲をカメラに向けて指を伸ばした姿勢で入り、手招きのように
指先をカメラから遠ざけるとズームイン、近づけるとズームアウトする。

指先の前後の動きの測り方は、候補を 3つ持ち、learn-zoom で利用者の動きに合うものを選ぶ。
- "depth": 4本の指先と付け根の奥行きの差（world の z。奥が正）/ 手首→中指付け根の長さ
- "curl": 4本の指の曲がり具合（1 − 真っすぐさ の平均）。手招きで指を曲げると大きくなる
- "length": 画像上の指の長さ（付け根→先）/ 画像上の手首→中指付け根。指先が前後どちらへ倒れても短くなるので、
  符号は区別できない（片側だけの動きのときの参考）
どれも、モードに入ったときの値からの変化を使い、zoom_sign で「ズームインが正」になるようにそろえる。
"""
from __future__ import annotations

import numpy as np

from .filters import OneEuroFilter
from .types import INDEX_MCP, MIDDLE_MCP, PINKY_MCP, RING_MCP, WRIST, Event, Hand

MCPS = (INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP)


def zoom_signals(hand: Hand, image_size: tuple[int, int]) -> dict[str, float]:
    from .gesture import finger_straightness

    w = hand.world
    ref_w = float(np.linalg.norm(w[MIDDLE_MCP] - w[WRIST])) or 1.0
    depth = float(np.mean([w[m + 3, 2] - w[m, 2] for m in MCPS])) / ref_w
    curl = float(np.mean([1.0 - finger_straightness(w, m) for m in MCPS]))
    p = hand.landmarks[:, :2] * np.asarray(image_size, dtype=np.float64)
    ref_i = float(np.linalg.norm(p[MIDDLE_MCP] - p[WRIST])) or 1.0
    length = float(np.mean([np.linalg.norm(p[m + 3] - p[m]) for m in MCPS])) / ref_i
    return {"depth": depth, "curl": curl, "length": length}


class ZoomController:
    def __init__(self, cfg):
        self.cfg = cfg
        self._filter = OneEuroFilter(cfg.zoom_min_cutoff, cfg.zoom_beta, 1.0)
        self.reset()

    def reset(self) -> None:
        self._filter.reset()
        self._origin: float | None = None
        self._last_t: float | None = None
        self._acc = 0.0  # ズームの段数の端数（持ち越す）
        self.value = 0.0  # 正面からの変化（ズームインが正）。オーバーレイ用
        self.level = 0.0  # -1..1（不感帯の外での割合）

    def enter(self) -> None:
        self.reset()

    def update(self, hand: Hand | None, image_size, t_ns: int, active: bool) -> list[Event]:
        c = self.cfg
        t_s = t_ns / 1e9
        dt = t_s - self._last_t if self._last_t is not None else 0.0
        self._last_t = t_s
        if hand is None:
            return []
        raw = zoom_signals(hand, image_size)[c.zoom_signal] * c.zoom_sign
        if self._origin is None:
            self._origin = raw
        v = self._filter(raw - self._origin, t_s)
        self.value = v
        a = abs(v)
        if a < c.zoom_dead:
            self.level = 0.0
            self._acc = 0.0
            return []
        u = min(1.0, (a - c.zoom_dead) / max(c.zoom_max - c.zoom_dead, 1e-9))
        self.level = u if v > 0 else -u
        # 一定速度に近い操作: 不感帯の外では、離れるほど速く（最大 zoom_max_rate 段/秒）ズームの段を送る
        rate = c.zoom_min_rate + (c.zoom_max_rate - c.zoom_min_rate) * u
        self._acc += rate * dt
        events: list[Event] = []
        while self._acc >= 1.0:
            self._acc -= 1.0
            if active:
                events.append(Event("zoom", t_ns, {"dir": "in" if v > 0 else "out"}))
        return events
