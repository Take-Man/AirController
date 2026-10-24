"""エントリポイントが読み込め、記録の再生と集計が通ることの確認（カメラ・権限は不要）。"""
from pathlib import Path

from aircontroller.__main__ import main

FIX = Path(__file__).parent / "fixtures"


def test_replay_and_analyze(capsys):
    assert main(["replay", str(FIX / "dark01.jsonl")]) == 0
    assert main(["analyze", str(FIX / "pinch01.jsonl")]) == 0
    out = capsys.readouterr().out
    assert "hand_lost" in out and "ラベル付きピンチ距離" in out
