import numpy as np
import pytest

from aircontroller.calibration import derive_pinch_thresholds


def test_separated_distributions():
    rng = np.random.default_rng(0)
    pinch = list(rng.uniform(0.03, 0.10, 100))
    rest = list(rng.uniform(0.30, 0.80, 200))
    th = derive_pinch_thresholds(rest, pinch, 0.15, 0.22, 0.26)
    assert th.separated
    assert th.pinch_hi < th.on < th.off < th.drag_off <= th.rest_lo


def test_overlap_falls_back_to_defaults():
    rng = np.random.default_rng(1)
    th = derive_pinch_thresholds(list(rng.uniform(0.05, 0.4, 100)), list(rng.uniform(0.05, 0.4, 100)), 0.15, 0.22)
    assert not th.separated and (th.on, th.off) == (0.15, 0.22)


def test_too_little_data():
    with pytest.raises(ValueError):
        derive_pinch_thresholds([0.5] * 3, [0.1] * 50, 0.15, 0.22)


def test_learn_thumbsup_synthetic():
    """合成のいいねで上下左右を記録したものから学習し、各方向が判定できること。"""
    from aircontroller.calibration import learn_thumbsup
    from aircontroller.types import LandmarkFrame
    from tests.test_stage4 import make_fist, make_thumbsup

    rng = np.random.default_rng(0)
    frames, segs, t = [], [], 0
    plan = [("neg_fist", None), ("center", (0.5, 0.5)), ("up", (0.5, 0.25)), ("center", (0.5, 0.5)),
            ("left", (0.72, 0.5)), ("center", (0.5, 0.5)),
            ("right", (0.28, 0.5)), ("center", (0.5, 0.5))]
    for label, palm in plan:
        start = t
        for _ in range(30):
            jitter = rng.normal(0, 0.004, 2)
            hand = make_fist() if palm is None else make_thumbsup(palm=(palm[0] + jitter[0], palm[1] + jitter[1]))
            frames.append(LandmarkFrame(len(frames), t, t, (1280, 720), (hand,)))
            t += 33_000_000
        segs.append({"label": label, "t_start_ns": start, "t_end_ns": t})
    r = learn_thumbsup(frames, segs)
    p = r.params
    for d in ("up", "left", "right"):
        assert p["thumbsup_dead_disp"] < p[f"thumbsup_fire_{d}"]
    assert "100% /   0%" in r.report.split("up    :")[1].splitlines()[0]


def test_learn_zoom_synthetic():
    from aircontroller.calibration import learn_zoom
    from aircontroller.types import LandmarkFrame
    from tests.test_stage6 import make_back

    rng = np.random.default_rng(0)
    frames, segs, t = [], [], 0
    for label, tilt in [("neutral", 0), ("in", 35), ("neutral", 0), ("out", -35), ("neutral", 0), ("in", 30),
                        ("neutral", 0), ("out", -30)]:
        start = t
        for _ in range(30):
            frames.append(LandmarkFrame(len(frames), t, t, (1280, 720), (make_back(tilt + rng.normal(0, 1.5)),)))
            t += 33_000_000
        segs.append({"label": label, "t_start_ns": start, "t_end_ns": t})
    r = learn_zoom(frames, segs)
    assert r.params["zoom_signal"] == "depth" and r.params["zoom_sign"] == 1.0
    assert 0 < r.params["zoom_dead"] < r.params["zoom_max"]


def test_save_calibration_writes_valid_toml(tmp_path, monkeypatch):
    import tomllib

    import aircontroller.__main__ as m
    monkeypatch.setattr(m, "CALIBRATION", tmp_path / "c.toml")
    m.save_calibration({"zoom": {"zoom_signal": "depth", "zoom_sign": -1.0, "flag": True}}, "test")
    assert tomllib.loads((tmp_path / "c.toml").read_text())["zoom"] == {"zoom_signal": "depth", "zoom_sign": -1.0,
                                                                         "flag": True}
