"""層の間で受け渡すデータ型。

入力層 → ジェスチャー層は LandmarkFrame、ジェスチャー層 → 出力層は Event だけを渡す。
どちらも純粋なデータで、カメラや OS の API には依存しない。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# MediaPipe のランドマーク番号（よく使うものだけ）
WRIST = 0
THUMB_MCP = 2
THUMB_TIP = 4
INDEX_MCP = 5
INDEX_TIP = 8
MIDDLE_MCP = 9
MIDDLE_TIP = 12
RING_MCP = 13
PINKY_MCP = 17
PALM = (WRIST, INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP)  # 手のひら中心の計算に使う点


@dataclass(frozen=True)
class Hand:
    handedness: str  # "Left" / "Right"（MediaPipe の判定。カメラ画像は非反転のまま渡す）
    score: float  # handedness の信頼度
    landmarks: np.ndarray  # (21, 3) 画像座標。x, y は 0..1 に正規化、z は相対深度
    world: np.ndarray  # (21, 3) world landmarks。手の幾何中心を原点にしたメートル単位


@dataclass(frozen=True)
class LandmarkFrame:
    seq: int  # カメラのフレーム番号（捨てたフレームも数えるので、飛びが出る）
    t_capture_ns: int  # フレーム取得時刻（time.perf_counter_ns）
    t_infer_ns: int  # 推論完了時刻
    image_size: tuple[int, int]  # (width, height)
    hands: tuple[Hand, ...] = field(default_factory=tuple)
    active: bool = False  # 有効化（¥ キー押下中など）の状態。フレーム処理時点の値を入力として記録する


@dataclass(frozen=True)
class Event:
    """ジェスチャー層が出すイベント。t_ns はトリガーになったフレームの取得時刻。"""

    kind: str
    t_ns: int
    data: dict = field(default_factory=dict)
