"""今の設定でのゴールデンテスト（SPEC 13.50）。

tests/golden-current/ の要約ごとに、記録を今の設定（config.toml + calibration.toml）で流し、イベント列が変わっていないことを確かめる。
記録の実体がない（recordings/ は git の管理外）ときはスキップする。
- 設定が変わった（設定画面で確定した）: 挙動の変化が意図したものか確かめてから作り直す。
- 設定が同じでイベント列が変わった: コードの変更で挙動が変わった。
比べる・作り直す: .venv/bin/python tools/golden_diff.py --current [--write]
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.golden_current import ROOT, compute, config_json, current_config, describe, expected_files

CMD = ".venv/bin/python tools/golden_diff.py --current"


@pytest.fixture(scope="module")
def cfg():
    return current_config()


def test_expected_exist():
    assert expected_files(), f"期待値がありません: {CMD} --write"


@pytest.mark.parametrize("path", expected_files(), ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_replay_matches_current_settings(path: Path, cfg):
    expected = json.loads(path.read_text())
    rec = ROOT / expected["recording"]
    if not rec.exists():
        pytest.skip(f"記録がありません: {expected['recording']}")
    now = config_json(cfg)
    changed = sorted(k for k in set(now) | set(expected["config"]) if now.get(k) != expected["config"].get(k))
    assert not changed, (f"設定が期待値を作ったときと違います（GestureConfig の項目を足し引きした場合も含む。{', '.join(changed)}）。"
                         f"挙動の変化を {CMD} で確かめ、意図したものなら {CMD} --write で作り直してください")
    got = compute(rec, cfg)
    assert got["sha256"] == expected["sha256"], (
        "設定は同じで、イベント列が変わりました（コードの変更で挙動が変わった）: "
        + " / ".join(describe(expected, got)) + f"。詳しくは {CMD} {expected['recording']}")
