"""設定画面のサーバー（aircontroller/settings_server.py、SPEC 13.46）: セキュリティの確認と、閲覧・保存の API。"""
import http.client
import json
import shutil
import threading
from pathlib import Path

import pytest

from aircontroller.settings_server import App, serve

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def server(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    app = App(tmp_path)
    httpd, url = serve(app, 0)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield app, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": f"127.0.0.1:{port}"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, out


def api(app, port, method, path, body=None, **extra):
    h = {"X-Token": app.token, "Origin": f"http://127.0.0.1:{port}"}
    h.update(extra)
    s, out = req(port, method, path, body, h)
    return s, json.loads(out)


def test_page_needs_token(server):
    app, port = server
    assert req(port, "GET", "/")[0] == 403
    assert req(port, "GET", "/?t=wrong")[0] == 403
    s, body = req(port, "GET", f"/?t={app.token}")
    assert s == 200 and b"AirController" in body


def test_api_needs_token_host_origin(server):
    app, port = server
    assert req(port, "GET", "/api/state")[0] == 403  # トークンなし
    assert req(port, "GET", "/api/state", headers={"X-Token": "x"})[0] == 403
    # DNS リバインディング: 別の名前で来た要求は断る
    assert req(port, "GET", "/api/state", headers={"X-Token": app.token, "Host": "evil.example:80"})[0] == 403
    # 別のサイトからの要求は断る
    assert api(app, port, "POST", "/api/save", {"values": {}}, Origin="http://evil.example")[0] == 403
    # POST は Origin と JSON が要る
    assert req(port, "POST", "/api/save", {"values": {}}, {"X-Token": app.token})[0] == 403


def test_state_hides_d_and_save_roundtrip(server):
    app, port = server
    s, st = api(app, port, "GET", "/api/state")
    assert s == 200
    ids = {p["id"] for p in st["params"]}
    assert "click.pinch_on" in ids and "camera.index" not in ids and "safety.emergency_keycode" not in ids
    assert "click.click_min_ms" not in ids  # C も画面に出さない
    assert st["toggles"]["zoom.zoom_enabled"] and "power.auto_off_after_s" in st["off_at_zero"]
    s, r = api(app, port, "POST", "/api/save", {"values": {"click.pinch_off": 0.27, "cursor.roi_adapt": False}})
    assert s == 200 and len(r["changes"]) == 2
    st = api(app, port, "GET", "/api/state")[1]
    p = next(p for p in st["params"] if p["id"] == "click.pinch_off")
    assert p["saved"] == 0.27 and p["source"] == "calibration"
    assert st["history"][0]["route"] == "settings"
    s, r = api(app, port, "POST", "/api/undo", {})
    assert s == 200 and app.store.load()["click"]["pinch_off"] == 0.22


@pytest.mark.parametrize("values", [{"camera.index": 1}, {"safety.timeout_s": 5}, {"nope.x": 1}, {"click.click_min_ms": 50.0},
                                    {"thumbsup.thumbsup_x_w_thumb": 0.5}, {"power.wake_hold_s": 0.0},
                                    {"click.pinch_on": 5.0}, {"click.pinch_on": "0.2"}, {"gesture.hand": "middle"},
                                    {"cursor.roi_adapt": 1}])
def test_save_rejects_bad_values(server, values):
    app, port = server
    s, r = api(app, port, "POST", "/api/save", {"values": values})
    assert s == 400 and "error" in r
    assert not app.store.calibration.exists()


def test_int_and_choice_coercion(server):
    app, _ = server
    upd = app.coerce({"click.drag_ms": 600, "zoom.zoom_sign": -1, "gesture.hand": "left"})
    assert upd == {"gesture": {"hand": "left"}, "zoom": {"zoom_sign": -1.0}, "click": {"drag_ms": 600.0}}
    assert isinstance(upd["click"]["drag_ms"], float) and isinstance(upd["zoom"]["zoom_sign"], float)


def test_measure_rejects_unknown_unit(server):
    app, port = server
    assert api(app, port, "POST", "/api/measure", {"units": ["rm -rf"]})[0] == 400


def test_measured_ignores_recompute(server, tmp_path):
    app, _ = server
    h = tmp_path / "calibration-history"
    h.mkdir()
    res = {"results": {"pinch": {"candidate": {"click": {"pinch_on": 0.2}}, "warnings": []}}}
    (h / "20261026-100000.json").write_text(json.dumps({"t": "a", **res}))
    res["results"]["pinch"]["candidate"]["click"]["pinch_on"] = 0.3
    (h / "20261026-110000-from-calib-x.json").write_text(json.dumps({"t": "b", **res}))
    assert app.measured()["click.pinch_on"]["value"] == 0.2


def test_zero_turns_off(server):
    app, _ = server
    assert app.coerce({"power.auto_off_after_s": 0.0, "power.idle_stop_s": 0}) == {"power": {"auto_off_after_s": 0.0, "idle_stop_s": 0.0}}
    with pytest.raises(ValueError):
        app.coerce({"power.auto_off_after_s": 0.5})  # 0 より上は範囲（1〜30）で確かめる


def test_dist_from_learn_recording(server, tmp_path):
    """いいね・ズームは、測定が無ければ learn の記録（ラベル付き）から分布を出す。"""
    app, _ = server
    assert app.dist("thumbsup")["available"] is False
    src = ROOT / "recordings" / "learn-zoom-20261024-061204.jsonl"
    if not src.exists():
        pytest.skip("recordings/ の learn-zoom の記録が無い")
    (tmp_path / "recordings").mkdir()
    shutil.copy(src, tmp_path / "recordings" / src.name)
    shutil.copy(src.with_suffix(".labels.json"), tmp_path / "recordings" / src.with_suffix(".labels.json").name)
    d = app.dist("zoom")
    assert d["available"] and {p["label"] for p in d["points"]} == {"neutral", "in", "out"}


class _FakeJob:
    def __init__(self, kind):
        self.kind, self.terminated = kind, False
        self.proc = self

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True


def test_shutdown_needs_token(server):
    app, port = server
    assert api(app, port, "POST", "/api/shutdown", {}, **{"X-Token": "x"})[0] == 403
    assert not app.closed_by_page


def test_shutdown_busy_then_force(server):
    app, port = server
    ev, me = _FakeJob("eval"), _FakeJob("measure")
    app.jobs = {"a": ev, "b": me}
    assert api(app, port, "POST", "/api/shutdown", {"force": False}) == (200, {"busy": ["eval", "measure"]})
    assert not app.closed_by_page and not ev.terminated
    assert api(app, port, "GET", "/api/run")[0] == 200  # まだ動いている
    s, out = api(app, port, "POST", "/api/shutdown", {"force": True})
    assert (s, out) == (200, {"ok": True, "left": ["measure"]})
    assert ev.terminated and not me.terminated  # 見積もりは止め、測定は最後まで動かす
    assert app.closed_by_page


def test_shutdown_stops_server(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    app = App(tmp_path)
    httpd, _ = serve(app, 0)
    port = httpd.server_address[1]
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    assert api(app, port, "POST", "/api/shutdown", {}) == (200, {"ok": True, "left": []})
    th.join(timeout=5)  # serve_forever が戻る（settings コマンドが終わる）
    assert not th.is_alive()
    httpd.server_close()
