"""実行中の設定の差し替え（GestureEngine.apply_config）と、設定モードの打ち切り（interrupt）。SPEC 13.47。"""
import dataclasses
import json
from pathlib import Path

import pytest

from aircontroller.gesture import GestureConfig, GestureEngine, run
from aircontroller.recording import iter_frames
from tests.test_gesture_replay import events_to_json

FIX = Path(__file__).parent / "fixtures"

# 差し替えで作り直す値（保持時間・平滑化・コントローラーが写す値・操作範囲）を広く変える
CHANGES = dict(posture_hold_ms=90.0, posture_release_ms=200.0, acquire_hold_ms=80.0, lose_hold_ms=220.0,
               cursor_min_cutoff=0.6, cursor_beta=0.05, cursor_d_cutoff=1.5, zoom_enter_hold_ms=250.0,
               zoom_min_cutoff=1.4, zoom_beta=0.3, peace_min_cutoff=1.5, peace_beta=0.05, peace_dead_disp=0.4,
               peace_max_disp=1.5, peace_fire_disp=0.6, peace_rearm_disp=0.2, nav_dwell_ms=300.0, nav_cooldown_ms=400.0,
               thumbsup_dead_disp=0.5, thumbsup_fire_left=1.0, thumbsup_fire_right=1.3, thumbsup_fire_up=0.8,
               thumbsup_dwell_ms=300.0, thumbsup_cooldown_ms=500.0, pinch_on=0.21, pinch_off=0.26, drag_ms=450.0,
               roi_cx=0.45, roi_cy=0.6, roi_width=0.5, zoom_dead=0.06, zoom_fire_level=0.6)


def golden_config(name: str) -> GestureConfig:
    g = json.loads((FIX / f"{name}.events.json").read_text())["config"]
    known = {f.name for f in dataclasses.fields(GestureConfig)}
    return GestureConfig(**{k: v for k, v in g.items() if k in known})


@pytest.mark.parametrize("name", ["use03", "peace04", "thumbsup01", "zoom02"])
def test_apply_before_first_frame_equals_fresh_engine(name):
    """設定 A のエンジンを、最初のフレームの前に設定 B に差し替えると、最初から B で作ったエンジンと同じイベント列になる
    （作り直し忘れの値があれば、ここで差が出る）。"""
    a = golden_config(name)
    b = dataclasses.replace(a, **CHANGES)
    want = events_to_json(run(GestureEngine(dataclasses.replace(b)), iter_frames(FIX / f"{name}.jsonl")))
    eng = GestureEngine(dataclasses.replace(a))
    eng.reset()
    changed = eng.apply_config(b)
    assert set(changed) == {k for k in CHANGES if getattr(a, k) != CHANGES[k]}
    got = []
    for f in iter_frames(FIX / f"{name}.jsonl"):
        got.extend(eng.update(f))
    assert events_to_json(got) == want


@pytest.mark.parametrize("name", ["use03", "peace04", "zoom02"])
def test_applying_same_config_every_frame_changes_nothing(name):
    cfg = golden_config(name)
    expected = json.loads((FIX / f"{name}.events.json").read_text())["events"]
    eng = GestureEngine(dataclasses.replace(cfg))
    eng.reset()
    got = []
    for f in iter_frames(FIX / f"{name}.jsonl"):
        assert eng.apply_config(dataclasses.replace(cfg)) == []
        got.extend(eng.update(f))
    assert events_to_json(got) == expected


def test_roi_kept_unless_changed():
    eng = GestureEngine(GestureConfig(roi_cx=0.5, roi_width=0.4))
    eng.roi_cx, eng.roi_width = 0.55, 0.45  # 自動補正で動いた
    eng.apply_config(GestureConfig(roi_cx=0.5, roi_width=0.4, pinch_on=0.2))
    assert (eng.roi_cx, eng.roi_width) == (0.55, 0.45)
    eng.apply_config(GestureConfig(roi_cx=0.42, roi_width=0.4, pinch_on=0.2))
    assert (eng.roi_cx, eng.roi_width) == (0.42, 0.4)


