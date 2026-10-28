"""環境のチェック（TASKS-20261027-package 第4節）の、権限やカメラに依存しない部分。"""
from __future__ import annotations

from pathlib import Path

import pytest

from aircontroller import envcheck
from aircontroller.envcheck import CheckContext, judge_key


def test_judge_key_levels():
    assert judge_key(93)["level"] == "ok"  # ¥
    assert judge_key(105)["level"] == "ok"  # F13
    assert judge_key(54)["level"] == "refuse"  # 右 Command（切り替えのキー・修飾キー）
    assert judge_key(36)["level"] == "refuse"  # Return
    assert judge_key(47)["level"] == "warn"  # 緊急停止と同じキー
    assert judge_key(102)["level"] == "warn"  # 英数


def test_judge_key_letter_warns():
    r = judge_key(0)  # A のキー（どの配列でも文字を入力する）
    assert r["level"] == "warn" and "入力できなくなります" in r["reason"]


def test_items_have_checks_and_titles():
    ids = [i["id"] for i in envcheck.ITEMS]
    assert ids == list(envcheck.CHECKS)
    assert ids[0] == "source"  # 実行元を必ず最初に出す


def test_one_failing_item_does_not_stop_others(tmp_path, monkeypatch):
    ctx = CheckContext(tmp_path, lambda: {})
    monkeypatch.setitem(envcheck.CHECKS, "system", lambda c: 1 / 0)
    r = ctx.run("system")
    assert r["status"] == "fail" and "エラー" in r["summary"]
    with pytest.raises(ValueError):
        ctx.run("nope")


def test_shortcuts_reports_disabled(monkeypatch):
    """システムのショートカットが無効なら要確認（設定は読むだけ）。"""
    monkeypatch.setattr(envcheck, "read_hotkeys", lambda: {
        "32": {"enabled": False, "value": {"parameters": [65535, 126, 0xA40000]}},
        "79": {"enabled": True, "value": {"parameters": [65535, 123, 0x840000]}},
        "81": {"enabled": True, "value": {"parameters": [65535, 124, 0x840000]}}})
    cfg = {"thumbsup": {"up_keycode": 126, "left_keycode": 124, "right_keycode": 123}}
    r = envcheck.check_shortcuts(CheckContext(Path("."), lambda: cfg))
    assert r["status"] == "warn" and "Mission Control" in r["summary"]


def test_keyboard_shortcut_keys_match_config_on_this_mac():
    """この Mac の配列で、「[」「]」「-」のキーが config.toml のキーコードと一致する（JIS で作ったもの）。配列が読めなければスキップ。"""
    from aircontroller.keyboard import find_keycode, layout, zoom_in_keycode

    lay = layout()
    if lay is None or lay["physical"] != "JIS":
        pytest.skip("JIS 配列でない")
    assert (find_keycode("["), find_keycode("]"), find_keycode("-"), zoom_in_keycode()) == (30, 42, 27, 41)


def test_resolve_shortcut(monkeypatch):
    """"auto" は今の配列から、読めなければ JIS の位置。数値はそのまま。"""
    from aircontroller import keyboard

    assert keyboard.resolve_shortcut("back", 33) == (33, "設定の値")
    monkeypatch.setattr(keyboard, "find_keycode", lambda ch, shift=None: {"[": 33, "]": 30, "-": 27, "=": 24}.get(ch))
    assert keyboard.resolve_shortcut("back", "auto") == (33, "今の配列から")  # US 配列
    assert keyboard.resolve_shortcut("in", "auto") == (24, "今の配列から")
    monkeypatch.setattr(keyboard, "find_keycode", lambda ch, shift=None: None)
    assert keyboard.resolve_shortcut("forward", "auto") == (42, "配列を読めないので JIS の位置")


def test_check_start_stops_run_then_checks(tmp_path, monkeypatch):
    """run が動いていれば、まず止めてよいか聞き（need_stop）、承認されたら SIGINT で止めてから始める（作者の FB: run 実行中に
    環境のチェックを押しても何も起きなかった）。本物の run には触れないよう、run の代わりのプロセスを使う。"""
    import subprocess
    import sys

    from aircontroller import settings_server
    from aircontroller.settings_server import App

    dummy = subprocess.Popen([sys.executable, "-c", "import time\ntry:\n time.sleep(60)\nexcept KeyboardInterrupt:\n pass"])
    try:
        monkeypatch.setattr(settings_server, "run_pids", lambda: [dummy.pid] if dummy.poll() is None else [])
        app = App(tmp_path)
        assert app.check_start(False) == {"need_stop": True, "run": [dummy.pid]}
        assert dummy.poll() is None  # 承認の前は止めない
        assert app.check_start(True) == {"ok": True}
        assert dummy.wait(5) is not None and app._check is not None
    finally:
        if dummy.poll() is None:
            dummy.kill()
