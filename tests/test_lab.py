"""lab-report が、ラベル付きの記録から集計できることの確認（合成データ）。"""
import json

from aircontroller.__main__ import main
from aircontroller.recording import Recorder
from aircontroller.types import LandmarkFrame
from tests.test_stage1 import make_hand
from tests.test_stage4 import make_fist, make_peace, make_thumbsup

MS = 1_000_000


def test_lab_report_runs(tmp_path, capsys):
    path = tmp_path / "lab.jsonl"
    rec = Recorder(path)
    segs, t, i = [], 0, 0
    plan = [("cursor", make_hand()), ("peace", make_peace()), ("fist", make_fist()),
            ("thumbsup/center", make_thumbsup()), ("thumbsup/up", make_thumbsup(palm=(0.5, 0.4)))]
    for label, hand in plan:
        start = t
        for _ in range(20):
            rec.write(LandmarkFrame(i, t, t, (1280, 720), (hand,)))
            t += 33 * MS
            i += 1
        segs.append({"label": label, "t_start_ns": start, "t_end_ns": t})
    rec.close()
    path.with_suffix(".labels.json").write_text(json.dumps({"segments": segs}))
    assert main(["lab-report", str(path)]) == 0
    out = capsys.readouterr().out
    assert "thumbsup" in out and "バランス精度" in out and "up (" in out