def test_hand_is_not_switched_live():
    eng = GestureEngine(GestureConfig(hand="right"))
    assert eng.apply_config(GestureConfig(hand="left")) == []
    assert eng.cfg.hand == "right"


def test_safe_moment_and_interrupt_during_drag():
    """ドラッグの途中は差し替えない。打ち切るとボタンを離すイベントが出る。"""
    cfg = golden_config("use03")
    eng = GestureEngine(dataclasses.replace(cfg))
    eng.reset()
    seen_unsafe = False
    for f in iter_frames(FIX / "use03.jsonl"):
        eng.update(f)
        if eng.state.mode == "drag":
            assert not eng.safe_to_reconfigure()
            seen_unsafe = True
            out = eng.interrupt(f.t_capture_ns)
            assert [e.kind for e in out] == ["drag_end", "release_all"]
            eng.update(f)  # 打ち切った後は、ピンチもドラッグも始まっていない
            assert eng._pinch_start is None and not eng._dragging
            break
    assert seen_unsafe


# ---------------------------------------------------------------- run に内蔵するサーバー（LiveHub）
import http.client  # noqa: E402
import shutil  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from aircontroller.live import LiveHub  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def hub(tmp_path):
    shutil.copy(ROOT / "config.toml", tmp_path / "config.toml")
    from aircontroller.settings import SettingsStore

    store = SettingsStore(tmp_path)
    h = LiveHub(tmp_path, store.load, (1440.0, 900.0))
    h.start()
    yield h
    h.close()


def _req(h, method, path, body=None, token=True):
    port = h.httpd.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    hd = {"Host": f"127.0.0.1:{port}", **({"X-Token": h.token} if token else {})}
    c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=hd)
    r = c.getresponse()
    out = r.status, r.read()
    c.close()
    return out


def test_live_json_and_token(hub):
    info = json.loads(hub.path.read_text())
    assert info["port"] == hub.httpd.server_address[1] and info["token"] == hub.token
    assert oct(hub.path.stat().st_mode & 0o777) == "0o600"
    assert _req(hub, "GET", "/stats", token=False)[0] == 403
    assert _req(hub, "POST", "/config", {"values": {"click.click_min_ms": 10.0}})[0] == 400  # C は受け付けない
    hub.close()
    assert not hub.path.exists()


