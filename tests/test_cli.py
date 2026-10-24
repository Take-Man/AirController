"""エントリポイントが読み込め、記録の再生と集計が通ることの確認（カメラ・権限は不要）。"""
from pathlib import Path

from aircontroller.__main__ import main

FIX = Path(__file__).parent / "fixtures"


def test_replay_and_analyze(capsys):
    assert main(["replay", str(FIX / "dark01.jsonl")]) == 0
    assert main(["analyze", str(FIX / "pinch01.jsonl")]) == 0
    out = capsys.readouterr().out
    assert "hand_lost" in out and "ラベル付きピンチ距離" in out


def test_all_modules_import():
    """run の途中で初めて読み込まれるモジュール（overlay など）も、構文エラーなく読み込めること。"""
    import importlib
    import pkgutil

    import aircontroller
    import aircontroller.output

    for pkg in (aircontroller, aircontroller.output):
        for m in pkgutil.iter_modules(pkg.__path__):
            if m.name != "__main__":
                importlib.import_module(f"{pkg.__name__}.{m.name}")
