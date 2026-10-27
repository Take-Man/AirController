"""run に内蔵する小さなサーバー（設定画面のライブのプレビュー、SPEC 13.47）。標準ライブラリだけ。

- GET /stream（SSE）: 手の骨格（21 点）、今のモード、ピンチ距離・親指の畳み・指の真っすぐさなどの今の値を、約 15Hz で送る。
  つながっている間は「設定モード」: 出力（マウス・キー）を止める（/mode で「操作に反映する」にできる）。設定モードに入るとき・抜けるとき、
  進行中のピンチ・ドラッグ・モードを打ち切り、押下中のボタンを離す。抜けると、画面から送られた変更中の値を捨て、保存済みの設定に戻す。
- POST /config {"values": {"節.キー": 値}}: 変更中の値（保存済みの設定に重ねる）。安全な瞬間（ドラッグ・ピンチ・モードの途中でない）に差し替える。
- POST /mode {"output": bool}、POST /reload（保存済みの設定を読み直す。設定画面で確定したとき）、GET /stats（負荷の計測用）。
設定画面のサーバーだけが使う（ブラウザは直接つながない）: 127.0.0.1、Host の確認、起動時のトークン（live.json に書く。所有者だけ読める）。
フレームの処理（推論スレッド）では、つながっている画面が無ければ何もしない。
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

from .params import BY_ID

STREAM_HZ = 15.0


def _r(v, n=3):
    return None if v is None or (isinstance(v, float) and v != v) else round(float(v), n)


class LiveHub:
    def __init__(self, root: Path, load_base, screen: tuple[float, float]):
        """load_base(): 保存済みの設定（config.toml < calibration.toml < auto.toml）を読んだ dict。"""
        self.root = Path(root)
        self.path = self.root / "live.json"
        self.load_base = load_base
        self.screen = screen
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.clients = 0
        self.output = False  # 設定モード中も出力を出すか
        self._mode_was = False  # フレームの処理で見た、設定モードかどうか
        self._interrupt = False  # 「操作に反映する」を切り替えた: 進行中の操作を打ち切る（ボタンの状態を出力と合わせる）
        self._overrides: dict[str, object] = {}
        self._pending = None  # 差し替える GestureConfig（安全な瞬間を待つ）
        self._snap: dict | None = None
        self._snap_n = 0
        self._last_pub = 0.0
        self.applied: list[str] = []
        self.lat = deque(maxlen=4000)  # (時刻, 遅延 ms)
        self.httpd: ThreadingHTTPServer | None = None
        self.on_connect = None  # 画面がつながったとき（run がカメラを起動する）

    # ------------------------------------------------------------ 状態
    @property
    def settings_mode(self) -> bool:
        return self.clients > 0

    def has_overrides(self) -> bool:
        with self.lock:
            return bool(self._overrides)

    def suppress_output(self) -> bool:
        return self.settings_mode and not self.output

    def _config_from(self, overrides: dict):
        from .gesture import GestureConfig

        cfg = self.load_base()
        for pid, v in overrides.items():
            p = BY_ID.get(pid)
            if p is None or p.cls not in "AB":
                raise ValueError(f"変更できない項目: {pid}")
            cfg.setdefault(p.section, {})[p.key] = v
        return GestureConfig.from_toml(cfg, self.screen)

    def set_overrides(self, overrides: dict) -> None:
        cfg = self._config_from(overrides)  # 型が合わなければここで落ちる（差し替えない）
        with self.lock:
            self._overrides = dict(overrides)
            self._pending = cfg

    def reload(self) -> None:
        """保存済みの設定が変わった（設定画面で確定した）: 読み直し、変更中の値を重ねて差し替える。"""
        self.set_overrides(self._overrides)

    # ------------------------------------------------------------ フレームの処理（推論スレッド）
    def before_update(self, engine, t_ns: int) -> list:
        """設定モードの出入りと、設定の差し替え。出したイベント（打ち切り）を返す。"""
        events = []
        mode = self.settings_mode
        if mode != self._mode_was or self._interrupt:
            self._interrupt = False
            self._mode_was = mode
            events += engine.interrupt(t_ns)
            if not mode:  # 画面を閉じた: 変更中の値を捨てて、保存済みに戻す
                try:
                    cfg = self._config_from({})
                    with self.lock:
                        self._overrides, self._pending = {}, cfg
                except Exception as e:  # noqa: BLE001  設定ファイルが読めないときは、今の設定のまま
                    print(f"[live] 保存済みの設定を読めませんでした: {e}")
        engine.adapt_paused = mode  # 設定モードの間は、操作範囲の自動補正を止める（画面での変更とぶつからないように）
        if self._pending is not None and engine.safe_to_reconfigure():
            with self.lock:
                cfg, self._pending = self._pending, None
            self.applied = engine.apply_config(cfg)
            if self.applied:
                print(f"[live] 設定を差し替えました: {', '.join(self.applied[:6])}" + (" …" if len(self.applied) > 6 else ""))
        return events

    def note_latency(self, ms: float) -> None:
        self.lat.append((time.monotonic(), ms))

    def publish(self, engine, frame, active: bool) -> None:
        if not self.clients:
            return
        now = time.monotonic()
        if now - self._last_pub < 1.0 / STREAM_HZ:
            return
        self._last_pub = now
        snap = snapshot(engine, frame, active)
        snap.update(settings_mode=True, output=self.output, pending=self._pending is not None,
                    overrides=len(self._overrides))
        with self.cond:
            self._snap, self._snap_n = snap, self._snap_n + 1
            self.cond.notify_all()

    # ------------------------------------------------------------ サーバー
    def start(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self))
        self.httpd.daemon_threads = True
        port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="live", daemon=True).start()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fp:
            json.dump({"port": port, "token": self.token, "pid": os.getpid()}, fp)

    def close(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        try:
            if json.loads(self.path.read_text()).get("pid") == os.getpid():
                self.path.unlink()
        except (OSError, ValueError):
            pass

    def stats(self, window_s: float) -> dict:
        t = os.times()
        since = time.monotonic() - window_s
        lat = np.array([ms for ts, ms in list(self.lat) if ts >= since])
        return {"cpu_s": t.user + t.system, "wall": time.monotonic(), "frames": int(len(lat)), "clients": self.clients,
                "lat_p50": _r(np.percentile(lat, 50)) if len(lat) else None,
                "lat_p95": _r(np.percentile(lat, 95)) if len(lat) else None}


def snapshot(engine, frame, active: bool) -> dict:
    """画面のメーターと図に出す今の値。"""
    from .gesture import (finger_straightness, image_pinch_metrics, palm_facing, thumb_far, thumb_fold_metric,
                          thumb_up)
    from .types import INDEX_MCP, MIDDLE_MCP, PINKY_MCP, RING_MCP

    st = engine.state
    c = engine.cfg
    out = {"t": frame.t_capture_ns, "active": bool(active), "mode": st.mode, "hand": False,
           "image_size": list(frame.image_size), "roi": [_r(engine.roi_cx), _r(engine.roi_cy), _r(engine.roi_width)],
           "cfg": {k: getattr(c, k) for k in ("index_up_enter", "index_up_exit", "peace_exit_straight", "ring_down_max",
                                               "fist_enter", "fist_exit", "zoom_back_min", "thumbsup_index_max",
                                               "thumbsup_up_min")}}
    hand = engine.primary_hand(frame)
    if hand is None:
        return out
    w = hand.world
    m = image_pinch_metrics(hand, frame.image_size)
    out.update(hand=True, label=hand.handedness, side=engine.side, score=_r(hand.score),
               landmarks=[[_r(x), _r(y)] for x, y in hand.landmarks[:, :2]],
               palm=[_r(v) for v in (st.palm or (None, None))],
               ti=_r(m["thumb_index_n"]), tm=_r(m["thumb_middle_n"]), thumb_fold=_r(thumb_fold_metric(hand)),
               straight=[_r(finger_straightness(w, k)) for k in (INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP)],
               palm_facing=_r(palm_facing(hand, engine.side)), thumb_up=_r(thumb_up(hand)), thumb_far=_r(thumb_far(hand, frame.image_size)),
               pinch_progress=_r(st.pinch_progress))
    if st.mode == "peace":
        out["peace"] = [_r(v) for v in engine._peace.view.raw]
    elif st.mode == "thumbsup":
        out["thumbsup"] = [_r(v) for v in engine._thumbsup.view.raw]
    elif st.mode == "zoom":
        out["zoom"] = _r(engine._zoom.value, 4)
    return out


def make_handler(hub: LiveHub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _ok(self) -> bool:
            port = hub.httpd.server_address[1]
            if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                self._send(403, {"error": "Host"})
                return False
            if not hmac.compare_digest(self.headers.get("X-Token", "").encode(), hub.token.encode()):
                self._send(403, {"error": "token"})
                return False
            return True

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._ok():
                return
            u = urlparse(self.path)
            if u.path == "/stats":
                q = parse_qs(u.query)
                self._send(200, hub.stats(float(q.get("window", ["10"])[0])))
                return
            if u.path != "/stream":
                self._send(404, {"error": "ありません"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with hub.lock:
                hub.clients += 1
            print(f"[live] 設定画面がつながりました（{hub.clients}）。設定モード: 出力を止めます")
            if hub.on_connect:
                hub.on_connect()
            n = -1
            try:
                while True:
                    with hub.cond:
                        hub.cond.wait_for(lambda: hub._snap_n != n, timeout=1.0)
                        snap, n2 = hub._snap, hub._snap_n
                    if n2 != n and snap is not None:
                        n = n2
                        self.wfile.write(b"data: " + json.dumps(snap, separators=(",", ":")).encode() + b"\n\n")
                    else:
                        self.wfile.write(b": keepalive\n\n")  # 画面が閉じたら、ここで書けなくなって抜ける
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with hub.lock:
                    hub.clients -= 1
                print(f"[live] 設定画面が離れました（{hub.clients}）")

        def do_POST(self):
            if not self._ok():
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(n, 1_000_000)) or b"{}")
                path = urlparse(self.path).path
                if path == "/config":
                    hub.set_overrides(dict(body.get("values", {})))
                elif path == "/mode":
                    out = bool(body.get("output"))
                    if out != hub.output:
                        hub.output, hub._interrupt = out, True
                elif path == "/reload":
                    hub.reload()
                else:
                    self._send(404, {"error": "ありません"})
                    return
                self._send(200, {"ok": True})
            except (ValueError, TypeError) as e:
                self._send(400, {"error": str(e)})
    return Handler
