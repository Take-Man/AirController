"""AirController のエントリポイント。

  python -m aircontroller run [--record] [--no-output] [--timeout N]   ¥ を押している間だけ操作（段階2まで）
  python -m aircontroller view [--record] [--timeout N]                段階0: カメラ画像にランドマークを描画
  python -m aircontroller keytest [--seconds N]                       キーコードと CGEventTap の確認
  python -m aircontroller camlatency                                  画面の明滅でカメラ側の遅延を測る
  python -m aircontroller replay recordings/xxx.jsonl [--realtime]
  python -m aircontroller stats logs/xxx.csv
  python -m aircontroller analyze recordings/xxx.jsonl
  python -m aircontroller report logs/events-*.jsonl                  イベントログの集計（印、取り消し、誤ドラッグの候補など）
"""
from __future__ import annotations

import argparse
import atexit
import dataclasses
import os
import signal
import sys
import threading
import time
import tomllib
from collections import deque
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent


CALIBRATION = ROOT / "calibration.toml"
HAND = "both"  # 設定の手（[gesture] hand）。校正・学習・表示で、複数の手が映ったときに優先する手。main で設定する


def preferred_hand(hands, hand: str = "both"):
    from .gesture import preferred_hand as ph

    return ph(hands, hand)


def load_config(path: str | None, calibration: bool = True) -> dict:
    """設定ファイルを読む。calibration.toml（calibrate コマンドが書く）があれば、その値で上書きする。"""
    p = Path(path) if path else ROOT / "config.toml"
    with p.open("rb") as fp:
        cfg = tomllib.load(fp)
    if calibration and CALIBRATION.exists():
        with CALIBRATION.open("rb") as fp:
            for section, values in tomllib.load(fp).items():
                if isinstance(values, dict):
                    cfg.setdefault(section, {}).update(values)
    return cfg


def _toml_value(v) -> str:
    import json

    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(float(v)) if isinstance(v, float) else str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def save_calibration(updates: dict[str, dict], note: str) -> None:
    """calibration.toml の値を更新する（他の値は残す）。値は数値だけを想定した簡単な書き出し。"""
    data: dict[str, dict] = {}
    if CALIBRATION.exists():
        with CALIBRATION.open("rb") as fp:
            data = {k: dict(v) for k, v in tomllib.load(fp).items() if isinstance(v, dict)}
    for section, values in updates.items():
        data.setdefault(section, {}).update(values)
    lines = [f"# {note}（{time.strftime('%Y-%m-%d %H:%M')}）。config.toml の値を上書きする。消せば config.toml の値に戻る"]
    for section, values in data.items():
        lines.append(f"\n[{section}]")
        lines += [f"{k} = {_toml_value(v)}" for k, v in values.items()]
    CALIBRATION.write_text("\n".join(lines) + "\n")


UI_STATE = ROOT / "ui_state.json"  # オーバーレイを畳んだかなど、次回の起動に引き継ぐ表示の状態