def test_settings_mode_stream_config_and_revert(hub):
    """画面がつながると設定モード（出力を止め、打ち切る）。変更中の値は安全な瞬間に差し替え、画面が離れたら保存済みに戻す。"""
    cfg = golden_config("use03")
    eng = GestureEngine(dataclasses.replace(cfg))
    eng.reset()
    frames = list(iter_frames(FIX / "use03.jsonl"))
    got_lines = []
    leave = threading.Event()

    def client():
        port = hub.httpd.server_address[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/stream", headers={"Host": f"127.0.0.1:{port}", "X-Token": hub.token})
        r = c.getresponse()
        while not leave.is_set():
            line = r.fp.readline()
            if line.startswith(b"data: "):
                got_lines.append(json.loads(line[6:]))
        c.close()

    th = threading.Thread(target=client, daemon=True)
    th.start()
    for _ in range(100):
        if hub.clients:
            break
        time.sleep(0.02)
    assert hub.settings_mode and hub.suppress_output()
    assert _req(hub, "POST", "/config", {"values": {"click.pinch_on": 0.19, "click.drag_ms": 600.0}})[0] == 200
    entered = False
    for f in frames[:1500]:
        evs = hub.before_update(eng, f.t_capture_ns)
        if not entered:
            assert [e.kind for e in evs][-1] == "release_all"  # 設定モードに入った最初のフレームで打ち切る
            entered = True
        eng.update(f)
        hub.publish(eng, f, f.active)
        time.sleep(0.0005)
    assert eng.cfg.pinch_on == 0.19 and eng.cfg.drag_ms == 600.0
    assert hub.settings_mode  # まだつながっている
    leave.set()
    th.join(timeout=5)
    assert len(got_lines) >= 3 and got_lines[-1]["settings_mode"] and "mode" in got_lines[-1]
    for _ in range(100):  # クライアントが離れる
        if not hub.clients:
            break
        time.sleep(0.05)
    assert not hub.settings_mode
    for f in frames[1500:2000]:
        hub.before_update(eng, f.t_capture_ns)
        eng.update(f)
    assert eng.cfg.pinch_on == 0.15 and eng.cfg.drag_ms == 500.0  # 保存済み（config.toml）に戻った


def test_output_toggle_interrupts(hub):
    eng = GestureEngine(GestureConfig())
    hub.clients = 1
    hub.before_update(eng, 0)
    assert hub.suppress_output()
    _req(hub, "POST", "/mode", {"output": True})
    assert not hub.suppress_output()
    assert [e.kind for e in hub.before_update(eng, 1)] == ["release_all"]
    hub.clients = 0


def test_roi_adapt_paused_in_settings_mode(hub):
    """設定モードの間は、操作範囲の自動補正を止める（画面で範囲を変えているのとぶつからないように）。"""
    cfg = dataclasses.replace(golden_config("use03"), roi_adapt=True, roi_width=0.2, roi_width_min=0.15)
    eng = GestureEngine(dataclasses.replace(cfg))
    eng.reset()
    hub.clients = 1
    frames = list(iter_frames(FIX / "use03.jsonl"))
    start = (eng.roi_cx, eng.roi_cy, eng.roi_width)
    for f in frames[:2000]:
        hub.before_update(eng, f.t_capture_ns)
        eng.update(f)
    assert (eng.roi_cx, eng.roi_cy, eng.roi_width) == start
    hub.clients = 0
    for f in frames[2000:]:
        hub.before_update(eng, f.t_capture_ns)
        eng.update(f)
    assert eng.roi_width > start[2]  # 設定モードを抜けると、また補正する


def test_practice_mode_suppresses_keys_only_and_sends_events(hub):
    """練習モード（SPEC 13.60）: キーの出力だけを止め（マウスは出す）、設定モードにはならない。発火したイベントを今の値と一緒に送る。"""
    from aircontroller.types import Event

    got, leave = [], threading.Event()

    def client():
        port = hub.httpd.server_address[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/stream?practice=1", headers={"Host": f"127.0.0.1:{port}", "X-Token": hub.token})
        r = c.getresponse()
        while not leave.is_set():
            line = r.fp.readline()
            if line.startswith(b"data: "):
                got.append(json.loads(line[6:]))
        c.close()

    th = threading.Thread(target=client, daemon=True)
    th.start()
    for _ in range(100):
        if hub.practice:
            break
        time.sleep(0.02)
    assert hub.practice == 1 and not hub.settings_mode
    assert hub.suppress_keys() and not hub.suppress_output()
    eng = GestureEngine(golden_config("peace04"))
    eng.reset()
    frames = list(iter_frames(FIX / "peace04.jsonl"))
    eng.update(frames[0])
    hub.note_events([Event("nav", 1, {"dir": "back"}), Event("scroll", 2, {"dy": 3}), Event("scroll", 3, {"dy": 4}),
                     Event("move", 4, {"x": 1, "y": 2})], True)
    hub._last_pub = 0.0
    hub.publish(eng, frames[0], True)
    for _ in range(100):
        if got:
            break
        time.sleep(0.02)
    leave.set()
    assert got[0]["practice"] is True and got[0]["settings_mode"] is False
    assert got[0]["events"] == [{"kind": "nav", "active": True, "dir": "back"}, {"kind": "scroll", "active": True, "dy": 7}]
