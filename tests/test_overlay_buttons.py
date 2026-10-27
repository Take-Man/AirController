"""オーバーレイのボタン（カメラ・設定・畳む）の配置と、ギアのボタンから設定画面を開く処理（SPEC 13.51）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

AppKit = pytest.importorskip("AppKit")

from Foundation import NSMakeRect  # noqa: E402

from aircontroller.output import overlay as O  # noqa: E402
from aircontroller.settings_launch import INFO, SettingsLauncher, remove_info, running_url, write_info  # noqa: E402

TY = O.PAD + O.CAM_H + 10  # 展開した表示のモードの行（_draw_full と同じ）


def _overlap(a, b) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


@pytest.mark.parametrize("rects,size", [(O.full_buttons(TY), (O.W, O.H)), (O.compact_buttons(), (O.COMPACT_W, O.COMPACT_H)),
                                        (O.small_buttons(), (O.SMALL_W, O.SMALL_H))], ids=["full", "compact", "small"])
def test_buttons_fit_and_settings_next_to_camera(rects, size):
    names = [n for n, _ in rects]
    assert names[:2] == ["camera", "settings"]  # ギアはカメラのボタンの左隣
    for i, (_, r) in enumerate(rects):
        assert 0 <= r[0] and r[0] + r[2] <= size[0] and 0 <= r[1] and r[1] + r[3] <= size[1]
        for _, q in rects[i + 1:]:
            assert not _overlap(r, q)
    cam, gear = rects[0][1], rects[1][1]
    assert 0 < cam[0] - (gear[0] + gear[2]) <= 8 and cam[1] == gear[1]


def test_text_room_kept():
    # 畳んだ帯の文字の幅は以前（ボタン 2 つ、幅 236）と同程度、No signal の文字（最も長い「カメラを起動できません」152pt）は切れない
    assert min(r[0] for _, r in O.compact_buttons()) - 6 - 52 >= 120
    assert min(r[0] for _, r in O.small_buttons()) - 6 - 14 >= 155


@pytest.mark.parametrize("state,shown", [
    ({"camera": "running", "active": False}, True),   # オフ
    ({"camera": "running", "active": True}, False),   # 操作中は出さない（クリックを通す）
    ({"camera": "stopped", "active": False}, True),   # No signal
    ({"camera": "error", "active": True}, True),
    ({"camera": "starting", "active": False}, False),
    (None, False)])
def test_buttons_shown(state, shown):
    assert O.buttons_shown(state) is shown


def _state(**kw):
    s = {"camera": "running", "camera_reason": "", "active": False, "mode": "idle", "power": "off", "roi": (0.2, 0.2, 0.6, 0.6),
         "landmarks": None, "palm": None, "pinch_progress": 0.0, "pinch_n": float("nan"), "status": "", "warning": "",
         "peace": None, "hand_label": "-", "config_label": "右手"}
    s.update(kw)
    return s


def _draw(state, collapsed=False):
    calls = []
    v = O.OverlayView.alloc().initWithFrame_(NSMakeRect(0, 0, O.W, O.H))
    v.on_button = lambda: calls.append("camera")
    v.on_settings = lambda: calls.append("settings")
    v.on_collapse = lambda: calls.append("collapse")
    v.collapsed = collapsed
    v.state = state
    img = AppKit.NSImage.alloc().initWithSize_((O.W, O.H))
    img.lockFocus()
    try:
        v.drawRect_(v.bounds())
    finally:
        img.unlockFocus()
    return v, calls


@pytest.mark.parametrize("state,collapsed,rects", [
    (_state(), False, O.full_buttons(TY)),
    (_state(), True, O.compact_buttons()),
    (_state(camera="stopped", camera_reason="手動"), False, O.small_buttons()),
    (_state(camera="stopped"), True, O.small_buttons())], ids=["off", "off-collapsed", "no-signal", "no-signal-collapsed"])
def test_gear_drawn_and_pressable(state, collapsed, rects):
    v, calls = _draw(state, collapsed)
    assert [tuple(r) for r, _ in v.buttons] == [tuple(r) for _, r in rects]
    gear = dict(rects)["settings"]
    assert v.press(gear[0] + gear[2] / 2, gear[1] + gear[3] / 2)
    assert calls == ["settings"]


def test_no_buttons_while_active():
    v, calls = _draw(_state(active=True, mode="cursor"))
    assert v.buttons == [] and not v.press(O.W - 20, TY + 10)


def test_no_gear_without_callback():
    v, _ = _draw(_state())
    v.on_settings = None
    img = AppKit.NSImage.alloc().initWithSize_((O.W, O.H))
    img.lockFocus()
    v.drawRect_(v.bounds())
    img.unlockFocus()
    assert len(v.buttons) == 2


# ---------------------------------------------------------------- 設定画面を開く


class FakeProc:
    pid = 4242

    def poll(self):
        return None


def _launcher(tmp_path, probe=lambda u: True, popen_exc=None, config=None):
    opened, spawned = [], []

    def popen(argv, **kw):
        if popen_exc:
            raise popen_exc
        spawned.append((argv, kw))
        return FakeProc()

    focused = []

    def focus(url, tab):
        focused.append((url, tab))
        return True

    lz = SettingsLauncher(tmp_path, config, popen=popen, probe=probe, background=False, focus=focus)
    lz.focused = focused
    return lz, opened, spawned


def test_info_file_owner_only_and_removed(tmp_path):
    write_info(tmp_path, 1234, "http://127.0.0.1:1234/?t=abc")
    assert (os.stat(tmp_path / INFO).st_mode & 0o777) == 0o600
    assert json.loads((tmp_path / INFO).read_text()) == {"pid": os.getpid(), "port": 1234, "url": "http://127.0.0.1:1234/?t=abc",
                                                          "app": False}
    remove_info(tmp_path)
    assert not (tmp_path / INFO).exists()


def test_focuses_running_app(tmp_path):
    """アプリが動いていれば、そのウィンドウを前に出して設定タブを開く（ブラウザは開かない。SPEC 13.58）。"""
    write_info(tmp_path, 1234, "http://127.0.0.1:1234/?t=abc", app=True)  # 自分の PID（生きている）
    lz, opened, spawned = _launcher(tmp_path)
    lz()
    assert lz.focused == [("http://127.0.0.1:1234/?t=abc", "settings")] and opened == [] and spawned == []


def test_browser_server_still_starts_app(tmp_path):
    """ブラウザで開く設定画面のサーバー（settings コマンド）だけが動いていても、アプリを起動する（作者の FB）。"""
    write_info(tmp_path, 1234, "http://127.0.0.1:1234/?t=abc")
    lz, opened, spawned = _launcher(tmp_path)
    lz()
    assert opened == [] and len(spawned) == 1 and spawned[0][0][-3:] == ["app", "--tab", "settings"]


def test_starts_new_server_detached(tmp_path):
    lz, opened, spawned = _launcher(tmp_path, config="my.toml")
    lz()
    (argv, kw), = spawned
    assert argv == [sys.executable, "-m", "aircontroller", "--config", "my.toml", "app", "--tab", "settings"]
    assert kw["start_new_session"] is True and kw["cwd"] == tmp_path and kw["stdin"] == subprocess.DEVNULL
    assert opened == []  # ブラウザは開かない（アプリのウィンドウで設定タブを開く）
    lz()  # 起動してすぐ（settings-server.json がまだない）に押しても、2 つ目は立てない
    assert len(spawned) == 1


def test_stale_info_starts_new(tmp_path):
    (tmp_path / INFO).write_text(json.dumps({"pid": 2 ** 22 + 12345, "port": 1, "url": "http://127.0.0.1:1/?t=x"}))
    assert running_url(tmp_path, lambda u: True) is None  # PID が生きていない
    write_info(tmp_path, 1, "http://127.0.0.1:1/?t=x")
    assert running_url(tmp_path, lambda u: False) is None  # PID は生きているが応答しない（PID の使い回し）
    lz, opened, spawned = _launcher(tmp_path, probe=lambda u: False)
    lz()
    assert len(spawned) == 1 and opened == []


def test_launch_failure_does_not_raise(tmp_path, capsys):
    lz, opened, spawned = _launcher(tmp_path, popen_exc=OSError("no python"))
    lz()
    assert "設定画面を開けませんでした" in capsys.readouterr().out
