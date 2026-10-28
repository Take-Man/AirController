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


def test_tutorial_manifest_and_static(tmp_path):
    """チュートリアルの manifest.json の形と、画像の置き場所の外を出さないこと（SPEC 13.60）。"""
    import http.client
    import json
    import threading

    from aircontroller.settings_server import WEB, serve

    m = json.loads((WEB / "tutorial/manifest.json").read_text())
    ids = [s["id"] for s in m["steps"]]
    assert len(ids) == len(set(ids)) == 10
    assert all(len(s.get("frame_text", [])) == s["frames"] and 2 <= s["frames"] <= 3 and s["tips"] for s in m["steps"])
    # スキルツリー: つながる元は 1 つの根（オン・オフ）に行き着き、系統は groups にある
    by = {s["id"]: s for s in m["steps"]}
    roots = [s["id"] for s in m["steps"] if s["parent"] is None]
    assert roots == ["wake"] and all(s["group"] in m["groups"] for s in m["steps"])
    for s in m["steps"]:
        seen, cur = set(), s
        while cur["parent"] is not None:
            assert cur["id"] not in seen
            seen.add(cur["id"])
            cur = by[cur["parent"]]
    app = App(tmp_path)
    httpd, _url = serve(app, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]

        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
            r = c.getresponse()
            return r.status

        assert get("/tutorial/manifest.json") == 200
        assert get("/tutorial/img/none-1.png") == 404
        assert get("/tutorial/../settings_server.py") == 404
        assert get("/tutorial/img/README.md") == 404  # 画像と JSON だけ
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_about_info_lists_data_location(tmp_path, monkeypatch):
    """このアプリについて（第8節）: データの場所と、アプリを消した後に残る場所（.app のときは macOS がアプリごとに作る場所も）。"""
    app = App(tmp_path)
    info = app.app_info()
    assert info["app"] is False and info["leftovers"] == [info["home"]] and info["version"] == ""
    try:
        app.app_reveal()
    except ValueError as e:
        assert "アプリの中" in str(e)
    else:
        raise AssertionError("アプリの外で Finder を開いてしまった")
