"""遅延ログ（logs/latency-*.csv）の要約。カメラの停止・再起動（seq の巻き戻り）で区間を分けて数える。

使い方: .venv/bin/python tools/latency_summary.py logs/latency-YYYYmmdd-HHMMSS.csv [> logs/summary-....txt]
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np


def pct(a: np.ndarray) -> str:
    if len(a) == 0:
        return "なし"
    p50, p95, p99 = np.percentile(a, [50, 95, 99])
    return f"p50 {p50:.1f}ms  p95 {p95:.1f}ms  p99 {p99:.1f}ms  max {a.max():.1f}ms  (n={len(a)})"


def main(path: str) -> None:
    rows = list(csv.DictReader(Path(path).open()))
    seq = np.array([int(r["seq"]) for r in rows])
    t = np.array([int(r["t_capture_ns"]) for r in rows])
    total = np.array([float(r["total_ms"]) for r in rows])
    infer = np.array([float(r["infer_ms"]) for r in rows])
    dropped = np.array([int(r["dropped"]) for r in rows])
    hands = np.array([int(r["n_hands"]) for r in rows])
    active = np.array([int(r["active"]) for r in rows]).astype(bool)
    posted = np.array([int(r.get("posted") or 0) for r in rows])

    # 区間: seq が巻き戻ったところ（カメラの再起動）で切る。区間の先頭の dropped は前区間からの差なので数えない
    starts = [0] + [i for i in range(1, len(seq)) if seq[i] <= seq[i - 1]]
    bounds = list(zip(starts, starts[1:] + [len(seq)]))
    cam_s = sum((t[e - 1] - t[s]) / 1e9 for s, e in bounds)
    camera_frames = sum(int(seq[e - 1] - seq[s] + 1) for s, e in bounds)
    drop = dropped.copy()
    drop[starts] = 0

    # 有効化中の区間の長さ（連続する active フレームの間隔の和。1 秒を超える間隔は数えない）
    dt = np.diff(t) / 1e9
    act_pair = active[1:] & active[:-1] & (dt < 1.0)
    active_s = float(dt[act_pair].sum())
    toggles_on = int(((~active[:-1]) & active[1:]).sum() + active[0])

    out = [
        f"遅延ログの要約: {path}",
        f"記録の期間: {(t[-1] - t[0]) / 1e9 / 60:.1f} 分（壁時計）、カメラが動いていた時間 {cam_s / 60:.1f} 分、"
        f"カメラの起動 {len(bounds)} 回",
        f"処理したフレーム {len(rows)}（カメラのフレーム {camera_frames}）",
        f"有効化中: {active.sum()} フレーム、約 {active_s / 60:.1f} 分、有効化の回数 {toggles_on}",
        "",
        "[有効化中のソフトウェア遅延（取得 → 出力）]",
        "  全フレーム: " + pct(total[active]),
        "  CGEvent を送出したフレーム: " + pct(total[active & (posted > 0)]),
        "  推論（取得 → 推論完了）: " + pct(infer[active]),
        "[待機中（有効化オフ）の遅延]",
        "  全フレーム: " + pct(total[~active]),
        "",
        f"手の検出率: 有効化中 {hands[active].mean() * 100:.1f}%  待機中 {hands[~active].mean() * 100:.1f}%  "
        f"全体 {(hands > 0).mean() * 100:.1f}%",
        f"取りこぼし（推論が追いつかず飛ばしたフレーム）: 有効化中 {int(drop[active].sum())} "
        f"（有効化中の処理フレーム {active.sum()} に対し {drop[active].sum() / max(1, active.sum()) * 100:.2f}%）、"
        f"待機中 {int(drop[~active].sum())}（10fps への間引きを含む）",
        f"  有効化中に 1 フレーム以上飛ばしたフレーム: {int((drop[active] > 0).sum())}、2 以上: {int((drop[active] > 1).sum())}",
        f"イベント送出: 送出したフレーム {int((posted > 0).sum())}、CGEvent の総数 {int(posted.sum())}",
        "",
        "注: CGEvent の総数はカーソル移動を含む。イベントの種類（クリック等）はこのログには残っていない。",
        "    5 秒を超える取得の空白: " + ", ".join(f"{g:.0f}s" for g in dt[dt > 5]),
    ]
    print("\n".join(out))


if __name__ == "__main__":
    main(sys.argv[1])