def _load_ui_state() -> dict:
    import json

    try:
        return json.loads(UI_STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save_ui_state(ui: dict) -> None:
    import json

    try:
        UI_STATE.write_text(json.dumps(ui, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[overlay] 表示の状態を保存できませんでした: {e}")


def _resolve(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else ROOT / q


def _open_camera(cfg: dict):
    from .input import CameraConfig, CameraError, open_camera

    try:
        camera = open_camera(CameraConfig(**cfg["camera"]))
        camera.start()
    except CameraError as e:
        print(f"エラー: {e}", file=sys.stderr)
        print("権限の付与: システム設定 → プライバシーとセキュリティ → カメラ で、"
              "実行元のターミナル（またはアプリ）をオンにして、再起動してください。", file=sys.stderr)
        return None
    size = camera.actual_size
    print(f"カメラ: {size[0]}x{size[1]}  報告fps {camera.reported_fps:.1f}  取得経路 {camera.cfg.backend}")
    return camera


def _tracker_config(cfg: dict):
    from .input import TrackerConfig

    t = dict(cfg["tracker"])
    t["model_path"] = str(_resolve(t["model_path"]))
    return TrackerConfig(**t)


# ---------------------------------------------------------------- 段階1: run
def cmd_run(args, cfg: dict) -> int:
    import Quartz

    from . import cpustat
    from .eventlog import EventLogConfig, EventLogWriter, MarkConfig, MarkRecorder, SessionLogger
    from .gesture import GestureConfig, GestureEngine
    from .input import CameraConfig, CameraError, VisionPipeline
    from .latency import LatencyLog, format_summary, summarize
    from .output import permissions
    from .output.activation import TapError, make_activation
    from .output.mouse import Mouse
    from .recording import Recorder
    from .types import LandmarkFrame

    perms = permissions.check(need_events=True)
    if not all(perms.values()):
        print(permissions.explain_missing(perms), file=sys.stderr)
        return 2

    b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    screen = (float(b.size.width), float(b.size.height))
    gcfg = GestureConfig.from_toml(cfg, screen)
    engine = GestureEngine(gcfg)
    print(f"メインディスプレイ: {screen[0]:.0f}x{screen[1]:.0f}pt")

    # カメラと推論（SPEC 13.22、TASKS-20261025 第3節）: オフの間は推論を power.off_detect_hz（1Hz）に間引き、手が見えたら
    # 確認の窓だけ毎フレームに上げて、パーでオンにする。操作中に手が auto_off_after_s 見えなければ自動でオフ。
    # オフのまま power.idle_stop_s 経つか（0 で無効）、オーバーレイのカメラのボタンを押すとカメラを止める
    from .power import PowerConfig, PowerController

    pw = cfg.get("power", {})
    idle_stop_s = float(pw.get("idle_stop_s", 0.0))
    power_ctl = PowerController(PowerConfig.from_toml(cfg))
    if args.no_auto_off:
        power_ctl.cfg.auto_off_after_s = 0.0  # 負荷の計測（有効・手なし）用
    act_ref: list = [None]
    frame_handler = [lambda f: None]

    def min_interval() -> float:
        a = act_ref[0]
        return 0.0 if a is not None and a.active else power_ctl.min_interval()

    from .gesture import HandCountPolicy

    tcfg = _tracker_config(cfg)
    hand_policy = HandCountPolicy(gcfg.hand, min_score=gcfg.min_score) if tcfg.auto_two_hands else None
    pipe = VisionPipeline(CameraConfig(**cfg["camera"]), tcfg, lambda f: frame_handler[0](f), min_interval,
                          (lambda: hand_policy.num_hands) if hand_policy else None,
                          (lambda: hand_policy.mask_boxes) if hand_policy else None)
    try:
        pipe.start()
    except CameraError as e:
        print(f"エラー: {e}", file=sys.stderr)
        print("権限の付与: システム設定 → プライバシーとセキュリティ → カメラ で、"
              "実行元のターミナル（またはアプリ）をオンにして、再起動してください。", file=sys.stderr)
        return 2
    cam_size = pipe.camera.actual_size
    print(f"カメラ: {cam_size[0]}x{cam_size[1]}  取得経路 {pipe.cam_cfg.backend}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = _resolve(cfg["log"]["dir"]) / f"latency-{stamp}.csv"
    rec_path = _resolve(cfg["log"]["recordings_dir"]) / f"{args.name or stamp}.jsonl"
    log = LatencyLog(log_path)
    recorder = Recorder(rec_path, {"camera": cfg["camera"], "actual_size": list(cam_size),
                                   "tracker": cfg["tracker"], "gesture": dataclasses.asdict(gcfg),
                                   "activation": cfg.get("activation", {}), "no_output": args.no_output}) \
        if args.record else None
    # 常時の軽い記録（TASKS-20261025 1.2）: イベントログと、印の前後のランドマーク
    elcfg = EventLogConfig.from_toml(cfg)
    evlog = EventLogWriter(_resolve(cfg["log"]["dir"]), elcfg, {"camera": cfg["camera"], "no_output": args.no_output},
                           stamp) if elcfg.enabled and not args.no_eventlog else None
    slog = SessionLogger(evlog.put if evlog else (lambda rec: None))

    def mark_saved(n: int, path: Path, frames: int) -> None:
        print(f"[mark] 印 {n} の前後 {frames} フレームを保存しました: {path.relative_to(ROOT)}")

    marks = MarkRecorder(_resolve(cfg["log"]["recordings_dir"]), MarkConfig.from_toml(cfg),
                         {"camera": cfg["camera"], "actual_size": list(cam_size), "tracker": cfg["tracker"],
                          "gesture": dataclasses.asdict(gcfg), "no_output": args.no_output},
                         run_io=evlog.call if evlog else None, on_saved=mark_saved)

    mouse = Mouse()
    pc = cfg.get("peace", {})
    nav_keys = {"back": {"keycode": pc.get("back_keycode", 30), "mods": pc.get("back_mods", ["cmd"])},
                "forward": {"keycode": pc.get("forward_keycode", 42), "mods": pc.get("forward_mods", ["cmd"])}}
    tc = cfg.get("thumbsup", {})
    thumbsup_keys = {d: {"keycode": tc.get(f"{d}_keycode", k), "mods": tc.get(f"{d}_mods", m)}
                 for d, k, m in (("left", 123, ["ctrl", "fn"]), ("right", 124, ["ctrl", "fn"]),
                                 ("up", 126, ["ctrl", "fn", "numpad"]))}
    zc = cfg.get("zoom", {})
    zoom_keys = {d: {"keycode": zc.get(f"{d}_keycode", k), "mods": zc.get(f"{d}_mods", ["cmd"])}
                 for d, k in (("in", 41), ("out", 27))}
    stop = threading.Event()
    output_enabled = threading.Event()
    if not args.no_output:
        output_enabled.set()

    def release_all():
        mouse.release_all()

    def on_emergency():
        output_enabled.clear()
        release_all()
        print("\n[緊急停止] ホットキーを検出しました。終了します。")
        stop.set()

    activation = make_activation(cfg, on_emergency, args.activation)
    act_ref[0] = activation
    # クリックなどには、利用者が実際に押している修飾キーだけを付ける（キー監視で追う）
    mouse.physical_flags = lambda: getattr(activation, "physical_flags", 0)

    def unstick(where: str) -> None:
        """押されていないのに押下中と扱われている修飾キー（⌃ ⌘ など）を解除する。クリックが ⌃クリック（右クリック）に
        なる不具合の対策（SPEC 13.23）。"""
        names = mouse.release_stuck_modifiers()
        if names:
            print(f"[mouse] 押されたままと扱われていた修飾キーを解除しました（{where}）: {', '.join(names)}")
    power = {"last_active": time.monotonic(), "reason": ""}

    def on_activation(active: bool):
        reason = {"key": "キー", "hand_absent": f"手が {power_ctl.cfg.auto_off_after_s:.0f}秒見えない", "palm": "パー",
                  "emergency": "緊急停止", "tap_timeout": "キー監視の停止", "exit": "終了"}.get(activation.last_reason,
                                                                                         activation.last_reason)
        print(f"[activation] {'開始' if active else '終了'}（{reason}）")
        now = time.perf_counter_ns()
        slog.record(now, "activation", on=active, reason=activation.last_reason)
        if active:
            power_ctl.activated(now)
        else:
            power_ctl.deactivated()
        power["last_active"] = time.monotonic()
        unstick("有効化の切り替え")
        if not active:
            release_all()  # 無効化した瞬間に、押下中のボタンを解除する（次のフレームを待たない）
        elif pipe.state in ("stopped", "error"):
            print("[camera] 有効化したので、カメラを起動します")
            pipe.start_async()

    def camera_button():
        """オーバーレイのカメラのボタン: 動いていれば止め（No signal）、止まっていれば起動する。"""
        if pipe.state == "running":
            release_all()
            pipe.stop()
            power["reason"] = "手動"
            print("[camera] カメラを止めました（ボタン）")
        elif pipe.state in ("stopped", "error"):
            power["last_active"] = time.monotonic()
            print("[camera] カメラを起動します（ボタン）")
            pipe.start_async()

    activation.on_change = on_activation
    mark_flash = {"until": 0.0, "n": 0}

    def on_mark():
        n, path = marks.mark()
        mark_flash.update(until=time.monotonic() + 1.5, n=n)
        slog.record(time.perf_counter_ns(), "mark", n=n, path=str(path.relative_to(ROOT)), active=activation.active,
                    mode=engine.state.mode)
        print(f"[mark] 印 {n}（{marks.cfg.post_s:.0f}秒後に前後を保存します）")

    activation.on_mark = on_mark

    # 10 秒ごとの状態（TASKS-20261025 第5節）: 負荷の計測で、状態ごとに CPU・遅延・取りこぼしを分けるため
    hb = {"interval_s": float(cfg.get("eventlog", {}).get("status_s", 10.0)), "last": time.monotonic(),
          "cpu": os.times(), "frames": 0, "dropped": 0, "last_seq": None, "lat": [], "collapsed": None}
    lock = threading.Lock()
    shared = {"lat": deque(maxlen=90), "t_proc": deque(maxlen=60), "clicks": 0, "right_clicks": 0, "drags": 0,
              "frame": None}
    timeout = args.timeout if args.timeout else cfg.get("safety", {}).get("timeout_s", 0)

    def on_frame(frame: LandmarkFrame) -> None:
        # 推論スレッド上のホットパス: ジェスチャー層 → 出力層
        frame = dataclasses.replace(frame, active=activation.active)
        events = engine.update(frame)
        posted = 0
        if output_enabled.is_set():
            for ev in events:
                if ev.kind == "move":
                    mouse.move(ev.data["x"], ev.data["y"])
                    posted += 1
                elif ev.kind == "click":
                    unstick("クリックの前")
                    if ev.data["button"] == "right":
                        mouse.right_click()
                    else:
                        mouse.click()
                    posted += 2
                elif ev.kind == "drag_start":
                    unstick("ドラッグの前")
                    mouse.left_down(ev.data["x"], ev.data["y"])
                    posted += 2
                elif ev.kind == "drag_end":
                    mouse.left_up()
                    posted += 1
                elif ev.kind == "scroll":
                    mouse.scroll(ev.data["dy"])
                    posted += 1
                elif ev.kind == "zoom":
                    key = zoom_keys[ev.data["dir"]]
                    mouse.shortcut(key["keycode"], tuple(key["mods"]))
                    posted += 2
                elif ev.kind == "thumbsup":
                    key = thumbsup_keys[ev.data["dir"]]
                    mouse.shortcut(key["keycode"], tuple(key["mods"]))
                    posted += 2
                elif ev.kind == "nav":
                    key = nav_keys[ev.data["dir"]]
                    mouse.shortcut(key["keycode"], tuple(key["mods"]))
                    posted += 2
                elif ev.kind == "release_all":
                    release_all()
        t_out = time.perf_counter_ns()
        hand = engine.primary_hand(frame)
        slog.observe(frame, events, engine.state.mode, engine.raw_hand(frame))  # 左右ラベルは選ぶ前の手で記録する
        prev_power = power_ctl.state
        action = power_ctl.update(frame.t_capture_ns, frame.active, engine.primary_hand(frame))
        if power_ctl.state != prev_power and "on" not in (prev_power, power_ctl.state):
            slog.record(frame.t_capture_ns, "power", state=power_ctl.state)  # off ⇄ probe（確認の窓）
        if action == "auto_off":
            activation.set(False, "hand_absent")  # on_activation で押下中のボタンを離す
        elif action == "wake":
            activation.set(True, "palm")
        if hand_policy is not None:
            n = hand_policy.update(frame, engine)
            if n:
                slog.record(frame.t_capture_ns, "num_hands", n=n)
        marks.add_frame(frame)
        log.write(frame.seq, frame.t_capture_ns, frame.t_infer_ns, t_out, len(frame.hands),
                  hand.score if hand else 0.0, frame.active, posted)
        if recorder:
            recorder.write(frame)
        hb["frames"] += 1
        if hb["last_seq"] is not None and frame.seq > hb["last_seq"]:
            hb["dropped"] += frame.seq - hb["last_seq"] - 1
        hb["last_seq"] = frame.seq
        hb["lat"].append((t_out - frame.t_capture_ns) / 1e6)
        with lock:
            shared["frame"] = frame
            shared["lat"].append((t_out - frame.t_capture_ns) / 1e6)
            shared["t_proc"].append(frame.t_capture_ns)
        for ev in events:
            if ev.kind == "click":
                shared["right_clicks" if ev.data["button"] == "right" else "clicks"] += 1
            elif ev.kind == "drag_start":
                shared["drags"] += 1
            if ev.kind not in ("move", "scroll", "zoom"):
                print(f"[event] {ev.kind} {ev.data or ''}")

    frame_handler[0] = on_frame

    def cleanup():
        output_enabled.clear()
        release_all()
        try:
            unstick("終了時")
        except Exception:  # noqa: BLE001 — 終了処理は止めない
            pass

    atexit.register(cleanup)
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGHUP, lambda *_: stop.set())

    try:
        activation.start()
    except TapError as e:
        print(f"エラー: {e}", file=sys.stderr)
        pipe.stop()
        return 2

    t_start = time.monotonic()
    err: list[BaseException] = []

    def tick() -> bool:
        # 推論が止まってフレームが来なくなった場合の保険: ボタンを押したまま 1 秒以上フレームが無ければ離す
        last = shared["t_proc"][-1] if shared["t_proc"] else None
        if mouse.is_left_down and last is not None and time.perf_counter_ns() - last > 1_000_000_000:
            print("\n[安全] フレームが届かないため、ボタンを離しました")
            release_all()
        if pipe.tracker_error:
            err.append(pipe.tracker_error)
            return False
        marks.poll()  # カメラが止まっていてフレームが来ないときも、印を締めて保存する
        cpustat.mark("main")
        now_m = time.monotonic()
        if hb["interval_s"] > 0 and now_m - hb["last"] >= hb["interval_s"]:
            status_record(now_m)
        # 有効化がオフのまま idle_stop_s 経ったら、カメラを止める
        if (idle_stop_s > 0 and pipe.state == "running" and not activation.active
                and time.monotonic() - power["last_active"] > idle_stop_s):
            pipe.stop()
            power["reason"] = "自動"
            print(f"[camera] 有効化がオフのまま {idle_stop_s:.0f}秒経ったので、カメラを止めました")
        if timeout and time.monotonic() - t_start > timeout:
            print(f"\n{timeout:.0f}秒経過したので自動終了します。")
            return False
        return not stop.is_set()

    pc_ = power_ctl.cfg
    config_label = ({"right": "右手", "left": "左手"}.get(gcfg.hand, "両手")  # 設定の要約: 使う手・自動オフ・パー
                    + (f"・{pc_.auto_off_after_s:.0f}s" if pc_.auto_off_after_s > 0 else "")
                    + ("・パー" if pc_.wake_enabled else ""))

    def status_record(now_m: float) -> None:
        """10 秒ごとの状態をイベントログに書く（メインスレッド）。"""
        import resource

        c1 = os.times()
        dt = now_m - hb["last"]
        c0 = hb["cpu"]
        lat, hb["lat"] = hb["lat"], []
        frames, dropped = hb["frames"], hb["dropped"]
        hb.update(last=now_m, cpu=c1, frames=0, dropped=0)
        rec = {"active": activation.active, "power": power_ctl.state, "camera": pipe.state,
               "collapsed": hb["collapsed"], "num_hands": hand_policy.num_hands if hand_policy else 1,
               "hand": engine.hand_present, "mode": engine.state.mode, "dt_s": round(dt, 2),
               "cpu_pct": round((c1.user - c0.user + c1.system - c0.system) / dt * 100, 1),
               "threads_cpu_s": {k: round(v, 3) for k, v in cpustat.totals().items()},
               "proc_cpu_s": round(c1.user + c1.system, 3),
               "maxrss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 1),
               "frames": frames, "dropped": dropped}
        if lat:
            rec["lat_p50"], rec["lat_p95"] = (round(float(v), 1) for v in np.percentile(lat, [50, 95]))
        slog.record(time.perf_counter_ns(), "status", **rec)

    def overlay_state():
        with lock:
            lat = list(shared["lat"])
            tp = list(shared["t_proc"])
        st = engine.state
        cam = pipe.camera
        ct = cam.capture_times[-31:] if cam is not None else []
        cam_fps = (len(ct) - 1) / ((ct[-1] - ct[0]) / 1e9) if len(ct) > 1 else 0.0
        proc_fps = (len(tp) - 1) / ((tp[-1] - tp[0]) / 1e9) if len(tp) > 1 else 0.0
        p95 = np.percentile(lat, 95) if lat else float("nan")
        warn = ""
        if not engine.hand_present:
            warn = "手が見えません"
        elif st.score < gcfg.min_score:
            warn = "検出信頼度が低いので停止中"
        if args.no_output:
            warn = (warn + "  " if warn else "") + "出力オフ（記録のみ）"
        if time.monotonic() < mark_flash["until"]:
            warn = f"印 {mark_flash['n']} を付けました" + ("  " + warn if warn else "")
        if pipe.state == "error":
            warn = f"カメラを起動できません: {pipe.start_error or ''}"
        fr = shared["frame"]
        raw = engine.raw_hand(fr) if fr is not None else None
        if raw is not None:  # 左右ラベル（MediaPipe）とスコア。使わない手なら ✕
            used = gcfg.hand == "both" or engine.stable_label == ("Right" if gcfg.hand == "right" else "Left")
            hand_label = f"{'R' if raw.handedness == 'Right' else 'L'} {raw.score:.2f}{'' if used else ' ✕'}"
            if hand_policy is not None and hand_policy.num_hands == 2:
                hand_label += " 2手"  # 両手を探している（設定と違う手だけが映っている）
        else:
            hand_label = "-"
        return {"camera": pipe.state, "camera_reason": power["reason"], "hand_label": hand_label,
                "power": power_ctl.state, "wake_progress": power_ctl.wake_progress, "config_label": config_label,
                "roi": engine.roi(cam_size), "mode": st.mode, "active": activation.active, "peace": st.peace,
                "zoom_level": st.zoom_level, "zoom_progress": st.zoom_progress, "zoom_armed": st.zoom_armed,
                "zoom_fire_level": gcfg.zoom_fire_level,
                "zoom_flash": st.zoom_fire_ns is not None and st.t_ns - st.zoom_fire_ns < 300_000_000,
                "landmarks": st.landmarks if engine.hand_present else None,
                "palm": st.palm if engine.hand_present else None,
                "pinch_progress": st.pinch_progress, "pinch_n": st.pinch_n,
                "status": f"cam {cam_fps:4.1f}fps  proc {proc_fps:4.1f}fps  手 {hand_label}  p95 {p95:4.1f}ms",
                "warning": warn}

    method = args.activation or cfg.get("activation", {}).get("method", "toggle")
    print(("実行中。右 Command（単独で押して離す）で、操作の開始・終了を切り替えます。¥ で印を付けます。"
           if method == "toggle" else "実行中。¥ キーを押している間だけ操作します。")
          + "終了: Ctrl+C、緊急停止: Control+Option+Command+ピリオド"
          + (f"（{timeout:.0f}秒で自動終了）" if timeout else ""))
    try:
        if args.no_overlay:
            while tick():
                time.sleep(0.05)
        else:
            from .output.overlay import Overlay

            ui = _load_ui_state()

            def collapse_changed(collapsed: bool) -> None:
                ui["overlay_collapsed"] = collapsed
                hb["collapsed"] = collapsed
                _save_ui_state(ui)

            hb["collapsed"] = bool(ui.get("overlay_collapsed", False))

            Overlay(overlay_state, tick, on_camera_button=camera_button,
                    collapsed=bool(ui.get("overlay_collapsed", False)), on_collapse_change=collapse_changed).run()
    finally:
        cleanup()
        activation.stop()
        pipe.stop()
        cleanup()
        log.close()
        if recorder:
            recorder.close()
        slog.close(time.perf_counter_ns())
        marks.flush()
        if evlog:
            evlog.close()

    if err:
        raise err[0]
    if gcfg.roi_adapt and (engine.roi_cx, engine.roi_cy, engine.roi_width) != (gcfg.roi_cx, gcfg.roi_cy, gcfg.roi_width):
        save_calibration({"cursor": {"roi_cx": round(engine.roi_cx, 4), "roi_cy": round(engine.roi_cy, 4),
                                     "roi_width": round(engine.roi_width, 4)}}, "操作範囲は run の自動補正が更新")
        print(f"操作範囲を補正しました: 中心 ({gcfg.roi_cx:.3f}, {gcfg.roi_cy:.3f}) 幅 {gcfg.roi_width:.3f} → "
              f"中心 ({engine.roi_cx:.3f}, {engine.roi_cy:.3f}) 幅 {engine.roi_width:.3f}（{CALIBRATION.name} に保存）")
    print(f"\n遅延ログ: {log_path}")
    if evlog:
        print(f"イベントログ: {', '.join(str(p) for p in evlog.paths)}（{evlog.lines} 行）"
              + (f"  上限を超えた古いログを消しました: {len(evlog.deleted)} 本" if evlog.deleted else ""))
    if marks.saved:
        print(f"印: {len(marks.saved)} 個 → " + ", ".join(str(p.relative_to(ROOT)) for p in marks.saved))
    if recorder:
        print(f"ランドマーク記録: {rec_path}（{recorder.count} フレーム）")
    if hasattr(activation, "press_count"):
        print(f"有効化キー: 押下 {activation.press_count} 回（リピート {activation.repeat_count} 回は無視）"
              f"\nクリック {shared['clicks']} 回  右クリック {shared['right_clicks']} 回  ドラッグ {shared['drags']} 回")
    print(format_summary(summarize(log_path)))
    lags = np.array(getattr(pipe.last_camera, "pts_lag_ns", [])) / 1e6
    if len(lags):
        print(f"撮影時刻(PTS)→受け取り: 中央値 {np.median(lags):.1f}ms  p95 {np.percentile(lags, 95):.1f}ms")
    return 0


# ---------------------------------------------------------------- 段階0: view
def cmd_view(args, cfg: dict) -> int:
    import cv2

    from .gesture import GestureConfig, GestureEngine, pinch_metrics
    from .input import Tracker
    from .latency import LatencyLog, format_summary, summarize
    from .recording import Recorder
    from .types import LandmarkFrame

    camera = _open_camera(cfg)
    if camera is None:
        return 2
    size = camera.actual_size
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = _resolve(cfg["log"]["dir"]) / f"latency-{stamp}.csv"
    rec_path = _resolve(cfg["log"]["recordings_dir"]) / f"{args.name or stamp}.jsonl"
    engine = GestureEngine(GestureConfig.from_toml(cfg))
    log = LatencyLog(log_path)
    recorder = Recorder(rec_path, {"camera": cfg["camera"], "actual_size": list(size),
                                   "tracker": cfg["tracker"]}) if args.record else None

    shared_lock = threading.Lock()
    shared: dict = {"frame": None, "lat": deque(maxlen=90), "t_proc": deque(maxlen=60)}

    def on_frame(frame: LandmarkFrame) -> None:
        events = engine.update(frame)
        t_out = time.perf_counter_ns()
        hand = engine.primary_hand(frame)
        log.write(frame.seq, frame.t_capture_ns, frame.t_infer_ns, t_out,
                  len(frame.hands), hand.score if hand else 0.0)
        if recorder:
            recorder.write(frame)
        with shared_lock:
            shared["frame"] = frame
            shared["lat"].append((t_out - frame.t_capture_ns) / 1e6)
            shared["t_proc"].append(frame.t_capture_ns)
        for ev in events:
            print(f"[event] {ev.kind}  seq={frame.seq}")

    tracker = Tracker(camera, _tracker_config(cfg), on_frame)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    t_start = time.monotonic()
    tracker.start()
    print("実行中。終了: ウィンドウで q / Esc、またはターミナルで Ctrl+C"
          + (f"（{args.timeout:.0f}秒で自動終了）" if args.timeout else ""))
    try:
        while not stop.is_set():
            if args.timeout and time.monotonic() - t_start > args.timeout:
                break
            if tracker.error:
                raise tracker.error
            if args.headless:
                time.sleep(0.05)
                continue
            _, _, bgr = camera.latest()
            with shared_lock:
                frame = shared["frame"]
                lat = list(shared["lat"])
                tp = list(shared["t_proc"])
            if bgr is None:
                time.sleep(0.01)
                continue
            img = cv2.flip(bgr, 1)  # 表示だけ鏡像にする（記録・ジェスチャー層は非反転の座標）
            ct = camera.capture_times[-31:]
            cam_fps = (len(ct) - 1) / ((ct[-1] - ct[0]) / 1e9) if len(ct) > 1 else 0.0
            proc_fps = (len(tp) - 1) / ((tp[-1] - tp[0]) / 1e9) if len(tp) > 1 else 0.0
            lines = [f"camera {cam_fps:4.1f}fps  processed {proc_fps:4.1f}fps",
                     f"latency p50 {np.percentile(lat, 50):4.1f}ms  p95 {np.percentile(lat, 95):4.1f}ms"
                     if lat else "latency -",
                     f"hand: {'present' if engine.hand_present else 'none'}"]
            if frame is not None and frame.hands:
                h = preferred_hand(frame.hands, HAND)
                _draw_hand(cv2, img, h)
                pm = pinch_metrics(h)
                lines.append(f"{h.handedness} score {h.score:.2f}  ref {pm['ref_m']*100:.1f}cm")
                lines.append(f"pinch T-I {pm['thumb_index_n']:.2f}  T-M {pm['thumb_middle_n']:.2f} (/ref)")
            for i, s in enumerate(lines):
                cv2.putText(img, s, (12, 30 + 26 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
                cv2.putText(img, s, (12, 30 + 26 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1, cv2.LINE_AA)
            cv2.imshow("AirController view", img)
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break
    finally:
        tracker.stop()
        camera.stop()
        log.close()
        if recorder:
            recorder.close()
        if not args.headless:
            cv2.destroyAllWindows()
            cv2.waitKey(1)

    print(f"\n遅延ログ: {log_path}")
    if recorder:
        print(f"ランドマーク記録: {rec_path}（{recorder.count} フレーム）")
    print(format_summary(summarize(log_path)))
    return 0


_JP_FONT = "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc"


def _put_jp(img, lines: list[str], size: int = 30) -> np.ndarray:
    """画像の上部に日本語の文字を描く（cv2.putText は日本語を描けないので PIL を使う）。"""
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype(_JP_FONT, size)
        small = ImageFont.truetype(_JP_FONT, int(size * 0.7))
    except OSError:
        font = small = ImageFont.load_default()
    pil = Image.fromarray(img[:, :, ::-1])
    draw = ImageDraw.Draw(pil)
    y = 12
    for i, line in enumerate(lines):
        f = font if i == 0 else small
        draw.text((14, y), line, font=f, fill=(255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0))
        y += int((size if i == 0 else size * 0.7) * 1.45)
    return np.asarray(pil)[:, :, ::-1].copy()


def _draw_hand(cv2, img, hand) -> None:
    from mediapipe.tasks.python import vision

    h, w = img.shape[:2]
    pts = [(int((1.0 - x) * w), int(y * h)) for x, y, _ in hand.landmarks]  # 鏡像表示に合わせて x を反転
    for c in vision.HandLandmarksConnections.HAND_CONNECTIONS:
        cv2.line(img, pts[c.start], pts[c.end], (0, 255, 0), 2, cv2.LINE_AA)
    for p in pts:
        cv2.circle(img, p, 4, (0, 0, 255), -1, cv2.LINE_AA)


# ---------------------------------------------------------------- 検証用コマンド
def cmd_calibrate(args, cfg: dict) -> int:
    """校正: 基準長、操作範囲、ピンチの閾値（SPEC 13.8, 13.10）。

    ピンチの閾値は、普段のカーソル操作の姿勢と、摘まんだ状態の距離から、利用者ごとに決める。
    記録は recordings/calib-*.jsonl に残す（active = 摘まんでいる段階）。
    """
    import cv2
    import Quartz

    from .calibration import derive_pinch_thresholds, derive_thumb_fold, neutral_direction
    from .gesture import GestureConfig, finger_straightness, image_pinch_metrics, thumb_fold_metric
    from .peace import direction_angles
    from .input import Tracker
    from .recording import Recorder
    from .types import MIDDLE_TIP, PALM, WRIST, LandmarkFrame

    camera = _open_camera(cfg)
    if camera is None:
        return 2
    iw, ih = camera.actual_size
    b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    sw, sh = float(b.size.width), float(b.size.height)
    gdef = GestureConfig.from_toml(load_config(args.config, calibration=False))
    lock = threading.Lock()
    latest: dict = {"frame": None, "ti": float("nan"), "tm": float("nan")}
    data: dict[str, list] = {"open": [], "range": [], "rest_ti": [], "rest_tm": [], "pinch": [], "rpinch": [],
                             "rest_thumb": [], "peace_thumb": [], "peace_dir": []}
    phase = {"name": "wait"}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    recorder = Recorder(_resolve(cfg["log"]["recordings_dir"]) / f"calib-{stamp}.jsonl",
                        {"camera": cfg["camera"], "actual_size": [iw, ih], "purpose": "calibrate"})

    def on_frame(frame: LandmarkFrame) -> None:
        name = phase["name"]
        recorder.write(dataclasses.replace(frame, active=name in ("pinch", "rpinch")))
        if time.monotonic() < phase.get("settle_until", 0):
            name = "settling"  # 移行中のフレームは集計しない
        h = preferred_hand(frame.hands, HAND) if frame.hands else None
        m = image_pinch_metrics(h, frame.image_size) if h else {}
        with lock:
            latest.update(frame=frame, ti=m.get("thumb_index_n", float("nan")), tm=m.get("thumb_middle_n", float("nan")))
        if h is None:
            return
        if name == "open":
            if all(finger_straightness(h.world, k) > 0.9 for k in (5, 9, 13, 17)):
                data["open"].append(float(np.linalg.norm(h.world[MIDDLE_TIP] - h.world[WRIST])))
        elif name == "range":
            data["range"].append(tuple(h.landmarks[list(PALM), :2].mean(axis=0)))
        elif name == "rest":
            data["rest_ti"].append(m["thumb_index_n"])
            data["rest_tm"].append(m["thumb_middle_n"])
            data["rest_thumb"].append(thumb_fold_metric(h))
        elif name == "peace":
            data["peace_thumb"].append(thumb_fold_metric(h))
            data["peace_dir"].append(direction_angles(h))
        elif name == "pinch":
            data["pinch"].append(m["thumb_index_n"])
        elif name == "rpinch":
            data["rpinch"].append(m["thumb_middle_n"])

    settle = 1.5  # 各段階の最初の 1.5 秒は、前の姿勢から移る途中なので使わない（calib-034725 で動作が約 1.5 秒遅れていた）
    steps = [("wait", 3.0, "準備: 楽な姿勢で手をカメラに映してください")]
    if not args.pinch_only:
        steps += [("open", 4.0 + settle, "手を大きく開いて、手のひらをカメラに向ける"),
                  ("wait", 2.0, "次: 楽な姿勢のまま、届く範囲の端まで手を動かします"),
                  ("range", args.seconds + settle, "上下左右の端まで、ゆっくり手のひらを動かす")]
    steps += [("wait", 2.5, "次: 普段カーソルを動かすときの手の形（人差し指を立て、親指は楽に）"),
              ("rest", 8.0 + settle, "普段のカーソル操作の手の形で、画面のあちこちを指す（摘ままない）"),
              ("wait", 2.5, "次: 親指と人差し指で摘まんだままにします"),
              ("pinch", 6.0 + settle, "親指と人差し指で摘まんだまま、手を動かす・横に向ける"),
              ("wait", 2.5, "次: 親指と中指で摘まんだままにします"),
              ("rpinch", 5.0 + settle, "親指と中指で摘まんだまま、手を動かす・横に向ける"),
              ("wait", 2.5, "次: ピースの形（人差し指と中指を立て、親指を畳む）"),
              ("peace", 5.0 + settle, "ピースの形で、楽に正面を指したまま止める")]
    tracker = Tracker(camera, _tracker_config(cfg), on_frame)
    tracker.start()
    try:
        n_data = sum(1 for st in steps if st[0] != "wait")
        k_data = 0
        for i, (name, dur, msg) in enumerate(steps):
            phase["name"] = name
            phase["settle_until"] = time.monotonic() + (settle if name != "wait" else 0)
            if name != "wait":
                k_data += 1
            print(msg)
            t_end = time.monotonic() + dur
            while time.monotonic() < t_end:
                _, _, bgr = camera.latest()
                if bgr is None:
                    time.sleep(0.01)
                    continue
                img = cv2.flip(bgr, 1)
                with lock:
                    f, ti, tm = latest["frame"], latest["ti"], latest["tm"]
                if f is not None and f.hands:
                    _draw_hand(cv2, img, preferred_hand(f.hands, HAND))
                if name == "range":
                    for x, y in data["range"][-300:]:
                        cv2.circle(img, (int((1 - x) * iw), int(y * ih)), 2, (255, 200, 0), -1)
                left = t_end - time.monotonic()
                head = msg if name == "wait" else f"[{k_data}/{n_data}] {msg}"
                sub = (f"あと {left:3.1f} 秒" + ("（準備中: まだ記録していません）" if time.monotonic() < phase["settle_until"] else "（記録中）")
                       if name != "wait" else f"{left:3.1f} 秒後に始まります")
                img = _put_jp(img, [head, sub, f"親指-人差し指 {ti:.2f}   親指-中指 {tm:.2f}"])
                # 残り時間のバー
                w = int(img.shape[1] * max(0.0, left) / dur)
                cv2.rectangle(img, (0, img.shape[0] - 10), (w, img.shape[0]), (0, 200, 255) if name != "wait" else (160, 160, 160), -1)
                cv2.imshow("AirController calibrate", img)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    print("中断しました")
                    return 1
    finally:
        tracker.stop()
        camera.stop()
        recorder.close()
        cv2.destroyAllWindows()
        cv2.waitKey(1)

    updates: dict[str, dict] = {}
    if not args.pinch_only:
        if len(data["open"]) < 10 or len(data["range"]) < 30:
            print(f"データが足りません（開いた手 {len(data['open'])}、範囲 {len(data['range'])} フレーム）。もう一度実行してください。")
            return 1
        base = float(np.median(data["open"]))
        P = np.array(data["range"])
        x0, x1 = np.percentile(P[:, 0], [5, 95])
        y0, y1 = np.percentile(P[:, 1], [5, 95])
        # 操作範囲は画面と同じアスペクト比。測った範囲（5〜95%）がちょうど収まる幅にする
        w = max(x1 - x0, (y1 - y0) * ih * sw / (iw * sh))
        updates["gesture"] = {"base_length_m": round(base, 4)}
        updates["cursor"] = {"roi_cx": round(float((x0 + x1) / 2), 4), "roi_cy": round(float((y0 + y1) / 2), 4),
                             "roi_width": round(float(w), 4)}
        print(f"基準長 {base * 100:.1f}cm、操作範囲 中心 ({(x0 + x1) / 2:.2f}, {(y0 + y1) / 2:.2f}) 幅 {w:.2f}")
    try:
        left = derive_pinch_thresholds(data["rest_ti"], data["pinch"], gdef.pinch_on, gdef.pinch_off, gdef.drag_off)
        right = derive_pinch_thresholds(data["rest_tm"], data["rpinch"], gdef.right_on, gdef.right_off)
    except ValueError as e:
        print(f"ピンチの校正: {e}。もう一度実行してください。")
        return 1
    print(left.summary("親指-人差し指"))
    print(right.summary("親指-中指"))
    peace_updates: dict = {}
    try:
        thumb = derive_thumb_fold(data["rest_thumb"], data["peace_thumb"], gdef.thumb_fold_on, gdef.thumb_fold_off)
        yaw, pitch = neutral_direction(data["peace_dir"])
        print(f"親指を畳んだ判定: ピース p10 {thumb.pinch_hi:.3f} / 普段の姿勢 p98 {thumb.rest_lo:.3f} → "
              f"{'分離できた' if thumb.separated else '分布が重なっている（既定値を使う）'}  成立 {thumb.on:.3f}  解除 {thumb.off:.3f}")
        print(f"ピースの中立方向: 方位角 {yaw:+.1f}°（右が正）  仰角 {pitch:+.1f}°（上が正）")
        peace_updates = {"peace_neutral_yaw_deg": round(yaw, 2), "peace_neutral_pitch_deg": round(pitch, 2)}
        if thumb.separated:
            peace_updates.update(thumb_fold_on=round(thumb.on, 4), thumb_fold_off=round(thumb.off, 4))
    except ValueError as e:
        print(f"ピースの校正: {e}")
    click = {}
    if left.separated:
        click.update(pinch_on=left.on, pinch_off=left.off, drag_off=left.drag_off)
    if right.separated:
        # 右クリックの「直前に離れていた」条件（right_arm）は、解除の閾値より十分大きくしておく
        click.update(right_on=right.on, right_off=right.off, right_arm=round(max(gdef.right_arm, right.off + 0.08), 4))
    if click:
        updates["click"] = click
    if peace_updates:
        updates["peace"] = peace_updates
    if updates:
        save_calibration(updates, "calibrate が書いた値")
        print(f"保存しました: {CALIBRATION}（消せば config.toml の値に戻ります）")
    print(f"校正の記録: recordings/calib-{stamp}.jsonl")
    return 0


LAB_CANDIDATES = [
    ("thumbsup", "いいね（親指を立て、ほかの指は握る）"),
    ("backtwo", "手の甲をカメラに向け、人差し指と中指を揃えて立てる"),
    ("palm", "手のひらをカメラに向ける（5本とも開く）"),
]
LAB_OTHERS = [
    ("cursor", "人差し指だけを立てて、画面のあちこちを指す"),
    ("pinch", "親指と人差し指で摘まんだまま、手を動かす"),
    ("peace", "ピースで、上下左右に動かす"),
    ("fist", "グーで、手を動かす"),
    ("relaxed", "力を抜いた手（指を軽く曲げる）で、手を動かす"),
]


def _guided_record(cfg: dict, name: str, steps: list[tuple[str | None, float, str]], settle: float = 1.5,
                   purpose: str = "lab", title: str = "AirController lab") -> tuple[Path, list[dict]] | None:
    """画面の指示に従って姿勢を順に記録する。label が None の段階は待ち時間。各段階の最初の settle 秒は集計しない。
    記録（.jsonl）とラベル（.labels.json）を保存し、(記録のパス, 区間のリスト) を返す。"""
    import json

    import cv2

    from .input import Tracker
    from .recording import Recorder
    from .types import LandmarkFrame

    camera = _open_camera(cfg)
    if camera is None:
        return None
    iw, ih = camera.actual_size
    rec_path = _resolve(cfg["log"]["recordings_dir"]) / f"{name}.jsonl"
    recorder = Recorder(rec_path, {"camera": cfg["camera"], "actual_size": [iw, ih], "purpose": purpose})
    lock = threading.Lock()
    latest: dict = {"frame": None}

    def on_frame(frame: LandmarkFrame) -> None:
        recorder.write(frame)
        with lock:
            latest["frame"] = frame

    segments: list[dict] = []
    tracker = Tracker(camera, _tracker_config(cfg), on_frame)
    tracker.start()
    n_data = sum(1 for st in steps if st[0])
    k = 0
    try:
        for label, dur, msg in steps:
            t0 = time.monotonic()
            t_end = t0 + dur
            settle_until = t0 + (settle if label else 0)
            if label:
                k += 1
                segments.append({"label": label, "t_start_ns": int((time.perf_counter() + settle) * 1e9),
                                 "t_end_ns": int((time.perf_counter() + dur) * 1e9)})
            print(msg)
            while time.monotonic() < t_end:
                _, _, bgr = camera.latest()
                if bgr is None:
                    time.sleep(0.01)
                    continue
                img = cv2.flip(bgr, 1)
                with lock:
                    f = latest["frame"]
                if f is not None and f.hands:
                    _draw_hand(cv2, img, preferred_hand(f.hands, HAND))
                left = t_end - time.monotonic()
                if label:
                    sub = f"あと {left:3.1f} 秒" + ("（準備中）" if time.monotonic() < settle_until else "（記録中）")
                    head = f"[{k}/{n_data}] {msg}"
                else:
                    sub, head = f"{left:3.1f} 秒後に始まります", msg
                img = _put_jp(img, [head, sub, "手が見えています" if f is not None and f.hands else "手が見えません"])
                w = int(img.shape[1] * max(0.0, left) / dur)
                cv2.rectangle(img, (0, img.shape[0] - 10), (w, img.shape[0]),
                              (0, 200, 255) if label else (160, 160, 160), -1)
                cv2.imshow(title, img)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    print("中断しました（ここまでの記録は保存します）")
                    if label:  # 途中の段階は、中断した時点までにする
                        segments[-1]["t_end_ns"] = time.perf_counter_ns()
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    finally:
        tracker.stop()
        camera.stop()
        recorder.close()
        cv2.destroyAllWindows()
        cv2.waitKey(1)
        rec_path.with_suffix(".labels.json").write_text(json.dumps({"segments": segments}, ensure_ascii=False, indent=1))
    print(f"記録: {rec_path}\nラベル: {rec_path.with_suffix('.labels.json')}")
    return rec_path, segments


def cmd_lab(args, cfg: dict) -> int:
    """ジェスチャーの候補を試して記録する。各段階にラベルを付け、lab-report で区別のしやすさを比べる。"""
    settle, hold, move = 1.5, 5.0, 2.5
    steps: list[tuple[str | None, float, str]] = [(None, 3.0, "準備: 楽な姿勢で、右手をカメラに映してください")]
    for label, desc in ([] if args.candidates_only else LAB_OTHERS):
        steps += [(None, 2.0, f"次: {desc}"), (label, hold + settle, desc)]
    dirs = [("up", "上"), ("down", "下"), ("left", "左"), ("right", "右")]
    for label, desc in LAB_CANDIDATES:
        steps += [(None, 3.0, f"次の候補: {desc}"), (f"{label}/center", hold + settle, f"{desc}。正面で止める")]
        for d, dj in dirs:
            steps += [(f"{label}/{d}", move + settle, f"{desc}。そのまま{dj}へ動かして止める"),
                      (f"{label}/center", 1.0 + settle, f"{desc}。正面に戻して止める")]
    res = _guided_record(cfg, args.name or f"lab-{time.strftime('%Y%m%d-%H%M%S')}", steps, settle)
    if res is None:
        return 2
    print(f"分析: .venv/bin/python -m aircontroller lab-report {res[0]}")
    return 0


def cmd_learn_thumbsup(args, cfg: dict) -> int:
    """いいねモードを、利用者の動きから学習する（SPEC 13.18）。1回分の上下左右と、間違えやすいグーを記録し、
    姿勢の閾値・方向の閾値・回転の重みを決めて calibration.toml に保存する。--from で既存の記録から学習し直せる。"""
    import json

    from .calibration import learn_thumbsup
    from .recording import iter_frames

    if args.source:
        rec_path = Path(args.source)
        segments = json.loads(rec_path.with_suffix(".labels.json").read_text())["segments"]
    else:
        settle = 1.5
        tu = "いいね（親指を立て、ほかの指は握る）"
        steps: list[tuple[str | None, float, str]] = [
            (None, 3.0, "準備: 楽な姿勢で、右手をカメラに映してください"),
            ("neg_fist_side", 4.0 + settle, "グーで、小指側をカメラに向ける（親指は隠す）。手を少し動かす"),
            (None, 2.0, "次: 普通のグー"),
            ("neg_fist", 4.0 + settle, "普通のグー（親指は握った指の上）。手を少し動かす"),
            (None, 2.0, "次: カーソル操作の形"),
            ("neg_cursor", 4.0 + settle, "人差し指だけを立てて、あちこちを指す"),
            (None, 3.0, f"次: {tu}。これから上・左・右を1回ずつ"),
            ("center", 3.0 + settle, f"{tu}で、正面に止める"),
        ]
        for d, dj in (("up", "上"), ("left", "左"), ("right", "右")):
            steps += [(d, 3.0 + settle, f"いいねのまま、{dj}へ（動かす・ひねる、やりやすい方法で）して止める"),
                      ("center", 2.0 + settle, "いいねのまま、正面に戻して止める")]
        res = _guided_record(cfg, args.name or f"learn-thumbsup-{time.strftime('%Y%m%d-%H%M%S')}", steps, settle,
                             purpose="learn-thumbsup", title="AirController learn-thumbsup")
        if res is None:
            return 2
        rec_path, segments = res
    frames = list(iter_frames(rec_path))
    try:
        result = learn_thumbsup(frames, segments, HAND)
    except ValueError as e:
        print(f"学習できませんでした: {e}")
        return 1
    print(result.report)
    if not args.dry_run:
        save_calibration({"thumbsup": result.params}, "learn-thumbsup が書いた値")
        print(f"保存しました: {CALIBRATION}（[thumbsup] の値。消せば config.toml の値に戻ります）")
    return 0


def cmd_learn_zoom(args, cfg: dict) -> int:
    """ズームモードを、利用者の動きから学習する（SPEC 13.20）。指先の前後の測り方・符号・不感帯を決め、calibration.toml に保存する。"""
    import json

    from .calibration import learn_zoom
    from .recording import iter_frames

    if args.source:
        rec_path = Path(args.source)
        segments = json.loads(rec_path.with_suffix(".labels.json").read_text())["segments"]
    else:
        settle = 1.5
        back = "手の甲をカメラに向け、指を伸ばす"
        steps: list[tuple[str | None, float, str]] = [(None, 3.0, f"準備: 右手で、{back}")]
        for _ in range(2):
            steps += [("neutral", 2.5 + settle, f"{back}。楽な姿勢で止める"),
                      ("in", 2.5 + settle, "手招きのように、指先をカメラから遠ざけて止める（ズームイン）"),
                      ("neutral", 2.0 + settle, f"{back}。元の姿勢に戻して止める"),
                      ("out", 2.5 + settle, "指先をカメラに近づけて止める（ズームアウト）")]
        steps += [("neutral", 2.0 + settle, f"{back}。元の姿勢に戻して止める")]
        res = _guided_record(cfg, args.name or f"learn-zoom-{time.strftime('%Y%m%d-%H%M%S')}", steps, settle,
                             purpose="learn-zoom", title="AirController learn-zoom")
        if res is None:
            return 2
        rec_path, segments = res
    try:
        result = learn_zoom(list(iter_frames(rec_path)), segments, HAND)
    except ValueError as e:
        print(f"学習できませんでした: {e}")
        return 1
    print(result.report)
    if not args.dry_run:
        save_calibration({"zoom": result.params}, "learn-zoom が書いた値")
        print(f"保存しました: {CALIBRATION}（[zoom] の値。消せば config.toml の値に戻ります）")
    return 0


def cmd_lab_report(args, cfg: dict) -> int:
    """lab の記録から、ラベルごとの特徴量と、候補ごとの区別のしやすさを出す。"""
    import itertools
    import json

    from .features import hand_features
    from .recording import iter_frames
    from .types import MIDDLE_MCP, PALM, WRIST

    path = Path(args.path)
    segs = json.loads(path.with_suffix(".labels.json").read_text())["segments"]
    rows: list[tuple[str, dict, tuple[float, float]]] = []
    seen: dict[str, list[int]] = {}
    for f in iter_frames(path):
        lab = next((s["label"] for s in segs if s["t_start_ns"] <= f.t_capture_ns < s["t_end_ns"]), None)
        if lab is None:
            continue
        seen.setdefault(lab.split("/")[0], [0, 0])[0] += 1
        if not f.hands:
            continue
        seen[lab.split("/")[0]][1] += 1
        h = preferred_hand(f.hands, HAND)
        p = h.landmarks[:, :2] * np.asarray(f.image_size)
        ref = float(np.linalg.norm(p[MIDDLE_MCP] - p[WRIST])) or 1.0
        palm = p[list(PALM)].mean(axis=0) / ref
        rows.append((lab, hand_features(h), (float(-palm[0]), float(-palm[1]))))  # 利用者から見た右・上が正
    if not rows:
        print("ラベル付きのフレームがありません")
        return 1
    names = list(rows[0][1].keys())
    groups = sorted({r[0].split("/")[0] for r in rows}, key=lambda g: [x[0] for x in LAB_OTHERS + LAB_CANDIDATES].index(g)
                    if g in [x[0] for x in LAB_OTHERS + LAB_CANDIDATES] else 99)
    print("■ ラベルごとの特徴量（中央値 [p10〜p90]）と検出率")
    for g in groups:
        X = np.array([[r[1][n] for n in names] for r in rows if r[0].split("/")[0] == g])
        tot, det = seen[g]
        print(f"\n{g}: {len(X)} フレーム  手の検出率 {det / tot * 100:.0f}%")
        for j, n in enumerate(names):
            print(f"  {n:15s} {np.median(X[:, j]):+.2f} [{np.percentile(X[:, j], 10):+.2f}〜{np.percentile(X[:, j], 90):+.2f}]")

    print("\n■ 候補ごとの区別のしやすさ（候補のフレーム vs それ以外のすべて）")
    print("  1つの特徴量のしきい値、または 2つの組み合わせで、両方を正しく分けられる割合（バランス精度）の上位")
    for cand, desc in LAB_CANDIDATES:
        pos = np.array([[r[1][n] for n in names] for r in rows if r[0].split("/")[0] == cand])
        neg = np.array([[r[1][n] for n in names] for r in rows if r[0].split("/")[0] != cand])
        if len(pos) < 10 or len(neg) < 10:
            continue

        def best_rule(j):
            best = (0.0, None)
            for thr in np.unique(np.percentile(pos[:, j], np.arange(2, 99, 2))):
                for sign in (1, -1):
                    tp = np.mean(sign * pos[:, j] > sign * thr)
                    tn = np.mean(~(sign * neg[:, j] > sign * thr))
                    if (tp + tn) / 2 > best[0]:
                        best = ((tp + tn) / 2, (sign, thr, tp, tn))
            return best

        singles = sorted(((best_rule(j), names[j]) for j in range(len(names)) if names[j] != "score"),
                         key=lambda x: -x[0][0])
        print(f"\n{cand}（{desc}）")
        for (acc, (sign, thr, tp, tn)), n in singles[:3]:
            print(f"  {n} {'>' if sign > 0 else '<'} {thr:+.2f}: バランス精度 {acc * 100:.1f}%（候補を拾う {tp * 100:.0f}%、"
                  f"ほかを除く {tn * 100:.0f}%）")
        top = [names.index(n) for _, n in singles[:5]]
        rules = {j: best_rule(j)[1] for j in top}
        best_pair = None
        for a, b in itertools.combinations(top, 2):
            (sa, ta, _, _), (sb, tb, _, _) = rules[a], rules[b]
            tp = np.mean((sa * pos[:, a] > sa * ta) & (sb * pos[:, b] > sb * tb))
            tn = np.mean(~((sa * neg[:, a] > sa * ta) & (sb * neg[:, b] > sb * tb)))
            if best_pair is None or (tp + tn) / 2 > best_pair[0]:
                best_pair = ((tp + tn) / 2, names[a], names[b], tp, tn)
        if best_pair:
            acc, na, nb, tp, tn = best_pair
            print(f"  組み合わせ {na} かつ {nb}: バランス精度 {acc * 100:.1f}%（拾う {tp * 100:.0f}%、除く {tn * 100:.0f}%）")
        # 方向: 正面に対する手のひら中心の変位（手の大きさで正規化）
        cen = np.array([r[2] for r in rows if r[0] == f"{cand}/center"])
        if len(cen):
            c0 = np.median(cen, axis=0)
            parts = []
            for d in ("up", "down", "left", "right"):
                P = np.array([r[2] for r in rows if r[0] == f"{cand}/{d}"])
                if len(P):
                    dx, dy = np.median(P, axis=0) - c0
                    parts.append(f"{d} ({dx:+.2f}, {dy:+.2f})")
            print("  方向ごとの変位の中央値（右・上が正）: " + "  ".join(parts))
    return 0


def cmd_modkeys(args, cfg: dict) -> int:
    """修飾キーの状態の確認（SPEC 13.23）。--old: 以前の送り方（修飾キーのフラグだけを付ける）で ⌃→ を送り、
    システムが ⌃ を押したままと見るようになるかを調べる。--new: 今の送り方で ⌃← を送って戻る。"""
    import Quartz

    from .output.mouse import Mouse

    names = {"ctrl": Quartz.kCGEventFlagMaskControl, "opt": Quartz.kCGEventFlagMaskAlternate,
             "cmd": Quartz.kCGEventFlagMaskCommand, "shift": Quartz.kCGEventFlagMaskShift,
             "fn": Quartz.kCGEventFlagMaskSecondaryFn}

    def show(label):
        hid = Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateHIDSystemState)
        ses = Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateCombinedSessionState)
        ev = Quartz.CGEventCreateMouseEvent(Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState),
                                            Quartz.kCGEventLeftMouseDown, (0, 0), Quartz.kCGMouseButtonLeft)
        new = Quartz.CGEventGetFlags(ev)
        fmt = lambda f: ",".join(n for n, m in names.items() if f & m) or "なし"  # noqa: E731
        print(f"{label}: システム(HID) {fmt(hid)} / セッション {fmt(ses)} / 以前の作り方のクリックに付く修飾 {fmt(new)}")

    show("いま")
    mouse = Mouse()
    if args.old:
        print("以前の送り方で ⌃→ を送ります（デスクトップが右へ移ることがあります）")
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(mouse._src, 124, down)
            Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskControl | Quartz.kCGEventFlagMaskSecondaryFn)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(1.0)
        show("以前の送り方の後")
    if args.new:
        print("今の送り方で ⌃← を送ります（デスクトップが左へ移ることがあります）")
        mouse.shortcut(123, ("ctrl", "fn"))
        time.sleep(1.0)
        show("今の送り方の後")
    if args.release:
        released = mouse.release_stuck_modifiers()
        print(f"押されたままと扱われていた修飾キーを解除: {', '.join(released) or 'なし'}")
        time.sleep(0.3)
        show("解除の後")
    return 0


def cmd_keytest(args, cfg: dict) -> int:
    """未確定事項6: キーコードの確認と、CGEventTap（握りつぶしが可能な種類）が作れるか。"""
    import Quartz

    from .output import permissions

    perms = permissions.check(need_events=True, request=False)
    print(f"権限: カメラ={permissions.camera_status()}  アクセシビリティ={'あり' if perms['アクセシビリティ'] else 'なし'}"
          f"  入力監視={'あり' if perms['入力監視'] else 'なし'}")
    mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
            | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged))
    for name, opt in (("listen-only", Quartz.kCGEventTapOptionListenOnly), ("default（握りつぶし可）",
                                                                           Quartz.kCGEventTapOptionDefault)):
        tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap, opt, mask,
                                      lambda *a: a[2], None)
        print(f"CGEventTap {name}: {'作成できた' if tap is not None else '作成できない'}")
        if tap is not None:
            Quartz.CFMachPortInvalidate(tap)
    print(f"{args.seconds:.0f}秒間、押したキーのコードを表示します（入力は握りつぶしません）。¥ と右 Command を押してください。")
    names = {Quartz.kCGEventKeyDown: "down", Quartz.kCGEventKeyUp: "up", Quartz.kCGEventFlagsChanged: "flags"}

    def cb(proxy, etype, event, refcon):
        if etype in names:
            code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            rep = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat)
            flags = Quartz.CGEventGetFlags(event)
            print(f"  {names[etype]:5s} keycode={code:3d}  repeat={rep}  flags=0x{flags:08x}")
        return event

    tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                  Quartz.kCGEventTapOptionListenOnly, mask, cb, None)
    if tap is None:
        print("キー監視ができません（入力監視の権限が必要です）")
        return 2
    src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src, Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(tap, True)
    try:
        Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, args.seconds, False)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_camlatency(args, cfg: dict) -> int:
    """画面全体を黒⇔白に切り替え、顔に当たる光の変化をカメラが捉えるまでの時間を測る。

    測れるのは「画面の切り替え命令 → 画面の発光 → 露光 → 転送 → 取得スレッドが受け取る」の合計。
    ソフトウェア遅延（取得→送出）の前に乗る、カメラ側の遅延の見積もりに使う（表示側の遅延も含む上限値）。
    """
    import cv2

    if args.size:
        w, h = (int(v) for v in args.size.lower().split("x"))
        cfg = {**cfg, "camera": {**cfg["camera"], "width": w, "height": h}}
    if args.backend:
        cfg = {**cfg, "camera": {**cfg["camera"], "backend": args.backend}}
    camera = _open_camera(cfg)
    if camera is None:
        return 2
    samples: list[tuple[int, float]] = []
    done = threading.Event()

    def sampler():
        last = 0
        while not done.is_set():
            got = camera.wait_newer(last, timeout=0.5)
            if got is None:
                continue
            last, t, bgr = got
            samples.append((t, float(cv2.cvtColor(cv2.resize(bgr, (160, 90)), cv2.COLOR_BGR2GRAY).mean())))

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    win = "AirController camlatency"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    black = np.zeros((90, 160, 3), np.uint8)
    white = np.full((90, 160, 3), 255, np.uint8)
    flips: list[tuple[int, bool]] = []
    print("画面に顔を向けて、動かずにいてください（部屋はやや暗めが測りやすい）。約20秒かかります。")
    try:
        t_end = time.monotonic() + 3.0
        while time.monotonic() < t_end:  # 自動露出を落ち着かせる
            cv2.imshow(win, black)
            cv2.waitKey(30)
        for i in range(args.cycles * 2):
            to_white = i % 2 == 0
            cv2.imshow(win, white if to_white else black)
            cv2.waitKey(1)
            flips.append((time.perf_counter_ns(), to_white))
            t_next = time.monotonic() + 0.6
            while time.monotonic() < t_next:
                cv2.waitKey(10)
    finally:
        done.set()
        th.join(1.0)
        cv2.destroyAllWindows()
        cv2.waitKey(1)
        camera.stop()

    ts = np.array([s[0] for s in samples])
    br = np.array([s[1] for s in samples])
    lat = []
    for t_f, to_white in flips:
        before = br[(ts > t_f - 250e6) & (ts <= t_f)]
        after = br[(ts > t_f + 350e6) & (ts <= t_f + 550e6)]
        if len(before) < 3 or len(after) < 3:
            continue
        lo, hi = np.median(before), np.median(after)
        if abs(hi - lo) < 3:  # 明るさの変化が小さすぎる
            continue
        mid = (lo + hi) / 2
        idx = np.where((ts > t_f) & ((br > mid) if hi > lo else (br < mid)))[0]
        if len(idx):
            lat.append((ts[idx[0]] - t_f) / 1e6)
    if not lat:
        print("明るさの変化を検出できませんでした。画面に近づくか、部屋を暗くして再実行してください。")
        return 1
    lat = np.array(lat)
    print(f"有効な切り替え {len(lat)}/{len(flips)} 回  明るさの変化 {abs(np.ptp(br)):.0f}/255")
    print(f"画面切り替え→カメラ取得: 中央値 {np.median(lat):.1f}ms  最小 {lat.min():.1f}ms  最大 {lat.max():.1f}ms")
    print("（表示側の遅延 0〜1フレーム分と、露光途中での切り替えによる最大 33ms のばらつきを含む）")
    lags = np.array(getattr(camera, "pts_lag_ns", [])) / 1e6
    if len(lags):
        print(f"撮影時刻(PTS)→受け取り: 中央値 {np.median(lags):.1f}ms  p95 {np.percentile(lags, 95):.1f}ms"
              f"  （{len(lags)} フレーム）")
    return 0


