"""記録ファイルから、再現テストの期待値（*.events.json）を作る。

  python -m tests.make_golden tests/fixtures/xxx.jsonl

出力は目で確認してから保存すること（期待値は「正しい挙動」の記述であり、現状の写しではない）。
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

from aircontroller.__main__ import load_config
from aircontroller.gesture import GestureConfig, GestureEngine, run
from aircontroller.recording import iter_frames
from tests.test_gesture_replay import events_to_json


def main(path: str) -> None:
    p = Path(path)
    cfg = GestureConfig.from_toml(load_config(None))
    events = events_to_json(run(GestureEngine(cfg), iter_frames(p)))
    out = {"config": dataclasses.asdict(cfg), "events": events}
    p.with_suffix(".events.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    t0 = next(iter_frames(p)).t_capture_ns
    for e in events:
        print(f"{(e['t_ns'] - t0) / 1e6:9.1f}ms  {e['kind']}")


if __name__ == "__main__":
    main(sys.argv[1])
