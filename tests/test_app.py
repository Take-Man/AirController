"""アプリの枠組み（TASKS-20261027-package 第5節、SPEC 13.58）のうち、ウィンドウを出さずに確かめられる部分。"""
from __future__ import annotations

import sys

from aircontroller.settings_launch import cli_argv, focus_app, is_app, write_info
from aircontroller.settings_server import RUN_RE, App


def test_cli_argv_source_and_frozen(monkeypatch):
    assert cli_argv("run") == [sys.executable, "-m", "aircontroller", "run"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert cli_argv("run") == [sys.executable, "run"]  # .app では実行ファイルに引数を渡す


def test_run_re_matches_source_and_app():
    assert RUN_RE.search(".venv/bin/python -m aircontroller run")
    assert RUN_RE.search("/Applications/AirController.app/Contents/MacOS/AirController run")
    assert RUN_RE.search("/Applications/AirController.app/Contents/MacOS/AirController --config x.toml run")
    assert not RUN_RE.search("/Applications/AirController.app/Contents/MacOS/AirController settings")
    assert not RUN_RE.search(".venv/bin/python -m aircontroller runner")


def test_app_endpoints_without_runner(tmp_path):
    app = App(tmp_path)
    st = app.app_status()
    assert st["app"] is False and "others" in st
    for call in (app.app_start, lambda: app.app_stop(False), lambda: app.app_focus("settings")):
        try:
            call()
        except ValueError as e:
            assert "アプリの中" in str(e)
        else:
            raise AssertionError("アプリの外で動いてしまった")


def test_focus_app_via_server(tmp_path):
    """ギアのボタン: アプリのサーバーに POST /api/app/focus を送ると、ウィンドウを前に出す関数が呼ばれる。"""
    import threading

    from aircontroller.settings_server import serve

    app = App(tmp_path)
    called = []
    app.focus = called.append
    httpd, url = serve(app, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        write_info(tmp_path, httpd.server_address[1], url, app=True)
        assert is_app(tmp_path)
        assert focus_app(url, "settings")
        assert called == ["settings"]
        assert not focus_app(url.replace("t=", "t=x"), "settings")  # トークンが違えば断る
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_quit_in_app_closes_window_and_keeps_server(tmp_path):
    """アプリの中の設定画面の「終了」: サーバーは止めず（ホームも出しているため）、設定のウィンドウを閉じる（作者の FB）。"""
    app = App(tmp_path)
    closed = []
    app.close_window = closed.append
    out = app.shutdown(force=False)
    assert out["ok"] and out["window"] and closed == ["settings"] and not app.closed_by_page
