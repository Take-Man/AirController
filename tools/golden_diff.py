"""再現テストの期待値（tests/fixtures/*.events.json）と、今の設定・コードでの出力の差を、イベントの種類ごとにまとめる。
期待値を作り直す前に、差が意図した変更だけかを確かめるために使う。

使い方: .venv/bin/python tools/golden_diff.py [tests/fixtures/xxx.jsonl ...] [--golden-config]
  既定は今の config.toml + calibration.toml で流す（make_golden と同じ）。--golden-config は期待値に保存された設定で流す
  （コードの変更だけの影響を見る）。--set key=value（JSON の値）で、その設定だけ上書きする（変えた設定の影響を見る）。
  --write で、その設定で期待値を書き直す（差を確かめてから使う）。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aircontroller.__main__ import load_config  # noqa: E402
from aircontroller.gesture import GestureConfig, GestureEngine, run  # noqa: E402
from aircontroller.recording import iter_frames  # noqa: E402


def key(e: dict) -> str:
    d = e["data"]
    sub = d.get("dir") or d.get("button") or d.get("reason") or ""
    return f"{e['kind']}:{sub}" if sub else e["kind"]


def main(argv: list[str]) -> None:
    use_golden = "--golden-config" in argv
    write = "--write" in argv
    sets = {}
    rest = []
    it = iter(argv)
    for a in it:
        if a == "--set":
            k, v = next(it).split("=", 1)
            sets[k] = json.loads(v)
        elif not a.startswith("--"):
            rest.append(a)
    paths = [Path(a) for a in rest] or sorted((ROOT / "tests/fixtures").glob("*.jsonl"))
    known = GestureConfig.__dataclass_fields__
    for p in paths:
        g = json.loads(p.with_suffix(".events.json").read_text())
        cfg = GestureConfig(**{k: v for k, v in g["config"].items() if k in known}) if use_golden \
            else GestureConfig.from_toml(load_config(None))
        for k, v in sets.items():
            setattr(cfg, k, v)
        new = [{"kind": e.kind, "t_ns": e.t_ns, "data": e.data} for e in run(GestureEngine(cfg), iter_frames(p))]
        old = g["events"]
        if write:
            import dataclasses
            p.with_suffix(".events.json").write_text(json.dumps({"config": dataclasses.asdict(cfg), "events": new},
                                                                indent=1, ensure_ascii=False) + "\n")
        if new == old:
            print(f"{p.name}: 差なし（{len(old)} イベント）")
            continue
        co = Counter(key(e) for e in old if e["kind"] not in ("move", "scroll"))
        cn = Counter(key(e) for e in new if e["kind"] not in ("move", "scroll"))
        moves = (sum(e["kind"] == "move" for e in old), sum(e["kind"] == "move" for e in new))
        scr = (sum(e["data"].get("dy", 0) for e in old if e["kind"] == "scroll"),
               sum(e["data"].get("dy", 0) for e in new if e["kind"] == "scroll"))
        diff = {k: (co.get(k, 0), cn.get(k, 0)) for k in sorted(set(co) | set(cn)) if co.get(k, 0) != cn.get(k, 0)}
        print(f"{p.name}: " + ("、".join(f"{k} {a}→{b}" for k, a, b in ((k, *v) for k, v in diff.items()))
                               or "種類ごとの数は同じ")
              + (f"  move {moves[0]}→{moves[1]}" if moves[0] != moves[1] else "")
              + (f"  scroll 合計 {scr[0]}→{scr[1]}px" if scr[0] != scr[1] else ""))
        # move・scroll 以外で、時刻か中身が変わったイベントの数
        so = {(e["t_ns"], key(e)) for e in old if e["kind"] not in ("move", "scroll")}
        sn = {(e["t_ns"], key(e)) for e in new if e["kind"] not in ("move", "scroll")}
        print(f"    move・scroll 以外: 消えた {len(so - sn)}、増えた {len(sn - so)}")


if __name__ == "__main__":
    main(sys.argv[1:])
