"""パラメータ台帳（aircontroller/params.py）と config.toml が食い違わないこと。"""
import tomllib
from pathlib import Path

from aircontroller.params import PARAMS

CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


def _config() -> dict:
    with CONFIG.open("rb") as fp:
        return tomllib.load(fp)


def test_every_config_key_is_in_ledger():
    keys = {f"{s}.{k}" for s, v in _config().items() for k in v}
    ids = [p.id for p in PARAMS]
    assert len(ids) == len(set(ids)), "台帳に同じ項目が 2 回ある"
    assert sorted(keys - set(ids)) == [], "config.toml にあって台帳にない"
    assert sorted(set(ids) - keys) == [], "台帳にあって config.toml にない"


def test_class_and_writers():
    for p in PARAMS:
        assert p.cls in "ABCD" and len(p.cls) == 1, p.id
        assert set(p.writers) <= {"calibrate", "learn-thumbsup", "learn-zoom", "run-auto"}, p.id
        if p.cls == "B":
            assert p.writers, f"{p.id}: B は測定が書く項目"


def test_range_and_step_contain_default():
    cfg = _config()
    for p in PARAMS:
        v = cfg[p.section][p.key]
        if p.lo is None:
            assert p.hi is None and p.step is None, p.id
            continue
        assert not isinstance(v, bool) and isinstance(v, (int, float)), f"{p.id}: 数値でない項目に範囲がある"
        assert p.lo < p.hi and p.step > 0, p.id
        assert p.lo <= v <= p.hi, f"{p.id}: 既定値 {v} が範囲 {p.lo}〜{p.hi} の外"
        q = (v - p.lo) / p.step
        assert abs(q - round(q)) < 1e-6, f"{p.id}: 既定値 {v} が刻み {p.step} の格子にない"
