"""負荷の計測の集計（TASKS-20261025 第5節）。run のイベントログの状態の行（10 秒ごと）と、tools/monitor.py の記録を、
状態ごとにまとめて logs/load-YYYYmmdd.md に書く。

状態（状態の行から決める）:
  1. 有効・手あり（30Hz、操作中）  2. 有効・手なし（30Hz）  3. オフ（1Hz。確認の窓を含む）  4. カメラ停止（No signal）
  有効の状態は、オーバーレイを畳んだか広げたかでも分ける（5.）。
各状態: 時間、プロセスの CPU（1コアに対する %）とスレッド別、ソフトウェア遅延（10 秒ごとの p95 の中央値）、
取りこぼし（推論が追いつかず飛ばしたフレームの割合。オフの間は間引きを含むので出さない）、
バッテリー駆動時の Mac 全体の消費電力（monitor、外部電源なしの行だけ）、熱の警告。
メモリ: monitor の RSS の推移（1 時間あたりの増加）と、状態の行の最大 RSS。

使い方: .venv/bin/python tools/load_report.py logs/events-....jsonl [logs/monitor-....csv] [--out logs/load-YYYYmmdd.md]
"""
from __future__ import annotations

import csv
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
STATES = ["1. 有効・手あり（30Hz）", "2. 有効・手なし（30Hz）", "3. オフ（1Hz）", "4. カメラ停止"]


def state_of(r: dict) -> str:
    if r.get("camera") != "running":
        return STATES[3]
    if not r.get("active"):
        return STATES[2]
    return STATES[0] if r.get("hand") else STATES[1]


def parse_wall(s: str) -> float:
    return datetime.fromisoformat(s[:19]).timestamp() + (float("0." + s[20:]) if len(s) > 20 else 0.0)


def main(argv: list[str]) -> None:
    out = None
    if "--out" in argv:
        i = argv.index("--out")
        out = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    ev_paths = [Path(a) for a in argv if a.endswith(".jsonl")]
    mon_paths = [Path(a) for a in argv if a.endswith(".csv")]
    status = []
    for p in ev_paths:
        for line in p.open():
            r = json.loads(line)
            if r.get("type") == "status":
                r["_wall"] = parse_wall(r["wall"])
                status.append(r)
    if not status:
        print("状態の行（type = status）がありません。run を status_s > 0 で動かしてください")
        return
    mon = []
    for p in mon_paths:
        for r in csv.DictReader(p.open()):
            r["_wall"] = parse_wall(r["wall"])
            mon.append(r)

    groups: dict[str, list] = defaultdict(list)
    prev_threads = None
    for r in status:
        st = state_of(r)
        if st in STATES[:2]:
            sub = f"{st} / オーバーレイ{'畳む' if r.get('collapsed') else '広げる'}"
            r["_sub"] = sub
        th = r.get("threads_cpu_s", {})
        if prev_threads is not None:
            r["_thr_pct"] = {k: max(0.0, (v - prev_threads.get(k, 0.0)) / r["dt_s"] * 100) for k, v in th.items()}
        prev_threads = th
        groups[st].append(r)
        if "_sub" in r:
            groups[r["_sub"]].append(r)

    def mon_rows(rows):
        """その状態の時間帯（状態の行の前 dt 秒）に入る monitor の行（外部電源なしのものだけ電力に使う）。"""
        spans = [(r["_wall"] - r["dt_s"], r["_wall"]) for r in rows]
        return [m for m in mon if any(a <= m["_wall"] <= b for a, b in spans)]

    L = [f"# 負荷の計測（{time.strftime('%Y-%m-%d')}）", "",
         f"- イベントログ: {', '.join(p.name for p in ev_paths)}",
         f"- monitor: {', '.join(p.name for p in mon_paths) or 'なし'}",
         f"- 期間: {status[0]['wall']} 〜 {status[-1]['wall']}（状態の行 {len(status)}）", "",
         "| 状態 | 時間 | CPU 平均（1コア比） | 遅延 p95（中央値） | 取りこぼし | 消費電力（電池、Mac 全体） | 熱の警告 |",
         "|---|---|---|---|---|---|---|"]
    keys = [k for k in STATES] + sorted(k for k in groups if "/" in k)
    detail = []
    for k in keys:
        rows = groups.get(k, [])
        if not rows:
            L.append(f"| {k} | 0 | - | - | - | - | - |")
            continue
        dur = sum(r["dt_s"] for r in rows)
        cpu = np.average([r["cpu_pct"] for r in rows], weights=[r["dt_s"] for r in rows])
        p95 = [r["lat_p95"] for r in rows if "lat_p95" in r]
        fr, dr = sum(r["frames"] for r in rows), sum(r["dropped"] for r in rows)
        drop = "-" if k == STATES[2] or k == STATES[3] else f"{dr / max(1, fr + dr) * 100:.2f}%"
        mr = mon_rows(rows)
        pw = [float(m["power_w"]) for m in mr if m.get("power_w") and m.get("external") == "No"]
        th = sorted({m["therm"] for m in mr if m.get("therm") and m["therm"] != "警告なし"})
        L.append(f"| {k} | {dur / 60:.1f}分 | {cpu:.1f}% | {np.median(p95):.1f}ms | {drop} | "
                 + (f"{np.mean(pw):.2f}W（{len(pw)} 点）" if pw else "-（電池の記録なし）") + f" | {'; '.join(th) or 'なし'} |"
                 if p95 else
                 f"| {k} | {dur / 60:.1f}分 | {cpu:.1f}% | - | {drop} | "
                 + (f"{np.mean(pw):.2f}W（{len(pw)} 点）" if pw else "-（電池の記録なし）") + f" | {'; '.join(th) or 'なし'} |")
        thr = defaultdict(list)
        for r in rows:
            for n, v in r.get("_thr_pct", {}).items():
                thr[n].append(v)
        if thr and "/" not in k:
            parts = [f"{n} {np.mean(v):.1f}%" for n, v in sorted(thr.items(), key=lambda x: -np.mean(x[1]))]
            known = sum(np.mean(v) for v in thr.values())
            parts.append(f"その他（MediaPipe の内部など）{max(0.0, cpu - known):.1f}%")
            detail.append(f"- {k}: " + "、".join(parts))
    L += ["", "## スレッド別の CPU（1コア比の平均）", ""] + detail
    # メモリ
    L += ["", "## メモリ", ""]
    mx = [r["maxrss_mb"] for r in status]
    L.append(f"- 状態の行の最大 RSS（ru_maxrss）: 最初 {mx[0]:.0f}MB → 最後 {mx[-1]:.0f}MB")
    rss = [(m["_wall"], float(m["rss_mb"])) for m in mon if m.get("rss_mb")]
    if len(rss) > 10:
        t = np.array([a for a, _ in rss]) - rss[0][0]
        v = np.array([b for _, b in rss])
        slope = np.polyfit(t / 3600, v, 1)[0]
        half = len(v) // 2
        L.append(f"- monitor の RSS: 最初 {v[0]:.0f}MB、最後 {v[-1]:.0f}MB、最大 {v.max():.0f}MB、"
                 f"期間 {t[-1] / 3600:.2f} 時間、傾き {slope:+.1f}MB/時、前半の中央値 {np.median(v[:half]):.0f}MB → "
                 f"後半 {np.median(v[half:]):.0f}MB")
    text = "\n".join(L) + "\n"
    print(text)
    if out:
        out.write_text(text)
        print(f"書きました: {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
