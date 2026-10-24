"""イベントログ（logs/events-*.jsonl）の集計（TASKS-20261025 1.3）。

  python -m aircontroller report logs/events-*.jsonl

印の位置、取り消されたモードの数、意図しないドラッグの候補、左右ラベルの反転回数、手の不在による自動オフの回数などを出す。
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .eventlog import LOG_FORMAT

MODE_JA = {"idle": "なし", "cursor": "カーソル", "pinch": "ピンチ（左）", "drag": "ドラッグ", "right": "右クリック待ち", "peace": "ピース",
           "thumbsup": "いいね", "zoom": "ズーム", "fist": "グー"}
REASON_JA = {"key": "キー", "hand_absent": "手の不在", "palm": "パー", "emergency": "緊急停止",
             "tap_timeout": "キー監視の停止", "exit": "終了"}


@dataclass
class ReportConfig:
    short_drag_ms: float = 1000.0  # これより短いドラッグは、意図しないドラッグの候補
    small_drag_pt: float = 30.0  # これより動かなかったドラッグも候補
    double_click_ms: float = 400.0  # 直前のクリックからこれ以内のクリック（連続クリックの候補）
    context_s: float = 5.0  # 印の前のこの秒数のイベントを、印の位置の説明として出す


def load(paths: list[str | Path]) -> list[dict]:
    """ファイルを読み、セッション（同じプロセス）ごとに session 番号を付けた行のリストを返す。"""
    rows: list[dict] = []
    session = -1
    for p in sorted(Path(x) for x in paths):
        with p.open() as fp:
            head = json.loads(fp.readline() or "{}")
            if head.get("format") != LOG_FORMAT:
                raise ValueError(f"{p}: イベントログではありません")
            if head.get("part", 1) == 1:
                session += 1
            for line in fp:
                if line.strip():
                    r = json.loads(line)
                    r["session"] = max(session, 0)
                    r["file"] = p.name
                    rows.append(r)
    return rows


def _fmt_dur(s: float) -> str:
    return f"{int(s // 3600)}時間{int(s % 3600 // 60)}分" if s >= 3600 else f"{int(s // 60)}分{int(s % 60)}秒"


def summarize(rows: list[dict], rc: ReportConfig | None = None) -> dict:
    rc = rc or ReportConfig()
    out: dict = {"lines": len(rows)}
    if not rows:
        return out
    out["start"], out["end"] = rows[0]["wall"], rows[-1]["wall"]
    out["sessions"] = len({r["session"] for r in rows})

    # 有効化中の時間（セッションごとに on → off の区間を足す）
    active_s = 0.0
    on_since: dict[int, int] = {}
    act_on, act_off = Counter(), Counter()
    for r in rows:
        if r["type"] == "activation":
            if r["on"]:
                act_on[r["reason"]] += 1
                on_since[r["session"]] = r["t_ns"]
            else:
                act_off[r["reason"]] += 1
                t0 = on_since.pop(r["session"], None)
                if t0 is not None:
                    active_s += (r["t_ns"] - t0) / 1e9
    for s, t0 in on_since.items():  # 有効のまま終わったセッション
        last = max(r["t_ns"] for r in rows if r["session"] == s)
        active_s += (last - t0) / 1e9
    out["active_s"] = active_s
    out["activation_on"], out["activation_off"] = dict(act_on), dict(act_off)
    out["auto_off_hand_absent"] = act_off.get("hand_absent", 0)

    types = Counter(r["type"] for r in rows)
    out["counts"] = dict(types)
    clicks = [r for r in rows if r["type"] == "click"]
    out["clicks_left"] = sum(1 for r in clicks if r.get("button") == "left")
    out["clicks_right"] = sum(1 for r in clicks if r.get("button") == "right")
    out["double_click_candidates"] = sum(
        1 for a, b in zip(clicks, clicks[1:])
        if a["session"] == b["session"] and a.get("button") == b.get("button") == "left"
        and (b["t_ns"] - a["t_ns"]) / 1e6 <= rc.double_click_ms)

    # 取り消されたモード
    exits = [r for r in rows if r["type"] == "mode_exit" and "canceled" in r]
    out["mode_exits"] = Counter(r["mode"] for r in exits)
    out["mode_canceled"] = Counter(r["mode"] for r in exits if r["canceled"])
    out["pinch_cancel"] = Counter(f"{r.get('button', 'left')}:{r.get('reason')}" for r in rows
                                  if r["type"] == "pinch_cancel")

    # 意図しないドラッグの候補
    cands = []
    for r in rows:
        if r["type"] != "drag_end":
            continue
        why = []
        if r.get("held_ms", 1e9) < rc.short_drag_ms:
            why.append(f"短い（{r['held_ms']:.0f}ms）")
        if r.get("moved_pt", 1e9) < rc.small_drag_pt:
            why.append(f"ほぼ動かない（{r['moved_pt']:.0f}pt）")
        if r.get("reason") in ("hand_lost", "deactivated"):
            why.append({"hand_lost": "手を見失って解除", "deactivated": "無効化で解除"}[r["reason"]])
        if why:
            cands.append({"wall": r["wall"], "why": why})
    out["drags"] = types.get("drag_start", 0)
    out["drag_candidates"] = cands

    # 左右ラベルの反転
    flips = [r for r in rows if r["type"] == "hand_label"]
    out["label_flips"] = len(flips)
    out["label_flips_active"] = sum(1 for r in flips if r.get("active"))
    out["label_flips_by_mode"] = Counter(r.get("mode", "?") for r in flips)
    labels = Counter(r["label"] for r in rows if r["type"] in ("hand_acquired", "mode_enter") and "label" in r)
    out["labels_seen"] = dict(labels)

    # 印
    marks = []
    for r in rows:
        if r["type"] != "mark":
            continue
        ctx = [x for x in rows if x["session"] == r["session"] and 0 <= (r["t_ns"] - x["t_ns"]) / 1e9 <= rc.context_s
               and x["type"] not in ("mark", "activation")]
        desc = []
        for x in ctx:
            dt = (x["t_ns"] - r["t_ns"]) / 1e9
            k = x["type"]
            if k == "mode_enter":
                desc.append(f"{dt:+.1f}s {MODE_JA.get(x['mode'], x['mode'])}に入る")
            elif k == "mode_exit":
                desc.append(f"{dt:+.1f}s {MODE_JA.get(x['mode'], x['mode'])}を抜ける"
                            + ("（取り消し）" if x.get("canceled") else ""))
            elif k in ("click", "nav", "thumbsup", "zoom", "drag_start", "drag_end", "pinch_cancel", "hand_lost",
                       "hand_acquired", "hand_label"):
                extra = x.get("button") or x.get("dir") or x.get("reason") or x.get("label") or ""
                desc.append(f"{dt:+.1f}s {k} {extra}".rstrip())
        marks.append({"n": r["n"], "wall": r["wall"], "mode": r.get("mode"), "active": r.get("active"),
                      "path": r.get("path"), "context": desc})
    out["marks"] = marks
    return out


def format_report(s: dict) -> str:
    if s.get("lines", 0) == 0:
        return "行がありません"
    L = [f"期間: {s['start']} 〜 {s['end']}（{s['sessions']} セッション、{s['lines']} 行）",
         f"有効化中の時間: {_fmt_dur(s['active_s'])}",
         "有効化: オン " + ", ".join(f"{REASON_JA.get(k, k)} {v}" for k, v in s["activation_on"].items())
         + " / オフ " + ", ".join(f"{REASON_JA.get(k, k)} {v}" for k, v in s["activation_off"].items()),
         f"手の不在による自動オフ: {s['auto_off_hand_absent']} 回",
         "",
         f"クリック {s['clicks_left']}  右クリック {s['clicks_right']}  ドラッグ {s['drags']}  "
         f"戻る・進む {s['counts'].get('nav', 0)}  いいね {s['counts'].get('thumbsup', 0)}  "
         f"ズーム {s['counts'].get('zoom', 0)} 段",
         f"連続クリックの候補（{ReportConfig.double_click_ms:.0f}ms 以内の左クリック）: {s['double_click_candidates']}",
         "",
         "[取り消されたモード]（出力なしで抜けた回数 / 抜けた回数）"]
    for m, n in s["mode_exits"].most_common():
        L.append(f"  {MODE_JA.get(m, m)}: {s['mode_canceled'].get(m, 0)} / {n}")
    if s["pinch_cancel"]:
        L.append("  ピンチの取り消しの内訳: " + ", ".join(f"{k} {v}" for k, v in s["pinch_cancel"].most_common()))
    L.append("")
    L.append(f"[意図しないドラッグの候補] {len(s['drag_candidates'])} / ドラッグ {s['drags']}")
    for c in s["drag_candidates"]:
        L.append(f"  {c['wall']}  {'、'.join(c['why'])}")
    L.append("")
    L.append(f"[左右ラベルの反転] {s['label_flips']} 回（有効化中 {s['label_flips_active']} 回）  "
             f"見えたラベル: {s['labels_seen']}")
    if s["label_flips_by_mode"]:
        L.append("  反転したときのモード: " + ", ".join(f"{MODE_JA.get(k, k)} {v}"
                                                for k, v in s["label_flips_by_mode"].most_common()))
    L.append("")
    L.append(f"[印] {len(s['marks'])} 個")
    for m in s["marks"]:
        L.append(f"  印 {m['n']}  {m['wall']}  モード {MODE_JA.get(m['mode'], m['mode'])}  "
                 f"{'有効' if m['active'] else '無効'}  記録 {m['path']}")
        for d in m["context"]:
            L.append(f"      {d}")
    return "\n".join(L)
