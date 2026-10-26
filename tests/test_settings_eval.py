"""効果の見積もり（aircontroller/settings_eval.py、SPEC 13.45）。"""
import json
from pathlib import Path

import pytest

from aircontroller import settings_eval as E

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"
MS = 1_000_000


def test_manifest_is_valid():
    entries = E.load_manifest(ROOT / "eval-manifest.toml", ROOT)
    assert len({e.path for e in entries}) == len(entries)
    kinds = {"unlabeled", "lab", "labpos", "labpos-edge", "learn-thumbsup", "learn-zoom"}
    for e in entries:
        assert e.kind in kinds and e.frames
        assert "mark-" not in e.path.name  # 印の記録は評価の根拠にしない
        if "tests/fixtures" in str(e.path):
            assert e.path.exists()  # git にある記録は必ずある
        if e.kind != "unlabeled" and e.path.exists():  # ラベルはすべて狙った形に対応づく
            for s in json.loads(e.path.with_suffix(".labels.json").read_text())["segments"]:
                E.target_of(e.kind, s["label"])


def test_labeled_reached_share_and_false():
    segs = [{"label": "center/peace", "t_start_ns": 0, "t_end_ns": 100 * MS},
            {"label": "center/cursor", "t_start_ns": 100 * MS, "t_end_ns": 200 * MS}]
    modes = [(t * MS, m) for t, m in [(0, "cursor"), (20, "peace"), (40, "peace"), (60, "peace"), (80, "peace"),
                                     (100, "cursor"), (120, "pinch"), (140, "drag"), (160, "cursor"), (180, "right")]]
    r = E.eval_labeled("labpos", segs, modes)
    assert r["summary"]["peace"] == {"segments": 1, "reached": 1, "share": 0.8}
    assert r["summary"]["cursor"] == {"segments": 1, "reached": 1, "share": 0.4}
    assert r["false"]["pinch"] == 1 and r["false"]["right"] == 1  # pinch → drag は 1 回の成立


def test_targets():
    assert E.target_of("lab", "thumbsup/left") == "thumbsup"
    assert E.target_of("lab", "palm/up") == "none"
    assert E.target_of("labpos-edge", "downright/move") == "cursor"
    assert E.target_of("labpos-edge", "down/peace_move") == "peace"
    assert E.target_of("learn-thumbsup", "neg_fist_side") == "fist"


def test_same_config_has_no_difference():
    from aircontroller.__main__ import load_config

    cfg = load_config(None)
    res = E.evaluate([E.Entry(FIX / "right01.jsonl", "unlabeled")], cfg, cfg, log=lambda *_: None)
    r = res["unlabeled"][0]
    assert r["base"] == r["candidate"] and not r["gone"] and not r["added"]
    assert r["base"]["right_click"] > 0


def test_changed_config_shows_difference():
    from aircontroller.__main__ import load_config

    base = load_config(None)
    cand = E.overlay(base, E.parse_set(["click.pinch_on=0.10"]))
    res = E.evaluate([E.Entry(FIX / "use01.jsonl", "unlabeled")], base, cand, log=lambda *_: None)
    r = res["unlabeled"][0]
    assert r["candidate"]["click"] < r["base"]["click"] and r["gone"]
    assert res["unlabeled_total"]["base"]["click"] == r["base"]["click"]


def test_change_warning():
    base = {"click": {"pinch_on": 0.2, "pinch_off": 0.25}, "peace": {"peace_neutral_yaw_deg": 0.0}}
    cand = {"click": {"pinch_on": 0.21, "pinch_off": 0.30}, "peace": {"peace_neutral_yaw_deg": 30.0}}
    rows = {r["key"]: r for r in E.changed_params(base, cand, 0.15)}
    assert not rows["click.pinch_on"]["warn"]  # +5%
    assert rows["click.pinch_off"]["warn"]  # +20%
    assert rows["peace.peace_neutral_yaw_deg"]["warn"]  # 0 からの変化は範囲の幅（180 度）の 17%
    assert rows["click.pinch_off"]["name"] == "ピンチの解除"


def test_read_candidate_from_calibration_history(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"results": {"pinch": {"candidate": {"click": {"pinch_on": 0.2}}},
                                         "right": {"candidate": None},
                                         "peace": {"candidate": {"peace": {"thumb_fold_on": 0.3}}}}}))
    assert E.read_candidate(p) == {"click": {"pinch_on": 0.2}, "peace": {"thumb_fold_on": 0.3}}


@pytest.mark.parametrize("args", [["--set", "click.pinch_on=0.2235", "--only", "unlabeled"]])
def test_cli_runs(args, tmp_path, monkeypatch, capsys):
    import aircontroller.__main__ as m

    man = tmp_path / "m.toml"
    man.write_text('[[recording]]\npath = "tests/fixtures/right01.jsonl"\nkind = "unlabeled"\n')
    monkeypatch.setattr(m.os, "nice", lambda n: 0)
    assert m.main(["settings-eval", "--manifest", str(man), "--json", str(tmp_path / "r.json"), *args]) == 0
    assert json.loads((tmp_path / "r.json").read_text())["unlabeled"][0]["path"] == "right01.jsonl"
