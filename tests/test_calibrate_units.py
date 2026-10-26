"""calibrate の測定ユニット（SPEC 13.44）。"""
import argparse
import json
from pathlib import Path

import numpy as np
import pytest

from aircontroller import calibration as C
from aircontroller.gesture import GestureConfig

ROOT = Path(__file__).resolve().parent.parent
CALIB = ROOT / "recordings" / "calib-20261026-020957.jsonl"


def labels(units):
    return [lab for lab, _, _ in C.unit_steps(units) if lab]


def test_rest_is_shared_and_included_alone():
    assert labels(["range", "pinch", "right", "peace"]) == ["range", "rest", "pinch", "rpinch", "peace"]
    for u, lab in (("pinch", "pinch"), ("right", "rpinch"), ("peace", "peace")):
        assert labels([u]) == ["rest", lab]
    assert labels(["range"]) == ["range"]
    assert labels(["zoom", "pinch"])[:2] == ["rest", "pinch"]  # 順番はユニットの決まった順


def test_pinch_only_has_old_steps():
    """--pinch-only は以前と同じ段階と長さ（準備 3、待ち 2.5、普段 9.5、待ち 2.5、摘まむ 7.5、待ち 2.5、中指 6.5、待ち 2.5、ピース 6.5）。"""
    from aircontroller.__main__ import _calib_units

    a = argparse.Namespace(unit=None, pinch_only=True)
    assert [(lab, d) for lab, d, _ in C.unit_steps(_calib_units(a))] == [
        (None, 3.0), (None, 2.5), ("rest", 9.5), (None, 2.5), ("pinch", 7.5), (None, 2.5), ("rpinch", 6.5),
        (None, 2.5), ("peace", 6.5)]


def test_unit_option():
    from aircontroller.__main__ import _calib_units

    def units(s):
        return _calib_units(argparse.Namespace(unit=s, pinch_only=False))
    assert units("peace,pinch") == ["pinch", "peace"]
    assert units("all") == list(C.UNIT_ORDER)
    assert _calib_units(argparse.Namespace(unit=None, pinch_only=False)) == ["range", "pinch", "right", "peace"]
    with pytest.raises(ValueError):
        units("base")


def _fake(monkeypatch, d):
    monkeypatch.setattr(C, "collect_samples", lambda frames, segments, hand="both": d)


def test_overlap_gives_no_candidate(monkeypatch):
    rng = np.random.default_rng(0)
    d = {k: [] for k in ("range", "rest_ti", "rest_tm", "rest_thumb", "pinch", "rpinch", "peace_thumb", "peace_dir")}
    d.update(rest_ti=list(rng.uniform(0.3, 0.8, 240)), pinch=list(rng.uniform(0.05, 0.2, 180)),
             rest_tm=list(rng.uniform(0.2, 0.6, 240)), rpinch=list(rng.uniform(0.1, 0.5, 150)),
             frames_rest=[240], seen_rest=[240], frames_pinch=[180], seen_pinch=[180], frames_rpinch=[150], seen_rpinch=[100])
    _fake(monkeypatch, d)
    r = C.compute_units(["pinch", "right"], [], [], GestureConfig(), {}, (1280, 720), (1440, 900))
    assert r["pinch"].params is not None and r["pinch"].stats["separated"]
    assert set(r["pinch"].stats) >= {"rest_p2", "rest_p10", "rest_median", "op_p50", "op_p90", "rest_n", "op_n"}
    assert r["right"].params is None and not r["right"].stats["separated"]
    assert any("重なっている" in w for w in r["right"].warnings)
    assert any("67%" in w for w in r["right"].warnings)  # 手が見えたフレームの割合


def test_too_little_data(monkeypatch):
    d = {k: [] for k in ("range", "rest_ti", "rest_tm", "rest_thumb", "pinch", "rpinch", "peace_thumb", "peace_dir")}
    d.update(rest_ti=[0.5] * 5, pinch=[0.1] * 5, range=[(0.5, 0.5)] * 5)
    _fake(monkeypatch, d)
    r = C.compute_units(["range", "pinch"], [], [], GestureConfig(), {}, (1280, 720), (1440, 900))
    assert r["range"].params is None and r["pinch"].params is None
    assert any("サンプル不足" in w for w in r["pinch"].warnings)
    assert json.dumps(r["pinch"].to_json(), ensure_ascii=False)  # 履歴に書ける


@pytest.mark.skipif(not CALIB.exists(), reason="recordings/ の校正の記録が無い")
def test_recompute_old_recording(tmp_path, monkeypatch):
    """以前の calibrate の記録を、段階を時刻で復元して計算し直すと、そのとき書いた値になる（SPEC 13.37、git の履歴）。"""
    import aircontroller.__main__ as m

    monkeypatch.setattr(m, "CALIB_HISTORY", tmp_path)
    assert m.main(["calibrate", "--from", str(CALIB)]) == 0
    res = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert not res["written"]
    pinch = res["results"]["pinch"]["stats"]
    assert (round(pinch["op_p90"], 3), round(pinch["rest_p2"], 3)) == (0.212, 0.369)
    assert res["results"]["right"]["candidate"]["click"] == {"right_on": 0.2465, "right_off": 0.275, "right_arm": 0.355}
    peace = res["results"]["peace"]["candidate"]["peace"]
    assert (peace["thumb_fold_on"], peace["thumb_fold_off"]) == (0.3405, 0.2564)
