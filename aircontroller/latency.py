"""フレーム時刻ログ（CSV）と集計。

列: seq, t_capture_ns, t_infer_ns, t_output_ns, infer_ms, total_ms, dropped, n_hands, score, active, posted
- t_output_ns: ジェスチャー層の処理が終わり、イベントを出力層へ渡した時刻。
  段階1以降は、CGEvent を送出し終えた時刻。
- posted: そのフレームで送出した CGEvent の数（0 なら t_output はジェスチャー層の処理完了時刻）。
- dropped: 直前に処理したフレームから飛ばしたフレーム数（最新フレームのみ処理した結果）。
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

COLUMNS = ["seq", "t_capture_ns", "t_infer_ns", "t_output_ns", "infer_ms", "total_ms", "dropped", "n_hands", "score",
           "active", "posted"]


class LatencyLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = self.path.open("w", newline="")
        self._w = csv.writer(self._fp)
        self._w.writerow(COLUMNS)
        self._last_seq: int | None = None
        self.total_ms: list[float] = []

    def write(self, seq: int, t_capture: int, t_infer: int, t_output: int, n_hands: int, score: float,
              active: bool = False, posted: int = 0) -> None:
        dropped = 0 if self._last_seq is None else max(0, seq - self._last_seq - 1)
        self._last_seq = seq
        infer_ms = (t_infer - t_capture) / 1e6
        total_ms = (t_output - t_capture) / 1e6
        self.total_ms.append(total_ms)
        self._w.writerow([seq, t_capture, t_infer, t_output, f"{infer_ms:.3f}", f"{total_ms:.3f}",
                          dropped, n_hands, f"{score:.4f}", int(active), posted])

    def close(self) -> None:
        if not self._fp.closed:
            self._fp.close()


def summarize(path: str | Path) -> dict:
    rows = list(csv.DictReader(Path(path).open()))
    if not rows:
        return {"frames": 0}
    total = np.array([float(r["total_ms"]) for r in rows])
    infer = np.array([float(r["infer_ms"]) for r in rows])
    t_cap = np.array([int(r["t_capture_ns"]) for r in rows])
    seq = np.array([int(r["seq"]) for r in rows])
    dropped = np.array([int(r["dropped"]) for r in rows])
    hands = np.array([int(r["n_hands"]) for r in rows])
    score = np.array([float(r["score"]) for r in rows])
    dur_s = (t_cap[-1] - t_cap[0]) / 1e9 if len(rows) > 1 else 0.0
    out = {
        "frames": len(rows),
        "duration_s": dur_s,
        "processed_fps": (len(rows) - 1) / dur_s if dur_s > 0 else float("nan"),
        "camera_fps": (seq[-1] - seq[0]) / dur_s if dur_s > 0 else float("nan"),
        "dropped_total": int(dropped.sum()),
        "hand_ratio": float((hands > 0).mean()),
        "score_mean": float(score[hands > 0].mean()) if (hands > 0).any() else float("nan"),
    }
    posted = np.array([int(r.get("posted") or 0) for r in rows])
    out["posted_frames"] = int((posted > 0).sum())
    series = [("infer_ms", infer), ("total_ms", total)]
    if (posted > 0).sum() >= 2:
        series.append(("posted_total_ms", total[posted > 0]))
    for name, a in series:
        out[name] = {"p50": float(np.percentile(a, 50)), "p95": float(np.percentile(a, 95)),
                     "p99": float(np.percentile(a, 99)), "max": float(a.max()), "mean": float(a.mean())}
    return out


def format_summary(s: dict) -> str:
    if not s.get("frames"):
        return "フレームなし"
    lines = [
        f"フレーム数 {s['frames']}  時間 {s['duration_s']:.1f}s  処理 {s['processed_fps']:.1f}fps  "
        f"カメラ {s['camera_fps']:.1f}fps  捨てたフレーム {s['dropped_total']}",
        f"手の検出率 {s['hand_ratio']*100:.1f}%  平均信頼度 {s['score_mean']:.3f}",
    ]
    for name, label in (("infer_ms", "取得→推論完了"), ("total_ms", "取得→出力（全フレーム）"),
                        ("posted_total_ms", f"取得→CGEvent 送出（送出した {s.get('posted_frames', 0)} フレーム）")):
        if name not in s:
            continue
        d = s[name]
        lines.append(f"{label}: p50 {d['p50']:.1f}ms  p95 {d['p95']:.1f}ms  p99 {d['p99']:.1f}ms  "
                     f"max {d['max']:.1f}ms")
    return "\n".join(lines)
