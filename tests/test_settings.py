"""設定の層・原子的な保存・履歴（aircontroller/settings.py）。"""
import json
import shutil
import tomllib
from pathlib import Path

import pytest

from aircontroller import settings
from aircontroller.settings import SettingsStore

ROOT = Path(__file__).resolve().parent.parent

CONFIG = """[cursor]
roi_cx = 0.5
roi_width = 0.4

[click]
pinch_on = 0.15
pinch_off = 0.22
"""


@pytest.fixture
def store(tmp_path):
    (tmp_path / "config.toml").write_text(CONFIG)
    return SettingsStore(tmp_path)


def test_layers_order(store):
    assert store.load()["click"]["pinch_on"] == 0.15
    store.save({"click": {"pinch_on": 0.2}, "cursor": {"roi_cx": 0.45}}, "calibrate")
    store.save_auto({"cursor": {"roi_cx": 0.47}})
    cfg = store.load()
    assert cfg["click"] == {"pinch_on": 0.2, "pinch_off": 0.22}
    assert cfg["cursor"]["roi_cx"] == 0.47  # auto.toml が一番上
    assert store.load(calibration=False)["cursor"]["roi_cx"] == 0.5


def test_auto_is_dropped_when_lower_layer_changes(store):
    """自動補正の後に calibrate で測り直したら、測った値を使う（auto.toml の古い値は使わない）。"""
    store.save({"cursor": {"roi_cx": 0.45}}, "calibrate")
    store.save_auto({"cursor": {"roi_cx": 0.47, "roi_width": 0.5}})
    assert tomllib.loads(store.auto.read_text())["base"]["cursor"] == {"roi_cx": 0.45, "roi_width": 0.4}
    store.save({"cursor": {"roi_cx": 0.40}}, "calibrate")
    cfg = store.load()
    assert cfg["cursor"]["roi_cx"] == 0.40  # 測り直した値
    assert cfg["cursor"]["roi_width"] == 0.5  # 下の層が変わっていない項目は、自動補正の値のまま
    # 次の自動補正は、測り直した値を基にして書く（古い値は捨てる）
    store.save_auto({"cursor": {"roi_width": 0.52}})
    auto = tomllib.loads(store.auto.read_text())
    assert auto["cursor"] == {"roi_width": 0.52} and auto["base"]["cursor"] == {"roi_width": 0.4}


def test_legacy_roi_in_calibration_is_read(store):
    """以前 run が calibration.toml に書いた操作範囲は、auto.toml が無くてもそのまま読む。"""
    store.calibration.write_text("[cursor]\nroi_cx = 0.4151\nroi_width = 0.5513\n")
    assert store.load()["cursor"] == {"roi_cx": 0.4151, "roi_width": 0.5513}


def test_real_files_load_like_before(tmp_path):
    """今の config.toml と calibration.toml を、以前の読み方（config.toml を calibration.toml で上書き）と同じに読む。"""
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    if (ROOT / "calibration.toml").exists():
        shutil.copy(ROOT / "calibration.toml", tmp_path / "calibration.toml")
    with (tmp_path / "config.toml").open("rb") as fp:
        old = tomllib.load(fp)
    if (tmp_path / "calibration.toml").exists():
        with (tmp_path / "calibration.toml").open("rb") as fp:
            for s, v in tomllib.load(fp).items():
                old.setdefault(s, {}).update(v)
    assert SettingsStore(tmp_path).load() == old


def test_atomic_write_keeps_old_file_on_failure(store, monkeypatch):
    store.save({"click": {"pinch_on": 0.2}}, "calibrate")
    before = store.calibration.read_text()

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(settings.os, "replace", boom)
    with pytest.raises(OSError):
        store.save({"click": {"pinch_on": 0.3}}, "calibrate")
    assert store.calibration.read_text() == before
    assert not [p for p in store.root.iterdir() if p.name.endswith(".tmp")]


def test_history_and_changes(store):
    store.save({"click": {"pinch_on": 0.2}}, "calibrate")
    store.save({"click": {"pinch_on": 0.2, "pinch_off": 0.25}}, "settings")
    snaps = store.snapshots()
    assert [s["route"] for s in snaps] == ["初期状態（最初の保存の前）", "calibrate", "settings"]
    rows = [json.loads(line) for line in store.changes.read_text().splitlines()]
    assert [(r["key"], r["before"], r["after"], r["route"]) for r in rows] == [
        ("click.pinch_on", None, 0.2, "calibrate"), ("click.pinch_off", None, 0.25, "settings")]
    assert all(r["t"] and r["layer"] == "calibration" for r in rows)


def test_undo_restores_previous_save(store):
    store.save({"click": {"pinch_on": 0.2}}, "calibrate")
    store.save_auto({"cursor": {"roi_cx": 0.47}})
    store.save({"click": {"pinch_on": 0.3}}, "settings")
    changes = store.undo()
    assert store.load()["click"]["pinch_on"] == 0.2
    assert ("click.pinch_on", 0.3, 0.2) in [(c["key"], c["before"], c["after"]) for c in changes]
    assert store.snapshots()[-1]["route"].startswith("前の保存に戻す")


def test_undo_to_initial_state_removes_layer(store):
    store.save({"click": {"pinch_on": 0.2}}, "calibrate")
    store.undo()
    assert not store.calibration.exists()
    assert store.load()["click"]["pinch_on"] == 0.15


def test_named_state(store):
    store.save({"click": {"pinch_on": 0.2}}, "calibrate")
    store.name("快適")
    store.save({"click": {"pinch_on": 0.3}}, "settings")
    assert store.named_states() == ["快適"]
    store.restore_named("快適")
    assert store.load()["click"]["pinch_on"] == 0.2
    with pytest.raises(ValueError):
        store.name("../外")


def test_keeps_latest_snapshots(store, monkeypatch):
    monkeypatch.setattr(settings, "KEEP", 5)
    store.name("残す")
    for i in range(8):
        store.save({"click": {"pinch_on": 0.1 + i / 100}}, "calibrate")
    assert len(store.snapshots()) == 5
    assert store.named_states() == ["残す"]  # 名前を付けた状態は数に入れない


def test_auto_save_makes_no_snapshot(store):
    store.save_auto({"cursor": {"roi_cx": 0.47}})
    assert store.snapshots() == []
    assert json.loads(store.changes.read_text())["layer"] == "auto"


def test_same_setting_same_name(store):
    """同じ中身の設定には同じ名前: 名前を付けた状態があればその名前、無ければ最初に現れた版の名前。"""
    store.save({"click": {"pinch_on": 0.2}}, "calibrate")  # 初期状態 + A
    store.save({"click": {"pinch_on": 0.3}}, "settings")  # B
    store.save({"click": {"pinch_on": 0.2}}, "settings")  # A と同じ
    snaps = store.snapshots()
    names = store.names()
    first_a = snaps[1]["id"]
    assert snaps[3]["key"] == snaps[1]["key"] and names[snaps[3]["key"]] == first_a
    assert names[snaps[2]["key"]] == snaps[2]["id"]
    store.name("快適")  # 今（A）に名前を付ける
    assert store.names()[snaps[1]["key"]] == "快適"
    assert store.current_key() == snaps[1]["key"]