def cmd_replay(args, cfg: dict) -> int:
    from .gesture import GestureConfig, GestureEngine
    from .recording import read_header, replay

    # 既定は現在の設定ファイルで再評価する。--recorded-config なら記録時の設定を使う
    g = read_header(args.path).get("gesture") if args.recorded_config else None
    if g is not None:
        g = {"pinch_metric": "world", **g}  # pinch_metric 導入前の記録は world で判定していた
        known = {f.name for f in dataclasses.fields(GestureConfig)}
        g = {k: v for k, v in g.items() if k in known}  # 削除した設定（変位ベースなど）を含む記録も読めるように
    engine = GestureEngine(GestureConfig(**g) if g else GestureConfig.from_toml(cfg))
    n = 0
    t0 = None
    for f in replay(args.path, realtime=args.realtime):
        t0 = t0 or f.t_capture_ns
        n += 1
        for ev in engine.update(f):
            if ev.kind != "move" or args.moves:
                print(f"{(ev.t_ns - t0) / 1e6:9.1f}ms  {ev.kind}  {ev.data or ''}")
    print(f"{n} フレームを再生しました")
    return 0


def cmd_stats(args, cfg: dict) -> int:
    from .latency import format_summary, summarize

    print(format_summary(summarize(args.path)))
    return 0


