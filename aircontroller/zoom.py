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
    """ズームの段の送り方（TASKS-20261025 2.1）: 閾値を超えて一定時間保ったら 1 段だけ送り、不感帯に戻るまで次を送らない。

    以前は不感帯の外で、離れるほど速く連続して送っていた（0.8〜2.5 段/秒）。今は、ピースの戻る・進みやいいねと同じ
    「とどまって発火」（SPEC 13.18）: 棒の長さで zoom_fire_level を超えたら計時を始め、同じ向きのまま
    zoom_fire_hold_ms 経ったら発火する。計時中は、閾値を下回っても不感帯の外にいる間は続ける（閾値付近の行き来で
    やり直さない）。発火した後は、変化が zoom_dead × zoom_rearm_frac より内側に戻るまで再発火しない（再武装）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._filter = OneEuroFilter(cfg.zoom_min_cutoff, cfg.zoom_beta, 1.0)
        self.reset()

    def reset(self) -> None:
        self._filter.reset()
        self._origin: float | None = None
        self._armed = True
        self._since: float | None = None  # 発火の計時を始めた時刻
        self._side = 0  # 計時中の向き（+1 イン / -1 アウト）
        self.value = 0.0  # 正面からの変化（ズームインが正）。オーバーレイ用
        self.level = 0.0  # -1..1（不感帯の外での割合）
        self.progress = 0.0  # 発火までの経過（0..1）。オーバーレイ用
        self.fired = 0  # 発火した回数（オーバーレイの、発火の表示に使う）
        self.last_fire_ns: int | None = None
        self.last_fire_dir = 0

    def enter(self) -> None:
        self.reset()

    @property
    def armed(self) -> bool:
        return self._armed

    def update(self, hand: Hand | None, image_size, t_ns: int, active: bool) -> list[Event]:
        c = self.cfg
        t_s = t_ns / 1e9
        if hand is None:
            return []
        raw = zoom_signals(hand, image_size)[c.zoom_signal] * c.zoom_sign
        if self._origin is None:
            self._origin = raw
        v = self._filter(raw - self._origin, t_s)
        self.value = v
        a = abs(v)
        side = 1 if v > 0 else -1
        u = min(1.0, max(0.0, (a - c.zoom_dead) / max(c.zoom_max - c.zoom_dead, 1e-9)))
        self.level = u * side if a >= c.zoom_dead else 0.0
        if a < c.zoom_dead * c.zoom_rearm_frac:
            self._armed = True  # 中央に戻った
        if a < c.zoom_dead or side != self._side:
            self._since = None
        if not self._armed or (self._since is None and u < c.zoom_fire_level):
            self.progress = 0.0
            return []
        if self._since is None:
            self._since, self._side = t_s, side
        held_ms = (t_s - self._since) * 1000
        self.progress = min(1.0, held_ms / c.zoom_fire_hold_ms) if c.zoom_fire_hold_ms > 0 else 1.0
        if held_ms < c.zoom_fire_hold_ms:
            return []
        self._armed = False
        self._since = None
        self.progress = 0.0
        self.fired += 1
        self.last_fire_ns, self.last_fire_dir = t_ns, side
        return [Event("zoom", t_ns, {"dir": "in" if side > 0 else "out"})] if active else []
