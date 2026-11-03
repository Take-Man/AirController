"""待機中の省電力: 推論の間引きと、カメラの停止・再起動（SPEC 13.22）。実カメラの代わりに、決まった間隔でフレームを出す偽物を使う。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np

from aircontroller import input as inp

MODEL = str(Path(__file__).resolve().parent.parent / "models" / "hand_landmarker.task")


class FakeCamera:
    """30fps で黒い画像を出す。Camera と同じ wait_newer / stop / capture_times / actual_size を持つ。"""

    def __init__(self, cfg=None):
        self.cfg = cfg or inp.CameraConfig()
        self._cond = threading.Condition()
        self.seq, self._t_ns, self._frame, self._stop = 0, 0, None, False
        self.capture_times: list[int] = []
        self.actual_size = (320, 240)
        self.stopped = False

    def start(self, *_):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        img = np.zeros((240, 320, 3), np.uint8)
        while not self._stop:
            time.sleep(1 / 30)
            with self._cond:
                self.seq += 1
                self._t_ns = time.perf_counter_ns()
                self._frame = img
                self.capture_times.append(self._t_ns)
                self._cond.notify_all()

    wait_newer = inp.Camera.wait_newer

    def stop(self):
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        self.stopped = True


def count_frames(min_interval, seconds=1.0):
    cam = FakeCamera()
    cam.start()
    n = []
    tr = inp.Tracker(cam, inp.TrackerConfig(model_path=MODEL), lambda f: n.append(f), min_interval)
    tr.start()
    time.sleep(seconds)
    tr.stop()
    cam.stop()
    return len(n)


def test_idle_throttling():
    full = count_frames(None)
    idle = count_frames(lambda: 0.1)  # 10fps
    assert full >= 20 and 5 <= idle <= 13 and idle < full / 2


def test_pipeline_stop_and_restart(monkeypatch):
    cams = []

    def fake_open(cfg):
        c = FakeCamera(cfg)
        cams.append(c)
        return c

    monkeypatch.setattr(inp, "open_camera", fake_open)
    frames = []
    pipe = inp.VisionPipeline(inp.CameraConfig(), inp.TrackerConfig(model_path=MODEL), frames.append)
    pipe.start()
    assert pipe.state == "running"
    time.sleep(0.3)
    pipe.stop()
    assert pipe.state == "stopped" and cams[0].stopped and pipe.camera is None
    n = len(frames)
    time.sleep(0.2)
    assert len(frames) == n  # 止めたらフレームは来ない
    pipe.start_async()
    for _ in range(50):
        if pipe.state == "running":
            break
        time.sleep(0.05)
    assert pipe.state == "running" and len(cams) == 2
    time.sleep(0.3)
    assert len(frames) > n
    pipe.stop()


def test_pipeline_keeps_last_camera(monkeypatch):
    """止めた後も、終了時の集計に使うカメラ（last_camera）を参照できる（run の終了時に NameError になっていた）。"""
    monkeypatch.setattr(inp, "open_camera", lambda cfg: FakeCamera(cfg))
    pipe = inp.VisionPipeline(inp.CameraConfig(), inp.TrackerConfig(model_path=MODEL), lambda f: None)
    pipe.start()
    pipe.stop()
    assert pipe.camera is None and isinstance(pipe.last_camera, FakeCamera)


def test_tracker_switches_num_hands():
    """auto_two_hands: 推論器を 2 つ持ち、num_hands_fn の値で切り替える。"""
    cam = FakeCamera()
    cam.start()
    want = [1]
    tr = inp.Tracker(cam, inp.TrackerConfig(model_path=MODEL, auto_two_hands=True), lambda f: None, None,
                     lambda: want[0])
    assert set(tr._landmarkers) == {1, 2}
    tr.start()

    def wait(cond, timeout=2.0):  # 負荷が高いと推論の 1 回目が遅れるので、決まった時間ではなく条件を待つ
        t0 = time.monotonic()
        while not cond() and time.monotonic() - t0 < timeout:
            time.sleep(0.02)
        return cond()

    assert wait(lambda: tr.current_num_hands == 1)
    want[0] = 2
    assert wait(lambda: tr.current_num_hands == 2) and tr.error is None
    tr.stop()
    cam.stop()


class FakeCap:
    """cv2.VideoCapture の代わり: 30fps で grab でき、retrieve（画像への変換）の回数を数える。"""

    def __init__(self, *a):
        self.retrieves = 0
        self._t = time.monotonic()

    def isOpened(self):
        return True

    def set(self, *a):
        return True

    def get(self, prop):
        return 30.0

    def grab(self):
        self._t += 1 / 30
        d = self._t - time.monotonic()
        if d > 0:
            time.sleep(d)
        return True

    def retrieve(self):
        self.retrieves += 1
        return True, np.zeros((240, 320, 3), np.uint8)

    def release(self):
        pass


def test_camera_skips_conversion_and_rates_match(monkeypatch):
    """間引く間は、取得だけして画像に変換しない。カメラと推論の両方の間引きで、1 つおきにならない（1Hz と 10Hz を保つ）。"""
    import cv2

    monkeypatch.setattr(cv2, "VideoCapture", FakeCap)  # 開発用の OpenCV の取得経路（.venv には OpenCV がある）
    for interval, lo, hi in ((1.0, 2, 4), (0.1, 25, 33), (0.0, 80, 95)):
        cam = inp.Camera(inp.CameraConfig())
        cam.decimate = lambda: interval
        frames = []
        tr = inp.Tracker(cam, inp.TrackerConfig(model_path=MODEL), frames.append, lambda: interval)
        cam.start()
        tr.start()
        time.sleep(3.0)
        tr.stop()
        cam.stop()
        n = len(frames)
        assert lo <= n <= hi, (interval, n)  # 3 秒で 1Hz → 3 回、10Hz → 30 回、毎フレーム → 約 90 回
        if interval > 0:  # 間引く間は、推論しないフレームを変換しない（毎フレームのときは、推論中に来たものも変換する）
            assert cam._cap.retrieves <= n + 3, (interval, cam._cap.retrieves)


def test_skip_and_bgra_to_rgb():
    """AVFoundation の取得経路（本番）: 間引きの判断は OpenCV の経路と同じ。BGRA（行の詰め物つき）から RGB を作る。"""
    assert not inp._skip(None, 10**9, 0, True)
    assert inp._skip(lambda: 1.0, int(0.5e9), 0, True)  # 1Hz の間、前に渡してから 0.5 秒なら変換しない
    assert not inp._skip(lambda: 1.0, int(0.5e9), 0, False)  # まだ 1 枚も無ければ渡す
    assert not inp._skip(lambda: 1.0, int(0.99e9), 0, True)  # ゆとり（DECIMATE_SLACK_S）の分、早めに渡す
    h, w, bpr = 3, 5, 5 * 4 + 12
    src = np.random.default_rng(0).integers(0, 255, (h, bpr), dtype=np.uint8)
    rgb = inp.bgra_to_rgb(src, w, h, bpr)
    assert rgb.shape == (h, w, 3) and rgb.flags.c_contiguous
    assert np.array_equal(rgb, src[:, :w * 4].reshape(h, w, 4)[:, :, 2::-1])


def test_input_imports_without_cv2():
    """配布版（OpenCV なし）: mediapipe が読む cv2 は中身のない代わりで足り、取得経路 opencv は avf に置き換わる。"""
    import subprocess
    import sys

    code = """
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name == "cv2" or name.startswith("cv2."):
            raise ModuleNotFoundError("blocked")
sys.meta_path.insert(0, Block())
import aircontroller.input as inp
try:
    sys.modules["cv2"].imshow
except ImportError:
    print("stub")
inp.AVFCamera = lambda cfg: cfg.backend
print(inp.open_camera(inp.CameraConfig(backend="opencv")))
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         cwd=Path(__file__).resolve().parent.parent)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert lines[0] == "stub" and lines[-1] == "avf" and "avf で取得します" in out.stdout
