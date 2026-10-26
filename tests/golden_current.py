"""今の設定でのゴールデンテスト（SPEC 13.50）の共通部分。

tests/fixtures/ と recordings/ の記録を、今の設定（config.toml + calibration.toml。auto.toml は run のたびに変わるので含めない）で
ジェスチャー層に流し、tests/golden-current/<fixtures|recordings>/<名前>.json の要約と比べる。
要約: 使った設定、全イベント列のハッシュ、種類別の件数、move・scroll 以外の全イベント、move の件数と scroll の件数・dy の合計。
比べる・作り直す: .venv/bin/python tools/golden_diff.py --current [--write]
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from collections import Counter
from pathlib import Path

from aircontroller.__main__ import settings_store
from aircontroller.gesture import GestureConfig, GestureEngine, run
from aircontroller.recording import iter_frames
from tests.test_gesture_replay import events_to_json

ROOT = Path(__file__).resolve().parent.parent
DIR = Path(__file__).parent / "golden-current"
GROUPS = {"fixtures": ROOT / "tests/fixtures", "recordings": ROOT / "recordings"}
CONTINUOUS = ("move", "scroll")  # 数が多いので、要約には件数と合計だけ持つ


def current_config() -> GestureConfig:
    """config.toml に calibration.toml を重ねた設定（auto.toml は含めない）。"""
    store = settings_store()
    return GestureConfig.from_toml(store.load_state({"calibration": store.layer("calibration")}))


def config_json(cfg: GestureConfig) -> dict:
    return json.loads(json.dumps(dataclasses.asdict(cfg)))  # タプルをリストにして、保存したものと比べられる形にする


def key(e: dict) -> str:
    """イベントの種類（向き・ボタン・理由があれば付ける）。tools/golden_diff.py と同じ。"""
    d = e["data"]
    sub = d.get("dir") or d.get("button") or d.get("reason") or ""
    return f"{e['kind']}:{sub}" if sub else e["kind"]


def summarize(events: list[dict]) -> dict:
    body = json.dumps(events, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
        "counts": dict(sorted(Counter(key(e) for e in events).items())),
        "move": sum(e["kind"] == "move" for e in events),
        "scroll": {"count": sum(e["kind"] == "scroll" for e in events),
                   "dy": sum(e["data"].get("dy", 0) for e in events if e["kind"] == "scroll")},
        "events": [e for e in events if e["kind"] not in CONTINUOUS],
    }


def compute(rec: Path, cfg: GestureConfig) -> dict:
    events = events_to_json(run(GestureEngine(cfg), iter_frames(rec)))
    return {"recording": rec.resolve().relative_to(ROOT).as_posix(), "config": config_json(cfg), **summarize(events)}


def expected_path(rec: Path) -> Path:
    rec = rec.resolve()
    group = next((g for g, d in GROUPS.items() if rec.parent == d), None)
    if group is None:
        raise ValueError(f"tests/fixtures/ か recordings/ の記録ではありません: {rec}")
    return DIR / group / (rec.stem + ".json")


def expected_files() -> list[Path]:
    return sorted(DIR.glob("*/*.json"))


def all_recordings() -> list[Path]:
    return [p for d in GROUPS.values() for p in sorted(d.glob("*.jsonl"))]


def write(path: Path, summary: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n")


def describe(old: dict, new: dict) -> list[str]:
    """2 つの要約の差（種類別の件数、move・scroll、move・scroll 以外で消えた・増えたイベント）。"""
    if old["sha256"] == new["sha256"]:
        return []
    keys = sorted(set(old["counts"]) | set(new["counts"]))
    lines = ["、".join(f"{k} {old['counts'].get(k, 0)}→{new['counts'].get(k, 0)}" for k in keys
                      if old["counts"].get(k, 0) != new["counts"].get(k, 0)) or "種類ごとの数は同じ"]
    if old["scroll"]["dy"] != new["scroll"]["dy"]:
        lines.append(f"scroll 合計 {old['scroll']['dy']}→{new['scroll']['dy']}px")
    so = {(e["t_ns"], key(e)) for e in old["events"]}
    sn = {(e["t_ns"], key(e)) for e in new["events"]}
    if so != sn:
        lines.append(f"move・scroll 以外: 消えた {len(so - sn)}、増えた {len(sn - so)}")
    elif old["events"] != new["events"]:
        lines.append("move・scroll 以外: 時刻と種類は同じで、中身が変わった")
    else:
        lines.append("move・scroll 以外は同じ（move の座標か scroll の量が変わった）")
    return lines
