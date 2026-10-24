"""ランドマーク列の記録と再生（JSON Lines）。

1行目はヘッダ {"format": "aircontroller-landmarks", "version": 1, ...}、
2行目以降は1フレーム1行。手が検出されなかったフレームも記録する（見失いの再現に必要）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator

import numpy as np

from .types import Hand, LandmarkFrame

FORMAT = "aircontroller-landmarks"
VERSION = 2  # 2: フレームに active（有効化の状態）を追加。1 は active=False として読む


def _round(a: np.ndarray) -> list:
    return np.round(a.astype(np.float64), 6).tolist()


def frame_to_dict(f: LandmarkFrame) -> dict:
    return {
        "seq": f.seq,
        "t_capture_ns": f.t_capture_ns,
        "t_infer_ns": f.t_infer_ns,
        "image_size": list(f.image_size),
        "active": f.active,
        "hands": [
            {
                "handedness": h.handedness,
                "score": round(h.score, 6),
                "landmarks": _round(h.landmarks),
                "world": _round(h.world),
            }
            for h in f.hands
        ],
    }


def frame_from_dict(d: dict) -> LandmarkFrame:
    return LandmarkFrame(
        seq=d["seq"],
        t_capture_ns=d["t_capture_ns"],
        t_infer_ns=d["t_infer_ns"],
        image_size=tuple(d["image_size"]),
        active=d.get("active", False),
        hands=tuple(
            Hand(
                handedness=h["handedness"],
                score=h["score"],
                landmarks=np.asarray(h["landmarks"], dtype=np.float64),
                world=np.asarray(h["world"], dtype=np.float64),
            )
            for h in d["hands"]
        ),
    )


class Recorder:
    def __init__(self, path: str | Path, meta: dict | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self.path.open("w")
        header = {"format": FORMAT, "version": VERSION, "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
        header.update(meta or {})
        self._fp.write(json.dumps(header) + "\n")
        self.count = 0

    def write(self, f: LandmarkFrame) -> None:
        self._fp.write(json.dumps(frame_to_dict(f), separators=(",", ":")) + "\n")
        self.count += 1

    def close(self) -> None:
        if not self._fp.closed:
            self._fp.close()


def read_header(path: str | Path) -> dict:
    with Path(path).open() as fp:
        header = json.loads(fp.readline())
    if header.get("format") != FORMAT:
        raise ValueError(f"{path}: ランドマーク記録ファイルではありません")
    return header


def iter_frames(path: str | Path) -> Iterator[LandmarkFrame]:
    with Path(path).open() as fp:
        header = json.loads(fp.readline())
        if header.get("format") != FORMAT:
            raise ValueError(f"{path}: ランドマーク記録ファイルではありません")
        for line in fp:
            if line.strip():
                yield frame_from_dict(json.loads(line))


def replay(path: str | Path, realtime: bool = False) -> Iterator[LandmarkFrame]:
    """記録を再生する。realtime=True なら記録時の取得間隔どおりに待つ。"""
    start_wall = None
    start_rec = None
    for f in iter_frames(path):
        if realtime:
            if start_wall is None:
                start_wall, start_rec = time.perf_counter_ns(), f.t_capture_ns
            wait = (f.t_capture_ns - start_rec) - (time.perf_counter_ns() - start_wall)
            if wait > 0:
                time.sleep(wait / 1e9)
        yield f
