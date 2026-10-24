"""入力層: カメラ取得と MediaPipe HandLandmarker（ライブストリーム）。

- 取得スレッドはカメラを読み続け、最新の1枚だけを保持する（古いフレームは上書きで捨てる）。
- 推論スレッドは最新フレームを1枚ずつ HandLandmarker に渡す。推論中は次を投入しないので、
  キューは溜まらない。結果はコールバックで LandmarkFrame として外へ渡す。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

import cv2
import mediapipe as mp
import Quartz
import numpy as np
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python import vision

from .types import Hand, LandmarkFrame


class CameraError(RuntimeError):
    pass


@dataclass
class CameraConfig:
    index: int = 0
    width: int = 1280
    height: int = 720
    fps: int = 30
    backend: str = "opencv"  # "opencv" / "avf"（AVFoundation を直接使う。撮影時刻 PTS も取れる）


class Camera:
    """取得スレッド。latest() で最新フレームを取り出す。"""

    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self._cap = cv2.VideoCapture(cfg.index, cv2.CAP_AVFOUNDATION)
        if not self._cap.isOpened():
            raise CameraError("カメラを開けませんでした（カメラの権限が無い可能性があります）")
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        self._cap.set(cv2.CAP_PROP_FPS, cfg.fps)
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._t_ns = 0
        self.seq = 0  # 取得したフレームの通し番号
        self.capture_times: list[int] = []  # 取得間隔の計測用（取得時刻）
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="camera", daemon=True)

    def start(self, first_frame_timeout_s: float = 5.0) -> None:
        self._thread.start()
        with self._cond:
            ok = self._cond.wait_for(lambda: self._frame is not None or self._stop, first_frame_timeout_s)
        if not ok or self._frame is None:
            self.stop()
            raise CameraError("カメラからフレームが届きません（カメラの権限が無い可能性があります）")

    @property
    def actual_size(self) -> tuple[int, int]:
        return (int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))

    @property
    def reported_fps(self) -> float:
        return float(self._cap.get(cv2.CAP_PROP_FPS))

    def _loop(self) -> None:
        while not self._stop:
            ok, frame = self._cap.read()
            t = time.perf_counter_ns()
            if not ok:
                time.sleep(0.005)
                continue
            with self._cond:
                self.seq += 1
                self._frame, self._t_ns = frame, t
                self.capture_times.append(t)
                self._cond.notify_all()

    def wait_newer(self, last_seq: int, timeout: float = 1.0) -> tuple[int, int, np.ndarray] | None:
        """last_seq より新しいフレームを待って (seq, t_capture_ns, BGR画像) を返す。"""
        with self._cond:
            if not self._cond.wait_for(lambda: self.seq > last_seq or self._stop, timeout):
                return None
            if self._stop:
                return None
            return self.seq, self._t_ns, self._frame

    def latest(self) -> tuple[int, int, np.ndarray | None]:
        with self._cond:
            return self.seq, self._t_ns, self._frame

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._cap.release()


_AVF_DELEGATE_CLASS = None


class AVFCamera:
    """AVFoundation を pyobjc で直接使う取得経路。Camera と同じ使い方ができる。

    - alwaysDiscardsLateVideoFrames で、処理が遅れたフレームを OS 側で捨てる（バッファに溜めない）。
    - サンプルの PTS（ホストクロック = time.perf_counter_ns と同じ時計）から、
      撮影されてから受け取るまでの時間 pts_lag_ns を記録する。
    """

    def __init__(self, cfg: CameraConfig):
        import AVFoundation as AVF
        import dispatch
        from Foundation import NSObject

        self.cfg = cfg
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._t_ns = 0
        self.seq = 0
        self.capture_times: list[int] = []
        self.pts_lag_ns: list[int] = []
        self._stop = False
        self._size = (cfg.width, cfg.height)

        devices = AVF.AVCaptureDevice.devicesWithMediaType_(AVF.AVMediaTypeVideo)
        if not devices or cfg.index >= len(devices):
            raise CameraError("カメラが見つかりません")
        device = devices[cfg.index]
        inp, err = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
        if inp is None:
            raise CameraError(f"カメラを開けませんでした（カメラの権限が無い可能性があります）: {err}")
        self._session = AVF.AVCaptureSession.alloc().init()
        preset = {(1280, 720): AVF.AVCaptureSessionPreset1280x720, (640, 480): AVF.AVCaptureSessionPreset640x480,
                  (1920, 1080): AVF.AVCaptureSessionPreset1920x1080}.get((cfg.width, cfg.height))
        if preset and self._session.canSetSessionPreset_(preset):
            self._session.setSessionPreset_(preset)
        self._session.addInput_(inp)
        out = AVF.AVCaptureVideoDataOutput.alloc().init()
        out.setAlwaysDiscardsLateVideoFrames_(True)
        out.setVideoSettings_({Quartz.kCVPixelBufferPixelFormatTypeKey: Quartz.kCVPixelFormatType_32BGRA})
        global _AVF_DELEGATE_CLASS
        if _AVF_DELEGATE_CLASS is None:  # pyobjc のクラスは同名で再定義できないので、初回だけ作る
            class _AVFDelegate(NSObject):
                def captureOutput_didOutputSampleBuffer_fromConnection_(self, output, sample, connection):
                    self.owner._on_sample(sample)

            _AVF_DELEGATE_CLASS = _AVFDelegate
        self._delegate = _AVF_DELEGATE_CLASS.alloc().init()
        self._delegate.owner = self
        self._queue = dispatch.dispatch_queue_create(b"aircontroller.camera", None)
        out.setSampleBufferDelegate_queue_(self._delegate, self._queue)
        self._session.addOutput_(out)

    def _on_sample(self, sample) -> None:
        import CoreMedia

        t = time.perf_counter_ns()
        pts = CoreMedia.CMSampleBufferGetPresentationTimeStamp(sample)
        pb = CoreMedia.CMSampleBufferGetImageBuffer(sample)
        Quartz.CVPixelBufferLockBaseAddress(pb, 1)
        try:
            w, h = Quartz.CVPixelBufferGetWidth(pb), Quartz.CVPixelBufferGetHeight(pb)
            bpr = Quartz.CVPixelBufferGetBytesPerRow(pb)
            buf = Quartz.CVPixelBufferGetBaseAddress(pb).as_buffer(bpr * h)
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpr // 4, 4)[:, :w, :3].copy()
        finally:
            Quartz.CVPixelBufferUnlockBaseAddress(pb, 1)
        with self._cond:
            self.seq += 1
            self._frame, self._t_ns = frame, t
            self._size = (w, h)
            self.capture_times.append(t)
            if pts.timescale:
                self.pts_lag_ns.append(t - int(pts.value * 1_000_000_000 // pts.timescale))
            self._cond.notify_all()

    def start(self, first_frame_timeout_s: float = 5.0) -> None:
        self._session.startRunning()
        with self._cond:
            ok = self._cond.wait_for(lambda: self._frame is not None, first_frame_timeout_s)
        if not ok or self._frame is None:
            self.stop()
            raise CameraError("カメラからフレームが届きません（カメラの権限が無い可能性があります）")

    @property
    def actual_size(self) -> tuple[int, int]:
        return self._size

    @property
    def reported_fps(self) -> float:
        return float(self.cfg.fps)

    wait_newer = Camera.wait_newer
    latest = Camera.latest

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        if self._session.isRunning():
            self._session.stopRunning()


def open_camera(cfg: CameraConfig):
    return AVFCamera(cfg) if cfg.backend == "avf" else Camera(cfg)


@dataclass
class TrackerConfig:
    model_path: str = "models/hand_landmarker.task"
    num_hands: int = 1
    min_detection_confidence: float = 0.5
    min_presence_confidence: float = 0.5
    min_tracking_confidence: float = 0.5
    # 設定と違う手だけが映っている間だけ、2 つの手を探す（TASKS-20261025 2.5 の作者の要望）。num_hands = 2 を常に使うと
    # 推論が p50 13 → 26ms、p95 15 → 44ms に増えた（2026-10-25 の実測）ので、普段は num_hands の推論器を使い、
    # num_hands_fn が 2 を返す間だけ 2 つの手を探す推論器に切り替える
    auto_two_hands: bool = False


class Tracker:
    """推論スレッド。フレームごとに on_frame(LandmarkFrame) を呼ぶ（呼び出しは推論スレッド上）。"""

    def __init__(self, camera: Camera, cfg: TrackerConfig, on_frame: Callable[[LandmarkFrame], None],
                 min_interval: Callable[[], float] | None = None, num_hands_fn: Callable[[], int] | None = None,
                 mask_fn: Callable[[], list] | None = None):
        """min_interval: 推論の最短間隔（秒）を返す関数。待機中に処理回数を落とすために使う（0 なら毎フレーム）。
        num_hands_fn: cfg.auto_two_hands のとき、探す手の数（1 か 2）をフレームごとに返す関数。
        mask_fn: 推論の前に画像を黒く塗る範囲 [(x0, y0, x1, y1)]（画像座標 0..1）を返す関数（gesture.HandCountPolicy）。"""
        self.camera = camera
        self.cfg = cfg
        self.on_frame = on_frame
        self.min_interval = min_interval
        self._pending: dict[int, tuple[int, tuple[int, int]]] = {}  # timestamp_ms -> (seq, t_capture_ns, size)
        self._done = threading.Event()
        self._stop = False
        self.error: BaseException | None = None
        self.num_hands_fn = num_hands_fn
        self.mask_fn = mask_fn
        self.current_num_hands = cfg.num_hands
        counts = {cfg.num_hands} | ({2} if cfg.auto_two_hands else set())
        self._landmarkers = {n: self._create(n) for n in counts}  # 切り替えで止まらないよう、起動時に両方作る
        self._landmarker = self._landmarkers[cfg.num_hands]
        self._thread = threading.Thread(target=self._loop, name="inference", daemon=True)

    def _create(self, num_hands: int):
        c = self.cfg
        options = vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=c.model_path),
            running_mode=vision.RunningMode.LIVE_STREAM,
            num_hands=num_hands,
            min_hand_detection_confidence=c.min_detection_confidence,
            min_hand_presence_confidence=c.min_presence_confidence,
            min_tracking_confidence=c.min_tracking_confidence,
            result_callback=self._on_result,
        )
        return vision.HandLandmarker.create_from_options(options)

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        last_seq = 0
        last_ts_ms = -1
        last_proc = 0
        try:
            while not self._stop:
                got = self.camera.wait_newer(last_seq)
                if got is None:
                    continue
                seq, t_cap, bgr = got
                last_seq = seq
                interval = self.min_interval() if self.min_interval else 0.0
                if interval > 0 and (t_cap - last_proc) / 1e9 < interval - 0.005:
                    continue  # 待機中は間引く（取得は続けるので、有効化した次のフレームから毎フレームに戻る）
                last_proc = t_cap
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)  # 新しい配列（取得スレッドの画像は書き換えない）
                boxes = self.mask_fn() if self.mask_fn is not None else None
                if boxes:
                    h_, w_ = rgb.shape[:2]
                    for x0, y0, x1, y1 in boxes:
                        rgb[max(0, int(y0 * h_)):max(0, int(y1 * h_)), max(0, int(x0 * w_)):max(0, int(x1 * w_))] = 0
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                ts_ms = max(t_cap // 1_000_000, last_ts_ms + 1)  # 単調増加が必須
                last_ts_ms = ts_ms
                self._pending[ts_ms] = (seq, t_cap, (bgr.shape[1], bgr.shape[0]))
                for k in [k for k in self._pending if k < ts_ms - 2000]:  # 結果が返らなかった分の掃除
                    del self._pending[k]
                if self.num_hands_fn is not None and len(self._landmarkers) > 1:
                    n = self.num_hands_fn()
                    if n in self._landmarkers:
                        self.current_num_hands = n
                        self._landmarker = self._landmarkers[n]
                self._done.clear()
                self._landmarker.detect_async(image, ts_ms)
                # 結果が返るまで次を投入しない（推論待ちの間に来たフレームは捨てられ、常に最新が使われる）
                self._done.wait(timeout=1.0)
        except BaseException as e:  # noqa: BLE001 — メインスレッドに伝える
            self.error = e

    def _on_result(self, result: vision.HandLandmarkerResult, image: mp.Image, ts_ms: int) -> None:
        t_infer = time.perf_counter_ns()
        try:
            seq, t_cap, size = self._pending.pop(ts_ms)
            hands = []
            for handed, lms, wlms in zip(result.handedness, result.hand_landmarks, result.hand_world_landmarks):
                hands.append(Hand(
                    handedness=handed[0].category_name,
                    score=float(handed[0].score),
                    landmarks=np.array([[p.x, p.y, p.z] for p in lms], dtype=np.float64),
                    world=np.array([[p.x, p.y, p.z] for p in wlms], dtype=np.float64),
                ))
            self.on_frame(LandmarkFrame(seq=seq, t_capture_ns=t_cap, t_infer_ns=t_infer,
                                        image_size=size, hands=tuple(hands)))
        except BaseException as e:  # noqa: BLE001
            self.error = e
        finally:
            self._done.set()

    def stop(self) -> None:
        self._stop = True
        self._done.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        for lm in self._landmarkers.values():
            lm.close()


class VisionPipeline:
    """カメラと推論スレッドをまとめて、実行中に止めたり起動し直したりする（SPEC 13.22）。

    state: "running" / "starting" / "stopped" / "error"。起動はカメラが開くまで 1〜2秒かかるので、start_async を使う。
    """

    def __init__(self, cam_cfg: CameraConfig, tracker_cfg: TrackerConfig, on_frame: Callable[[LandmarkFrame], None],
                 min_interval: Callable[[], float] | None = None, num_hands_fn: Callable[[], int] | None = None,
                 mask_fn: Callable[[], list] | None = None):
        self.cam_cfg, self.tracker_cfg, self.on_frame, self.min_interval = cam_cfg, tracker_cfg, on_frame, min_interval
        self.num_hands_fn = num_hands_fn
        self.mask_fn = mask_fn
        self.mask_fn = mask_fn
        self.camera = None
        self.last_camera = None  # 最後に動かしたカメラ（止めた後も、終了時の集計 pts_lag_ns などに使う）
        self.tracker: Tracker | None = None
        self.state = "stopped"
        self.error: BaseException | None = None
        self.start_error: str | None = None
        self._cancel = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """起動して最初のフレームを待つ（呼び出し元をブロックする）。失敗したら CameraError。"""
        with self._lock:
            if self.state in ("running", "starting"):
                return
            self.state = "starting"
            self._cancel = False
        try:
            camera = open_camera(self.cam_cfg)
            camera.start()
            tracker = Tracker(camera, self.tracker_cfg, self.on_frame, self.min_interval, self.num_hands_fn,
                              self.mask_fn)
            tracker.start()
        except BaseException as e:
            with self._lock:
                self.state = "error"
                self.start_error = str(e)
            raise
        with self._lock:
            self.camera, self.tracker, self.state, self.start_error = camera, tracker, "running", None
            self.last_camera = camera
            cancel = self._cancel
        if cancel:  # 起動中に止める指示が来ていた
            self.stop()

    def start_async(self) -> None:
        if self.state in ("running", "starting"):
            return

        def run():
            try:
                self.start()
            except BaseException:  # noqa: BLE001 — state と start_error に残す
                pass

        threading.Thread(target=run, name="vision-start", daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            tracker, camera = self.tracker, self.camera
            self.tracker = self.camera = None
            if self.state == "starting":
                self._cancel = True
            else:
                self.state = "stopped"
        if tracker:
            if tracker.error:
                self.error = tracker.error
            tracker.stop()
        if camera:
            camera.stop()

    @property
    def tracker_error(self) -> BaseException | None:
        t = self.tracker
        return t.error if t else None
