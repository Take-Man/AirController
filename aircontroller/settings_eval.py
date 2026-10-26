"""設定の効果の見積もり（設定画面の第4節、SPEC 13.45）。

基準記録（eval-manifest.toml）を 2 つの設定（保存済み = base、候補 = candidate）でジェスチャー層に流し、差を出す。
  - ラベル付きの記録: ラベルの区間ごとに、狙った形（カーソル・ピンチ・ピース・いいね・ズーム・グー）になったか（成立 / 見逃し）、
    その形のフレームの割合、狙っていない形に入った回数（誤成立: ピンチ・右ピンチ・ピース・いいね・ズーム）。
    ラベル付きの記録は有効化していない（hold で記録していない）ので、全体を有効化中として流す（tools/position_report.py と同じ）。
  - ラベルのない記録: 記録の有効化の状態のまま流し、イベントログの集計（report）と同じ定義で、クリック・右クリック・ドラッグ・
    意図しないドラッグの候補（短い・ほぼ動かない）・取り消されたモード・戻る・進む・いいね・ズーム・スクロールの量を数え、
    変わったイベント（move・scroll 以外）の時刻を並べる。
¥ の印（mark-*）は評価の根拠にしない（作者の方針）ので、基準記録に入れない。
"""
from __future__ import annotations

import dataclasses
import json
import tomllib
from collections import Counter
from pathlib import Path

GESTURES = ("pinch", "right", "peace", "thumbsup", "zoom", "fist")
GESTURE_JA = {"cursor": "カーソル", "pinch": "ピンチ", "right": "右ピンチ", "peace": "ピース", "thumbsup": "いいね",
              "zoom": "ズーム", "fist": "グー", "none": "どれでもない"}
FALSE_KINDS = ("pinch", "right", "peace", "thumbsup", "zoom")  # 誤成立を数える形（計画の「ピンチ、ピース、いいね、ズーム」+ 右）


@dataclasses.dataclass
class Entry:
    path: Path
    kind: str  # "unlabeled" / "lab" / "labpos" / "labpos-edge" / "learn-thumbsup" / "learn-zoom"
    note: str = ""
    frames: int | None = None  # 記録のフレーム数（別の記録に置き換わっていないかの確認）


def load_manifest(path: Path, root: Path) -> list[Entry]:
    with Path(path).open("rb") as fp:
        m = tomllib.load(fp)
    return [Entry(root / r["path"], r["kind"], r.get("note", ""), r.get("frames")) for r in m["recording"]]


# ---------------------------------------------------------------- ラベル → 狙った形
def target_of(kind: str, label: str) -> str:
    part = label.split("/")
    if kind == "lab":
        head = part[0]
        return {"cursor": "cursor", "pinch": "pinch", "peace": "peace", "fist": "fist",
                "thumbsup": "thumbsup"}.get(head, "none")  # relaxed, palm/*, backtwo/* は、どの形でもない
    if kind == "labpos":
        return {"cursor": "cursor", "click": "pinch", "peace": "peace"}[part[-1]]
    if kind == "labpos-edge":
        return "peace" if part[-1] == "peace_move" else "cursor"  # hold / move はカーソル
    if kind == "learn-thumbsup":
        return {"neg_cursor": "cursor", "neg_fist": "fist", "neg_fist_side": "fist"}.get(label, "thumbsup")
    if kind == "learn-zoom":
        return "zoom"
    raise ValueError(f"知らない記録の種類: {kind}")


def _mode_group(mode: str) -> str:
    return "pinch" if mode == "drag" else mode


def run_frames(gcfg, frames, force_active: bool):
    """記録を流し、(イベントの列, フレームごとの (時刻, モード)) を返す。"""
    from .gesture import GestureEngine

    engine = GestureEngine(gcfg)
    engine.reset()
    events, modes = [], []
    for f in frames:
        if force_active and not f.active:
            f = dataclasses.replace(f, active=True)
        ev = engine.update(f)
        events.append((f, ev, engine.state.mode, engine.raw_hand(f)))
        modes.append((f.t_capture_ns, engine.state.mode))
    return events, modes


