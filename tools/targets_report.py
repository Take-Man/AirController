"""的のテスト（tools/targets.html）の結果をまとめる（TASKS-20261027-package 第2節）。手順は CHECKLIST-targets.md。

targets.html の「JSON を保存」で書き出した結果（targets-YYYYmmdd-HHMMSS-<操作>.json）を読み、操作（AirController / トラックパッド）ごとに、
回ごとの表と、回をまたいだ中央値とばらつき（最小〜最大）を logs/targets-YYYYmmdd.md に書く。
条件として、次を自動で添える:
  - 設定のスナップショットの名前: calibration.toml の中身と同じ、名前を付けた状態（settings-history/named/）か、最初に現れたスナップショット
  - 操作範囲（自動補正を含む）、ソフトウェア遅延 p95、CPU: 測った時刻のイベントログ（logs/events-*.jsonl、10 秒ごとの状態の行）から
  - 主な設定の値（ピンチの閾値、操作範囲の上限、カーソルの平滑化、使う手）
bit/s は targets.html と同じ式（Fitts の ID = log2(D/W + 1) を時間で割り、大きさごとの平均を、さらに大きさで平均）。

使い方: .venv/bin/python tools/targets_report.py logs/targets/targets-*.json [--out logs/targets-YYYYmmdd.md]
"""
from __future__ import annotations

import json
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEVICE_JA = {"aircontroller": "AirController", "trackpad": "トラックパッド"}
# 前回（STATUS 第5節）。操作範囲・ピンチの閾値・平滑化などが今と違うので、同じ条件の比較ではない
PREVIOUS = [("校正前（2026-10-24）", "174.4", "27", "—", "0.97"),
            ("校正後（2026-10-24。操作範囲の幅 0.71）", "91.0", "32", "19", "1.49")]


