"""手の形の特徴量（ジェスチャーの候補を比べるため）。カメラや OS に依存しない。

どれも「指の形・向き」を表す、手の大きさや位置によらない量にしてある。
"""
from __future__ import annotations

import numpy as np

from .gesture import finger_straightness, palm_facing, thumb_fold_metric, thumb_up
from .types import INDEX_MCP, INDEX_TIP, MIDDLE_MCP, MIDDLE_TIP, PINKY_MCP, RING_MCP, THUMB_TIP, WRIST, Hand

FINGERS = {"index": INDEX_MCP, "middle": MIDDLE_MCP, "ring": RING_MCP, "pinky": PINKY_MCP}


def thumb_straightness(world: np.ndarray) -> float:
    """親指の付け根（CMC=1）→先（4）の直線距離 / 関節の長さの和。"""
    p = world[1:5]
    seg = float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())
    return float(np.linalg.norm(p[3] - p[0])) / seg if seg > 0 else 0.0


def thumb_extension(hand: Hand) -> float:
    """親指先→人差し指付け根の距離 / 手首→中指付け根（world）。親指を立てる・開くと大きい。"""
    w = hand.world
    ref = float(np.linalg.norm(w[MIDDLE_MCP] - w[WRIST]))
    return float(np.linalg.norm(w[THUMB_TIP] - w[INDEX_MCP])) / ref if ref > 0 else 0.0


def index_middle_gap(hand: Hand) -> float:
    """人差し指先と中指先の距離 / 人差し指付け根と中指付け根の距離（world）。揃えると 1 前後、開くと大きい。"""
    w = hand.world
    base = float(np.linalg.norm(w[MIDDLE_MCP] - w[INDEX_MCP]))
    return float(np.linalg.norm(w[INDEX_TIP] - w[MIDDLE_TIP])) / base if base > 0 else 0.0


def hand_features(hand: Hand) -> dict[str, float]:
    w = hand.world
    out = {f"{k}_straight": finger_straightness(w, m) for k, m in FINGERS.items()}
    out.update(thumb_straight=thumb_straightness(w), thumb_ext=thumb_extension(hand), thumb_up=thumb_up(hand),
               thumb_fold=thumb_fold_metric(hand), palm_z=palm_facing(hand), im_gap=index_middle_gap(hand),
               score=hand.score)
    return out