def cmd_analyze(args, cfg: dict) -> int:
    """記録から、取得間隔、world landmarks のスケールと揺れ、（あれば）ラベル付きピンチ距離を集計する。"""
    from .gesture import GestureConfig, finger_straightness, pinch_metrics
    from .recording import iter_frames
    from .types import INDEX_MCP, MIDDLE_TIP, WRIST

    frames = list(iter_frames(args.path))
    t = np.array([f.t_capture_ns for f in frames]) / 1e6
    seq = np.array([f.seq for f in frames])
    dt = np.diff(t)
    print(f"フレーム {len(frames)}  手あり {sum(1 for f in frames if f.hands)}  "
          f"有効化中 {sum(1 for f in frames if f.active)}")
    if len(dt):
        print(f"処理間隔: 中央値 {np.median(dt):.1f}ms  p95 {np.percentile(dt, 95):.1f}ms  max {dt.max():.1f}ms"
              f"  （カメラ {(seq[-1]-seq[0]) / ((t[-1]-t[0]) / 1e3):.1f}fps）")
    base = GestureConfig.from_toml(cfg).base_length_m
    ref, span, ti, tm, score, act, straight = [], [], [], [], [], [], []
    for f in frames:
        if not f.hands:
            continue
        h = preferred_hand(f.hands, HAND)
        pm = pinch_metrics(h)
        ref.append(pm["ref_m"])
        span.append(float(np.linalg.norm(h.world[MIDDLE_TIP] - h.world[WRIST])))
        ti.append(pm["thumb_index_m"])
        tm.append(pm["thumb_middle_m"])
        score.append(h.score)
        act.append(f.active)
        straight.append(finger_straightness(h.world, INDEX_MCP))
    if not ref:
        return 0
    ref, span, ti, tm, act, straight = map(np.array, (ref, span, ti, tm, act, straight))

    def desc(name, a, unit=1.0, u=""):
        d = np.abs(np.diff(a)) * unit
        print(f"{name}: 平均 {a.mean()*unit:.2f}{u}  標準偏差 {a.std()*unit:.2f}{u}  "
              f"範囲 {a.min()*unit:.2f}〜{a.max()*unit:.2f}{u}  フレーム間変化 中央値 {np.median(d):.3f}{u}")

    desc("手首→中指付け根（剛体長）", ref, 100, "cm")
    desc("手首→中指先", span, 100, "cm")
    desc("親指先-人差し指先 / 剛体長", ti / ref)
    desc("親指先-中指先 / 剛体長", tm / ref)
    print(f"信頼度: 平均 {np.mean(score):.3f}  最小 {np.min(score):.3f}")
    if act.any() and (~act).any():
        n = ti / base
        print(f"\nラベル付きピンチ距離（親指先-人差し指先 / 基準長 {base:.3f}m、ラベル = 有効化キーの押下）")
        for lab, m in (("押下中（ピンチしている）", act), ("押していない", ~act)):
            print(f"  {lab}: n={m.sum():4d}  p5 {np.percentile(n[m], 5):.3f}  p50 {np.percentile(n[m], 50):.3f}"
                  f"  p95 {np.percentile(n[m], 95):.3f}")
        print(f"  人差し指の真っすぐさ（押下中）: p5 {np.percentile(straight[act], 5):.2f}"
              f"  p50 {np.percentile(straight[act], 50):.2f}")
    return 0