def eval_labeled(kind: str, segments: list[dict], modes: list[tuple[int, str]]) -> dict:
    """区間ごとに、狙った形になったか・その形のフレームの割合・狙っていない形に入った回数。"""
    segs = []
    for s in segments:
        tgt = target_of(kind, s["label"])
        ms = [_mode_group(m) for t, m in modes if s["t_start_ns"] <= t < s["t_end_ns"]]
        segs.append({"label": s["label"], "target": tgt, "frames": len(ms),
                     "share": round(sum(m == tgt for m in ms) / len(ms), 3) if ms else 0.0,
                     "reached": tgt in ms if tgt != "none" else None, "false": Counter()})
    prev = "idle"
    for t, m in modes:
        g = _mode_group(m)
        if g != prev and g in FALSE_KINDS:
            seg = next((x for x, s in zip(segs, segments) if s["t_start_ns"] <= t < s["t_end_ns"]), None)
            if seg is not None and g != seg["target"]:
                seg["false"][g] += 1
        prev = g
    summary: dict = {}
    for tgt in ("cursor",) + GESTURES:
        mine = [x for x in segs if x["target"] == tgt]
        if mine:
            summary[tgt] = {"segments": len(mine), "reached": sum(bool(x["reached"]) for x in mine),
                            "share": round(sum(x["share"] for x in mine) / len(mine), 3)}
    false = Counter()
    for x in segs:
        false.update(x["false"])
    for x in segs:
        x["false"] = dict(x["false"])
    return {"segments": segs, "summary": summary, "false": {g: false.get(g, 0) for g in FALSE_KINDS}}


def eval_unlabeled(frames_events, header: dict) -> dict:
    """イベントログの集計（report）と同じ定義で数え、変わった箇所の比較用に move・scroll 以外のイベントを残す。"""
    from .eventlog import SessionLogger
    from .report import summarize

    rows: list[dict] = []
    slog = SessionLogger(rows.append, lambda t: str(t))
    prev_active, last, t0 = False, 0, None
    discrete = []
    for f, ev, mode, hand in frames_events:
        if t0 is None:
            t0 = f.t_capture_ns
        if f.active != prev_active:
            slog.record(f.t_capture_ns, "activation", on=f.active, reason="recorded")
            prev_active = f.active
        slog.observe(f, ev, mode, hand)
        last = f.t_capture_ns
        for e in ev:
            if e.kind not in ("move", "scroll"):
                sub = e.data.get("dir") or e.data.get("button") or e.data.get("reason") or ""
                discrete.append((round((e.t_ns - t0) / 1e9, 2), f"{e.kind}:{sub}" if sub else e.kind))
    slog.close(last)
    for r in rows:
        r["session"] = 0
    s = summarize(rows)
    scroll = sum(abs(e.data.get("dy", 0)) for _, ev, _, _ in frames_events for e in ev if e.kind == "scroll")
    canceled = s.get("mode_canceled", Counter())
    counts = s.get("counts", {})
    return {"metrics": {"click": s.get("clicks_left", 0), "right_click": s.get("clicks_right", 0),
                        "drag": s.get("drags", 0), "drag_suspect": len(s.get("drag_candidates", [])),
                        "canceled": sum(canceled.values()), "nav": counts.get("nav", 0),
                        "thumbsup": counts.get("thumbsup", 0), "zoom": counts.get("zoom", 0),
                        "scroll_px": int(scroll)},
            "canceled_by_mode": dict(canceled), "events": discrete}


METRIC_JA = {"click": "クリック", "right_click": "右クリック", "drag": "ドラッグ", "drag_suspect": "意図しないドラッグの候補",
             "canceled": "取り消されたモード", "nav": "戻る・進む", "thumbsup": "いいね", "zoom": "ズーム（段）",
             "scroll_px": "スクロール量（px）"}


def _diff_events(a: list, b: list, tol: float = 0.3) -> tuple[list, list]:
    """消えたイベントと増えたイベント（同じ種類で tol 秒以内の時刻のずれは、同じイベントとみなす。閾値を変えると、
    クリックは離した時刻が少しずれる）。"""
    rest = list(b)
    gone = []
    for t, k in a:
        j = next((i for i, (u, kk) in enumerate(rest) if kk == k and abs(u - t) <= tol), None)
        if j is None:
            gone.append((t, k))
        else:
            rest.pop(j)
    return gone, rest


