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


def test_real_config_loads():
    """実際の config.toml（＋ calibration.toml）から、各層の設定を作れること（設定の項目を足したときに run が起動時に
    落ちないように。[tracker] の two_hands_hz で TrackerConfig が落ちたことがある）。"""
    from aircontroller.__main__ import _tracker_config, load_config
    from aircontroller.eventlog import EventLogConfig, MarkConfig
    from aircontroller.gesture import GestureConfig
    from aircontroller.input import CameraConfig
    from aircontroller.power import PowerConfig

    cfg = load_config(None)
    _tracker_config(cfg)
    CameraConfig(**cfg["camera"])
    GestureConfig.from_toml(cfg, (1440.0, 900.0))
    PowerConfig.from_toml(cfg)
    EventLogConfig.from_toml(cfg)
    MarkConfig.from_toml(cfg)