def cmd_report(args, cfg: dict) -> int:
    """イベントログの集計（TASKS-20261025 1.3）。--from-recording では、ランドマークの記録をジェスチャー層に流して
    イベントログを作ってから集計する（有効化の状態は記録のものを使う。手の不在の自動オフなどは含まない）。"""
    from .report import format_report, load, summarize

    paths = list(args.paths)
    if args.from_recording:
        from .eventlog import EventLogConfig, EventLogWriter, SessionLogger
        from .gesture import GestureConfig, GestureEngine
        from .recording import iter_frames

        out = []
        for rp in paths:
            engine = GestureEngine(GestureConfig.from_toml(cfg))
            w = EventLogWriter(_resolve(cfg["log"]["dir"]) / "replay", EventLogConfig(max_files=0),
                               {"from_recording": rp}, Path(rp).stem, overwrite=True)
            from .eventlog import wall_iso
            from .recording import read_header

            created = time.mktime(time.strptime(read_header(rp)["created"], "%Y-%m-%dT%H:%M:%S"))
            t0 = next(iter_frames(rp)).t_capture_ns
            slog = SessionLogger(w.put, lambda t: wall_iso(int(created * 1e9) + t - t0))
            last, prev_active = 0, False
            for f in iter_frames(rp):
                if f.active != prev_active:
                    slog.record(f.t_capture_ns, "activation", on=f.active, reason="recorded")
                    prev_active = f.active
                slog.observe(f, engine.update(f), engine.state.mode, engine.raw_hand(f))
                last = f.t_capture_ns
            slog.close(last)
            w.close()
            out += [str(p) for p in w.paths]
            print(f"イベントログを作りました: {', '.join(str(p) for p in w.paths)}")
        paths = out
    print(format_report(summarize(load(paths))))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="aircontroller")
    ap.add_argument("--config", help="設定ファイル（既定: config.toml）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="有効化中だけ、カーソル移動・クリック・ドラッグ・右クリックを行う")
    r.add_argument("--record", action="store_true", help="ランドマークと有効化の状態を recordings/ に記録する")
    r.add_argument("--name", help="記録ファイル名（拡張子なし）")
    r.add_argument("--no-output", action="store_true", help="マウスイベントを送出しない（ラベル付き記録用）")
    r.add_argument("--no-overlay", action="store_true", help="オーバーレイを出さない")
    r.add_argument("--timeout", type=float, default=0, help="N 秒後に自動終了（既定は config の safety.timeout_s）")
    r.add_argument("--activation", choices=["toggle", "hold"], help="有効化の方式を上書きする（ラベル付き記録は hold）")
    r.add_argument("--no-eventlog", action="store_true", help="イベントログ（logs/events-*.jsonl）を書かない")
    r.add_argument("--no-auto-off", action="store_true", help="手の不在による自動オフを使わない（負荷の計測用）")
    cb = sub.add_parser("calibrate", help="基準長と操作範囲を校正し、calibration.toml に保存する")
    cb.add_argument("--seconds", type=float, default=10.0, help="操作範囲を測る時間")
    cb.add_argument("--pinch-only", action="store_true", help="ピンチの閾値だけを校正する（基準長と操作範囲は測らない）")
    v = sub.add_parser("view", help="段階0: カメラ画像にランドマークを描画し、遅延ログと記録を行う")
    v.add_argument("--record", action="store_true")
    v.add_argument("--name")
    v.add_argument("--headless", action="store_true")
    v.add_argument("--timeout", type=float, default=0)
    lb = sub.add_parser("lab", help="ジェスチャーの候補を試して、ラベル付きで記録する")
    lb.add_argument("--name", help="記録ファイル名（拡張子なし）")
    lb.add_argument("--candidates-only", action="store_true", help="既存のジェスチャーを飛ばし、候補だけを記録する")
    lt = sub.add_parser("learn-thumbsup", help="いいねモードを、上下左右 1回分の記録から学習する")
    lt.add_argument("--name", help="記録ファイル名（拡張子なし）")
    lt.add_argument("--from", dest="source", help="記録を取らず、既存の記録（.jsonl と .labels.json）から学習する")
    lt.add_argument("--dry-run", action="store_true", help="結果を表示するだけで保存しない")
    lz = sub.add_parser("learn-zoom", help="ズームモードを、手招きの記録から学習する")
    lz.add_argument("--name", help="記録ファイル名（拡張子なし）")
    lz.add_argument("--from", dest="source", help="記録を取らず、既存の記録から学習する")
    lz.add_argument("--dry-run", action="store_true", help="結果を表示するだけで保存しない")
    lr = sub.add_parser("lab-report", help="lab の記録から、候補ごとの区別のしやすさを出す")
    lr.add_argument("path")
    mk = sub.add_parser("modkeys", help="修飾キー（⌃ ⌘ など）が押されたままと扱われていないか確かめる")
    mk.add_argument("--old", action="store_true", help="以前の送り方で ⌃→ を送って、押されたままになるか調べる")
    mk.add_argument("--new", action="store_true", help="今の送り方で ⌃← を送る")
    mk.add_argument("--release", action="store_true", help="押されたままと扱われている修飾キーを解除する")
    k = sub.add_parser("keytest", help="キーコードと CGEventTap の作成可否を確認する")
    k.add_argument("--seconds", type=float, default=15)
    c = sub.add_parser("camlatency", help="画面の明滅で、カメラ側の遅延を測る")
    c.add_argument("--cycles", type=int, default=12)
    c.add_argument("--size", help="解像度を上書きする（例: 640x480）")
    c.add_argument("--backend", choices=["opencv", "avf"], help="取得経路を上書きする")
    p = sub.add_parser("replay", help="記録したランドマークをジェスチャー層に流す")
    p.add_argument("path")
    p.add_argument("--realtime", action="store_true", help="記録時の間隔どおりに再生する")
    p.add_argument("--moves", action="store_true", help="move イベントも表示する")
    p.add_argument("--recorded-config", action="store_true", help="記録時のジェスチャー設定で再生する")
    s = sub.add_parser("stats", help="遅延ログを集計する")
    s.add_argument("path")
    a = sub.add_parser("analyze", help="記録から fps と world landmarks の安定度を集計する")
    a.add_argument("path")
    rp = sub.add_parser("report", help="イベントログ（logs/events-*.jsonl）を集計する")
    rp.add_argument("paths", nargs="+")
    rp.add_argument("--from-recording", action="store_true", help="ランドマークの記録からイベントログを作って集計する")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    global HAND
    HAND = cfg.get("gesture", {}).get("hand", "both")
    return {"run": cmd_run, "calibrate": cmd_calibrate, "lab": cmd_lab, "lab-report": cmd_lab_report,
            "learn-thumbsup": cmd_learn_thumbsup, "learn-zoom": cmd_learn_zoom, "modkeys": cmd_modkeys, "view": cmd_view, "keytest": cmd_keytest, "camlatency": cmd_camlatency,
            "replay": cmd_replay, "stats": cmd_stats, "analyze": cmd_analyze, "report": cmd_report}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