# ---------------------------------------------------------------- 本体
def evaluate(entries: list[Entry], base_cfg: dict, cand_cfg: dict, screen=(1440.0, 900.0), log=print) -> dict:
    from .gesture import GestureConfig
    from .recording import iter_frames, read_header

    gb, gc = GestureConfig.from_toml(base_cfg, screen), GestureConfig.from_toml(cand_cfg, screen)
    out = {"labeled": [], "unlabeled": [], "skipped": []}
    for e in entries:
        if not e.path.exists():
            out["skipped"].append({"path": str(e.path), "why": "記録がない"})
            continue
        frames = list(iter_frames(e.path))
        if e.frames is not None and len(frames) != e.frames:
            out["skipped"].append({"path": str(e.path), "why": f"フレーム数が一覧と違う（{len(frames)} ≠ {e.frames}）"})
            continue
        log(f"  {e.path.name}（{len(frames)} フレーム）")
        if e.kind == "unlabeled":
            header = read_header(e.path)
            rb = eval_unlabeled(run_frames(gb, frames, False)[0], header)
            rc = eval_unlabeled(run_frames(gc, frames, False)[0], header)
            gone, added = _diff_events(rb["events"], rc["events"])
            out["unlabeled"].append({"path": e.path.name, "note": e.note, "base": rb["metrics"], "candidate": rc["metrics"],
                                     "gone": gone, "added": added})
        else:
            segments = json.loads(e.path.with_suffix(".labels.json").read_text())["segments"]
            lb = eval_labeled(e.kind, segments, run_frames(gb, frames, True)[1])
            lc = eval_labeled(e.kind, segments, run_frames(gc, frames, True)[1])
            changed = [{"label": a["label"], "target": a["target"], "base": {k: a[k] for k in ("reached", "share", "false")},
                        "candidate": {k: b[k] for k in ("reached", "share", "false")}}
                       for a, b in zip(lb["segments"], lc["segments"])
                       if (a["reached"], a["false"]) != (b["reached"], b["false"]) or abs(a["share"] - b["share"]) >= 0.05]
            out["labeled"].append({"path": e.path.name, "kind": e.kind, "note": e.note,
                                   "base": {"summary": lb["summary"], "false": lb["false"]},
                                   "candidate": {"summary": lc["summary"], "false": lc["false"]}, "changed": changed})
    tot = {"base": Counter(), "candidate": Counter()}
    for r in out["unlabeled"]:
        tot["base"].update(r["base"])
        tot["candidate"].update(r["candidate"])
    out["unlabeled_total"] = {k: dict(v) for k, v in tot.items()}
    ltot = {}
    for side in ("base", "candidate"):
        reached, segs, false = Counter(), Counter(), Counter()
        for r in out["labeled"]:
            for g, v in r[side]["summary"].items():
                reached[g] += v["reached"]
                segs[g] += v["segments"]
            false.update(r[side]["false"])
        ltot[side] = {"reached": dict(reached), "segments": dict(segs), "false": dict(false)}
    out["labeled_total"] = ltot
    return out


def flat(cfg: dict) -> dict[str, object]:
    return {f"{s}.{k}": v for s, vals in cfg.items() if isinstance(vals, dict) for k, v in vals.items()}


def changed_params(base_cfg: dict, cand_cfg: dict, threshold: float = 0.15) -> list[dict]:
    """候補で値が変わった項目と、変化量の警告（保存済みから threshold 以上動いた）。0 からの変化は、台帳の範囲の幅に対する割合で見る。"""
    from .params import BY_ID

    fb, fc = flat(base_cfg), flat(cand_cfg)
    out = []
    for k in sorted(set(fb) | set(fc)):
        a, b = fb.get(k), fc.get(k)
        if a == b:
            continue
        row = {"key": k, "base": a, "candidate": b, "warn": False, "rel": None}
        p = BY_ID.get(k)
        if p is not None:
            row["name"], row["cls"] = p.name, p.cls
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (a, b)):
            if a:
                row["rel"] = (b - a) / abs(a)
            elif p is not None and p.lo is not None:
                row["rel"] = (b - a) / (p.hi - p.lo)
            row["warn"] = row["rel"] is not None and abs(row["rel"]) >= threshold - 1e-9
        out.append(row)
    return out