def wall(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _diff(a: dict, b: dict) -> list[str]:
    keys = {(s, k) for d in (a, b) for s, v in d.items() if isinstance(v, dict) for k in v}
    return sorted(f"{s}.{k}" for s, k in keys if a.get(s, {}).get(k) != b.get(s, {}).get(k))


def settings_name() -> str:
    """calibration.toml と同じ中身の設定の名前（名前を付けた状態、無ければ最初に現れたスナップショット）。
    名前を付けた状態と一致しなければ、いちばん近いもの（違う項目が 3 つ以下）との違いも添える。"""
    from aircontroller.__main__ import settings_store

    store = settings_store()
    cal = store.layer("calibration")
    named = {n: tomllib.loads((store.named / f"{n}.toml").read_text()).get("calibration", {}) for n in store.named_states()}
    for n, c in named.items():
        if c == cal:
            return n
    name = next((s["id"] for s in store.snapshots() if tomllib.loads(s["path"].read_text()).get("calibration", {}) == cal),
                "（履歴にない）")
    near = sorted((len(d), n, d) for n, c in named.items() if len(d := _diff(cal, c)) <= 3)
    return name + (f"（名前を付けた状態「{near[0][1]}」と {', '.join(near[0][2])} だけ違う）" if near else "")


def status_rows(start: datetime, end: datetime) -> list[dict]:
    """測っていた間（前後 10 秒）のイベントログの状態の行。"""
    out = []
    for p in sorted((ROOT / "logs").glob("events-*.jsonl")):
        try:
            if datetime.fromtimestamp(p.stat().st_mtime) < start:
                continue
        except OSError:
            continue
        with p.open() as fp:
            for line in fp:
                if '"status"' not in line:
                    continue
                r = json.loads(line)
                if r.get("type") == "status" and start.timestamp() - 10 <= wall(r["wall"]).timestamp() <= end.timestamp() + 10:
                    out.append(r)
    return out


def run_conditions(res: dict) -> dict:
    rows = status_rows(wall(res["start"]), wall(res["end"]))
    roi = [r["roi"] for r in rows if r.get("roi")]
    lat = [r["lat_p95"] for r in rows if r.get("lat_p95") is not None and r.get("active")]
    cpu = [r["cpu_pct"] for r in rows if r.get("active")]
    return {"status_n": len(rows),
            "roi": tuple(float(np.median([v[i] for v in roi])) for i in range(3)) if roi else None,
            "roi_range": (min(v[2] for v in roi), max(v[2] for v in roi)) if roi else None,
            "lat_p95": float(np.median(lat)) if lat else None,
            "cpu": float(np.median(cpu)) if cpu else None,
            "active": any(r.get("active") for r in rows)}


def summarize(res: dict) -> dict:
    """targets.html と同じ計算（練習の 1 回は除く）。JSON の summary と一致するはず。"""
    done = [t for t in res["trials"] if not t["practice"]]
    by = {}
    for s in res["sizes"]:
        ts = [t for t in done if t["size"] == s]
        times = sorted(t["time_ms"] for t in ts)
        by[s] = {"median_s": times[len(times) // 2] / 1000, "misses": sum(t["misses"] for t in ts),
                 "bits": float(np.mean([np.log2(t["dist"] / t["size"] + 1) / (max(t["time_ms"], 1) / 1000) for t in ts]))}
    return {"total_s": sum(t["time_ms"] for t in done) / 1000, "misses": sum(t["misses"] for t in done),
            "bits": float(np.mean([v["bits"] for v in by.values()])), "by": by,
            "miss_px": [p for t in done for p in (t.get("miss_px") or [])]}


def f(v, fmt="{:.1f}"):
    return "—" if v is None else fmt.format(v)


def spread(vals, fmt="{:.1f}") -> str:
    if not vals:
        return "—"
    return f"{fmt.format(float(np.median(vals)))}（{fmt.format(min(vals))}〜{fmt.format(max(vals))}）"


def main(argv: list[str]) -> None:
    out = None
    if "--out" in argv:
        i = argv.index("--out")
        out = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    paths = sorted(Path(a) for a in argv)
    if not paths:
        sys.exit(__doc__)
    runs = []
    for p in paths:
        res = json.loads(p.read_text())
        if res.get("format") != "aircontroller-targets":
            sys.exit(f"{p}: targets.html の結果ではありません")
        runs.append({"path": p, "res": res, "sum": summarize(res), "cond": run_conditions(res)})
    runs.sort(key=lambda r: r["res"]["start"])
    day = runs[0]["res"]["start"][:10].replace("-", "")
    out = out or ROOT / "logs" / f"targets-{day}.md"

    from aircontroller.__main__ import load_config

    name = settings_name()
    cfg = load_config(None, calibration=False)
    from aircontroller.__main__ import settings_store

    eff = settings_store().load_state({"calibration": settings_store().layer("calibration")})
    sizes = runs[0]["res"]["sizes"]
    L = [f"# 的のテストの結果（{runs[0]['res']['start'][:10]}、tools/targets.html）", "",
         f"作成: {time.strftime('%Y-%m-%d %H:%M')}（tools/targets_report.py）。結果の JSON: "
         + "、".join(r["path"].name for r in runs), "",
         "**前回（2026-10-24）とは同じ条件の比較ではない。** 操作範囲（幅 0.71 → 今は上限 0.6）、ピンチの閾値（成立 0.205 など）、"
         "カーソルの平滑化（beta 0.02）、ピンチの判定（13.37, 13.38）、左右の扱い（13.53）などが変わっている。今回は今の設定での基準を作る。", "",
         "## 条件", "",
         f"- 設定のスナップショット: **{name}**（calibration.toml と同じ中身。測っている間に設定を変えていない前提）",
         f"- 主な値: 使う手 {eff['gesture'].get('hand')}、ピンチ 成立 {eff['click']['pinch_on']} / 解除 {eff['click']['pinch_off']}"
         f" / ドラッグ中の解除 {eff['click']['drag_off']}、操作範囲の幅の上限 {eff['cursor']['roi_width_max']}、"
         f"カーソルの平滑化 min_cutoff {eff['cursor']['cursor_min_cutoff']} / beta {eff['cursor']['cursor_beta']}",
         f"- カメラ: {cfg['camera']['width']}x{cfg['camera']['height']} {cfg['camera']['fps']}fps（{cfg['camera']['backend']}）", ""]
    L += ["| 回 | 操作 | 開始 | 距離 | 明るさ | ウィンドウ（画面） | ブラウザ | 操作範囲（中心・幅） | 遅延 p95 | CPU |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(runs, 1):
        res, c = r["res"], r["cond"]
        ua = res["user_agent"]
        browser = "Safari" if "Safari" in ua and "Chrome" not in ua else "Chrome" if "Chrome" in ua else "Firefox" if "Firefox" in ua else ua[:20]
        roi = (f"({c['roi'][0]:.3f}, {c['roi'][1]:.3f}) 幅 {c['roi'][2]:.3f}" if c["roi"] else "—（ログなし）") \
            if res["device"] == "aircontroller" else "—"
        L.append(f"| {i} | {DEVICE_JA.get(res['device'], res['device'])} | {res['start'][11:19]} | {f(res.get('distance_cm'), '{:.0f}cm')} | "
                 f"{res.get('light') or '—'} | {res['window'][0]}x{res['window'][1]}（{res['screen'][0]}x{res['screen'][1]}"
                 f"{'、全画面' if res.get('fullscreen') else ''}） | {browser} | {roi} | "
                 f"{f(c['lat_p95'], '{:.1f}ms') if res['device'] == 'aircontroller' else '—'} | "
                 f"{f(c['cpu'], '{:.0f}%') if res['device'] == 'aircontroller' else '—'} |")
    notes = [f"{i}: {r['res']['note']}" for i, r in enumerate(runs, 1) if r["res"].get("note")]
    if notes:
        L += ["", "メモ: " + "／".join(notes)]
    warn = [i for i, r in enumerate(runs, 1) if r["res"]["device"] == "aircontroller" and not r["cond"]["active"]]
    if warn:
        L += ["", f"注意: 回 {', '.join(map(str, warn))} の時刻に、有効（オン）の run の状態の行が見つからない（イベントログを書かない設定か、別の Mac）。"]

    L += ["", "## 回ごと", "", "| 回 | 操作 | 合計 | ミス | bit/s | " + " | ".join(f"{s}px 中央値・ミス" for s in sizes) + " |",
          "|---|---|---|---|---|" + "---|" * len(sizes)]
    for i, r in enumerate(runs, 1):
        s = r["sum"]
        L.append(f"| {i} | {DEVICE_JA.get(r['res']['device'])} | {s['total_s']:.1f}秒 | {s['misses']} | {s['bits']:.2f} | "
                 + " | ".join(f"{s['by'][z]['median_s']:.2f}秒・{s['by'][z]['misses']}" for z in sizes) + " |")

    L += ["", "## まとめ（中央値と、最小〜最大）", "",
          "| 操作 | 回数 | 合計（秒） | ミス | bit/s | " + " | ".join(f"{s}px のミス" for s in sizes) + " | 外れた距離（円の縁から、中央値） |",
          "|---|---|---|---|---|" + "---|" * len(sizes) + "---|"]
    for dev in DEVICE_JA:
        rs = [r["sum"] for r in runs if r["res"]["device"] == dev]
        if not rs:
            continue
        miss_px = [p for s in rs for p in s["miss_px"]]
        L.append(f"| {DEVICE_JA[dev]} | {len(rs)} | {spread([s['total_s'] for s in rs])} | {spread([s['misses'] for s in rs], '{:.0f}')} | "
                 f"{spread([s['bits'] for s in rs], '{:.2f}')} | "
                 + " | ".join(spread([s['by'][z]['misses'] for s in rs], '{:.0f}') for z in sizes)
                 + f" | {f(float(np.median(miss_px)) if miss_px else None, '{:.0f}px')} |")
    n_ac = sum(r["res"]["device"] == "aircontroller" for r in runs)
    if n_ac and n_ac < 3:
        L += ["", f"注意: AirController は {n_ac} 回。手順は同じ条件で 3 回（1 回の結果は慣れの影響を受ける）。"]

    L += ["", "## 参考: 前回（STATUS 第5節。同じ条件の比較ではない）", "",
          "| 条件 | 合計（秒） | ミス | うち 24px | bit/s |", "|---|---|---|---|---|"]
    L += [f"| {a} | {b} | {c} | {d} | {e} |" for a, b, c, d, e in PREVIOUS]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n")
    print(f"{out} に書きました（{len(runs)} 回）")


if __name__ == "__main__":
    main(sys.argv[1:])