def format_result(res: dict, params: list[dict], threshold: float) -> str:
    L = ["[変わる項目]（保存済み → 候補）"]
    for p in params:
        rel = f"  {p['rel'] * 100:+.0f}%" if p.get("rel") is not None else ""
        warn = f"  ⚠ {threshold * 100:.0f}% 以上" if p["warn"] else ""
        L.append(f"  {p['key']}（{p.get('name', '?')}）: {p['base']} → {p['candidate']}{rel}{warn}")
    n_warn = sum(p["warn"] for p in params)
    if n_warn:
        L.append(f"  変化量の警告: {n_warn} 項目")
    L += ["", "[ラベル付きの記録]（成立した区間 / 区間、狙った形のフレームの割合。保存済み → 候補）"]
    for r in res["labeled"]:
        L.append(f"  {r['path']}（{r['kind']}）")
        for g, b in r["base"]["summary"].items():
            c = r["candidate"]["summary"][g]
            same = (b["reached"], round(b["share"] * 100)) == (c["reached"], round(c["share"] * 100))
            mark = "" if same else "  ← 変化"
            L.append(f"    {GESTURE_JA[g]}: {b['reached']}/{b['segments']} ({b['share'] * 100:.0f}%) → "
                     f"{c['reached']}/{c['segments']} ({c['share'] * 100:.0f}%){mark}")
        fb, fc = r["base"]["false"], r["candidate"]["false"]
        L.append("    誤成立: " + "  ".join(f"{GESTURE_JA[g]} {fb[g]}→{fc[g]}" for g in FALSE_KINDS if fb[g] or fc[g])
                 if any(fb.values()) or any(fc.values()) else "    誤成立: なし")
        for ch in r["changed"]:
            L.append(f"      {ch['label']}: 成立 {ch['base']['reached']}→{ch['candidate']['reached']}  "
                     f"割合 {ch['base']['share'] * 100:.0f}→{ch['candidate']['share'] * 100:.0f}%  "
                     f"誤成立 {ch['base']['false'] or '-'}→{ch['candidate']['false'] or '-'}")
    lt = res["labeled_total"]
    if res["labeled"]:
        L.append("  合計: " + "  ".join(
            f"{GESTURE_JA[g]} {lt['base']['reached'].get(g, 0)}→{lt['candidate']['reached'].get(g, 0)}/{n}"
            for g, n in lt["base"]["segments"].items()))
        L.append("  誤成立の合計: " + "  ".join(f"{GESTURE_JA[g]} {lt['base']['false'].get(g, 0)}→"
                                          f"{lt['candidate']['false'].get(g, 0)}" for g in FALSE_KINDS))
    L += ["", "[ラベルのない記録]（保存済み → 候補）"]
    for r in res["unlabeled"]:
        diff = [f"{METRIC_JA[k]} {r['base'][k]}→{r['candidate'][k]}" for k in r["base"] if r["base"][k] != r["candidate"][k]]
        L.append(f"  {r['path']}: " + ("、".join(diff) if diff else "数は同じ")
                 + (f"  消えた {len(r['gone'])}・増えた {len(r['added'])}" if r["gone"] or r["added"] else ""))
        for t, k in r["gone"][:10]:
            L.append(f"      {t:7.2f}s  - {k}")
        for t, k in r["added"][:10]:
            L.append(f"      {t:7.2f}s  + {k}")
        if len(r["gone"]) > 10 or len(r["added"]) > 10:
            L.append("      …（全部は --json の出力に）")
    ut = res["unlabeled_total"]
    if res["unlabeled"]:
        L.append("  合計: " + "  ".join(f"{METRIC_JA[k]} {ut['base'].get(k, 0)}→{ut['candidate'].get(k, 0)}"
                                       for k in METRIC_JA))
    for s in res["skipped"]:
        L.append(f"  使わなかった記録: {s['path']}（{s['why']}）")
    return "\n".join(L)


def read_candidate(path: Path) -> dict:
    """候補のファイルを {節: {キー: 値}} にする。calibration-history/*.json（ユニットごとの候補を重ねる）、
    {節: {キー: 値}} の JSON / TOML。スナップショット（calibration と auto の表を持つ）は呼び出し側で扱う。"""
    p = Path(path)
    if p.suffix == ".json":
        d = json.loads(p.read_text())
        if "results" in d:
            out: dict = {}
            for r in d["results"].values():
                for sec, vals in (r.get("candidate") or {}).items():
                    out.setdefault(sec, {}).update(vals)
            return out
        return d
    with p.open("rb") as fp:
        return tomllib.load(fp)


def overlay(cfg: dict, updates: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in cfg.items()}
    for sec, vals in updates.items():
        out.setdefault(sec, {}).update(vals)
    return out


def parse_set(items: list[str]) -> dict:
    """--set section.key=value（値は JSON。数値・true/false・"文字列"）。"""
    out: dict = {}
    for it in items or []:
        k, v = it.split("=", 1)
        sec, key = k.split(".", 1)
        try:
            val = json.loads(v)
        except ValueError:
            val = v
        out.setdefault(sec, {})[key] = val
    return out

