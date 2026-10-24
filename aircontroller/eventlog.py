"""日常利用で常に有効にしておく軽い記録（TASKS-20261025 第1節）。

- EventLogWriter: イベントログ（JSON Lines、logs/events-YYYYmmdd-HHMMSS.jsonl）を別スレッドで書く。
  推論スレッド（ホットパス）は dict をキューに積むだけ。サイズの上限で新しいファイルに移り、世代数の上限を超えた
  古い events-*.jsonl を消す。印の記録（mark-*.jsonl）の書き出しも同じスレッドで行う。
- SessionLogger: フレームごとの (フレーム, イベント, ジェスチャーの状態) から、モードの遷移、発火したイベント、
  手の獲得・見失い、左右ラベルの変化をログの行にする。時刻はフレームの t_capture_ns だけを使うので、
  記録を流しても同じ行が出る（壁時計の列を除く）。
- MarkRecorder: ランドマークのリングバッファ（直近 ring_s 秒）を持ち、印が付いたら、その前 pre_s 秒と後 post_s 秒を
  recordings/mark-YYYYmmdd-HHMMSS-N.jsonl に保存する（recording.Recorder と同じ形式。再現テストにそのまま使える）。

行の共通の列: t_ns（単調時計。フレームの取得時刻と同じ time.perf_counter_ns）、wall（壁時計、ISO 8601、ミリ秒）、type。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import cpustat
from .recording import FORMAT, VERSION, frame_to_dict
from .types import Event, LandmarkFrame

LOG_FORMAT = "aircontroller-events"
LOG_VERSION = 1

# モードのうち、何かを出力するはずのもの。出力なしで抜けたら「取り消された」と数える
OUTPUT_MODES = {"pinch": ("click", "drag_start"), "right": ("click",), "peace": ("scroll", "nav"),
                "thumbsup": ("thumbsup",), "zoom": ("zoom",)}
# 1 件ずつ行にするイベント（move と scroll は多すぎるので行にしない。scroll はモードを抜けたときに合計を書く）
LOGGED_EVENTS = {"click", "drag_start", "drag_end", "pinch_start", "pinch_cancel", "nav", "thumbsup", "zoom"}


def wall_iso(ns: int | None = None) -> str:
    ns = time.time_ns() if ns is None else ns
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ns / 1e9)) + f".{ns // 1_000_000 % 1000:03d}"


@dataclass
class EventLogConfig:
    enabled: bool = True
    max_bytes: int = 20_000_000  # 1ファイルの上限。超えたら新しいファイルに移る
    max_files: int = 30  # events-*.jsonl をこの数まで残す（超えたら古いものから消す）
    flush_s: float = 2.0  # これだけ経ったらファイルに書き出す（落ちたときに失う量を抑える）

    @classmethod
    def from_toml(cls, cfg: dict) -> "EventLogConfig":
        e = cfg.get("eventlog", {})
        return cls(**{k: v for k, v in e.items() if k in cls.__dataclass_fields__})


class EventLogWriter:
    """別スレッドでイベントログを書く。put() は推論スレッドから呼んでよい（キューに積むだけ）。"""

    def __init__(self, log_dir: str | Path, cfg: EventLogConfig | None = None, header: dict | None = None,
                 stamp: str | None = None, overwrite: bool = False):
        self.cfg = cfg or EventLogConfig()
        self.overwrite = overwrite  # 同じ名前のファイルがあれば上書きする（記録から作り直すとき）
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.header = header or {}
        self.paths: list[Path] = []
        self.lines = 0
        self.deleted: list[Path] = []
        self._q: queue.Queue = queue.Queue()
        self._fp = None
        self._bytes = 0
        self._open(stamp)
        self._thread = threading.Thread(target=self._run, name="eventlog", daemon=True)
        self._thread.start()

    @property
    def path(self) -> Path:
        return self.paths[-1]

    def _open(self, stamp: str | None = None) -> None:
        stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
        p = self.dir / f"events-{stamp}.jsonl"
        n = 1
        while p.exists() and not (self.overwrite and not self.paths):  # 同じ秒に移ったとき
            n += 1
            p = self.dir / f"events-{stamp}-{n}.jsonl"
        if self._fp:
            self._fp.close()
        self._fp = p.open("w")
        self.paths.append(p)
        head = {"format": LOG_FORMAT, "version": LOG_VERSION, "created": wall_iso(), "part": len(self.paths)}
        head.update(self.header)
        line = json.dumps(head, ensure_ascii=False) + "\n"
        self._fp.write(line)
        self._bytes = len(line.encode())
        self._prune()

    def _prune(self) -> None:
        if self.cfg.max_files <= 0:
            return
        files = sorted(self.dir.glob("events-*.jsonl"), key=lambda p: p.stat().st_mtime)
        for p in files[:max(0, len(files) - self.cfg.max_files)]:
            if p in self.paths[-1:]:
                continue
            try:
                p.unlink()
                self.deleted.append(p)
            except OSError:
                pass

    def put(self, rec: dict) -> None:
        self._q.put(rec)

    def call(self, fn: Callable[[], None]) -> None:
        """重い処理（印の記録の書き出しなど）をこのスレッドで行う。"""
        self._q.put(fn)

    def _write(self, rec: dict) -> None:
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        n = len(line.encode())
        if self.cfg.max_bytes > 0 and self._bytes + n > self.cfg.max_bytes:
            self._open()
        self._fp.write(line)
        self._bytes += n
        self.lines += 1

    def _run(self) -> None:
        last_flush = time.monotonic()
        while True:
            try:
                item = self._q.get(timeout=self.cfg.flush_s)
            except queue.Empty:
                item = ...
            if item is None:
                break
            try:
                if callable(item):
                    item()
                elif item is not ...:
                    self._write(item)
            except Exception as e:  # noqa: BLE001 — 記録の失敗で本体を止めない
                print(f"[eventlog] 書き込みに失敗しました: {e}")
            cpustat.mark("eventlog")
            if time.monotonic() - last_flush >= self.cfg.flush_s:
                self._fp.flush()
                last_flush = time.monotonic()
        self._fp.flush()

    def close(self) -> None:
        if self._thread.is_alive():
            self._q.put(None)
            self._thread.join(timeout=5.0)
        if self._fp and not self._fp.closed:
            self._fp.close()


class SessionLogger:
    """フレームごとの結果を、イベントログの行にする。emit(rec) に dict を渡す（EventLogWriter.put など）。

    モードの遷移は有効化中だけ記録する（待機中もジェスチャー層は動いているが、出力しないので数えない）。
    手の獲得・見失いと左右ラベルの変化は、待機中も記録する（第3節の自動オフ・オンの検証に使う）。"""

    def __init__(self, emit: Callable[[dict], None], wall: Callable[[int], str] | None = None):
        """wall: 単調時計の時刻 → 壁時計の文字列。省くと書いた時点の壁時計（記録を流すときは記録の時刻に合わせる）。"""
        self.emit = emit
        self.wall = wall or (lambda t_ns: wall_iso())
        self.mode = "idle"
        self._mode_t = 0
        self._outputs = 0
        self._scroll = 0
        self._zoom = {"in": 0, "out": 0}
        self._label: str | None = None
        self._drag: dict | None = None
        self._last_move: tuple[float, float] | None = None
        self.counts: dict[str, int] = {}

    def _rec(self, t_ns: int, type_: str, **kw) -> None:
        rec = {"t_ns": t_ns, "wall": self.wall(t_ns), "type": type_}
        rec.update(kw)
        self.counts[type_] = self.counts.get(type_, 0) + 1
        self.emit(rec)

    def record(self, t_ns: int, type_: str, **kw) -> None:
        """フレームに依らない行（有効化のオン・オフ、印など）。"""
        self._rec(t_ns, type_, **kw)

    def observe(self, frame: LandmarkFrame, events: Iterable[Event], mode: str, hand=None) -> None:
        t = frame.t_capture_ns
        for ev in events:
            k = ev.kind
            if k == "move":
                self._last_move = (ev.data["x"], ev.data["y"])
                continue
            if k in ("hand_acquired", "hand_lost"):
                kw = {"label": hand.handedness, "score": round(hand.score, 3)} if hand is not None else {}
                self._rec(t, k, active=frame.active, **kw)
                continue
            if k == "scroll":
                self._scroll += ev.data["dy"]
                self._outputs += 1
                continue
            if k == "zoom":
                self._zoom[ev.data["dir"]] += 1
            if k == "drag_start":
                self._drag = {"t": t, "x": ev.data["x"], "y": ev.data["y"]}
            if k in LOGGED_EVENTS:
                data = dict(ev.data)
                if k == "drag_end" and self._drag is not None:
                    end = self._last_move or (self._drag["x"], self._drag["y"])
                    data["held_ms"] = round((t - self._drag["t"]) / 1e6, 1)
                    data["moved_pt"] = round(((end[0] - self._drag["x"]) ** 2 + (end[1] - self._drag["y"]) ** 2) ** 0.5, 1)
                    self._drag = None
                self._rec(t, k, **data)
                if k not in ("pinch_start", "pinch_cancel"):
                    self._outputs += 1
        # 左右ラベル（MediaPipe の判定）の変化。手が見えている間だけ
        if hand is not None:
            if self._label is not None and hand.handedness != self._label:
                self._rec(t, "hand_label", label=hand.handedness, prev=self._label, score=round(hand.score, 3),
                          mode=mode, active=frame.active)
            self._label = hand.handedness
        # モードの遷移（有効化中だけ。無効化したら今のモードを閉じる）
        new = mode if frame.active else "idle"
        if new != self.mode:
            self._exit(t, new)
            if new != "idle":
                kw = {"label": hand.handedness, "score": round(hand.score, 3)} if hand is not None else {}
                self._rec(t, "mode_enter", mode=new, prev=self.mode, **kw)
            self.mode, self._mode_t, self._outputs, self._scroll = new, t, 0, 0
            self._zoom = {"in": 0, "out": 0}

    def _exit(self, t: int, to: str) -> None:
        m = self.mode
        if m == "idle":
            return
        kw: dict = {"mode": m, "to": to, "dur_ms": round((t - self._mode_t) / 1e6, 1), "outputs": self._outputs}
        if m == "peace" and self._scroll:
            kw["scroll_px"] = self._scroll
        if m == "zoom":
            kw["zoom_in"], kw["zoom_out"] = self._zoom["in"], self._zoom["out"]
        # 出力するはずのモードを、何も出さずに抜けた。ピンチ → ドラッグ、ピンチ → 右 のような同じ動作の続きは除く
        if m in OUTPUT_MODES and not (m == "pinch" and to == "drag"):
            kw["canceled"] = self._outputs == 0
        self._rec(t, "mode_exit", **kw)

    def close(self, t_ns: int) -> None:
        self._exit(t_ns, "end")
        self.mode = "idle"


@dataclass
class MarkConfig:
    ring_s: float = 10.0  # ランドマークを持っておく長さ
    pre_s: float = 6.0  # 印の前に残す長さ（5〜8 秒）
    post_s: float = 3.0  # 印の後に残す長さ

    @classmethod
    def from_toml(cls, cfg: dict) -> "MarkConfig":
        e = cfg.get("marks", {})
        return cls(**{k: v for k, v in e.items() if k in cls.__dataclass_fields__})


class MarkRecorder:
    """リングバッファと、印の前後の記録。add_frame は推論スレッド、mark はキー監視のスレッドから呼ぶ。
    書き出しは run_io（EventLogWriter.call）に渡して、推論スレッドでは JSON を作らない。"""

    def __init__(self, out_dir: str | Path, cfg: MarkConfig | None = None, meta: dict | None = None,
                 run_io: Callable[[Callable[[], None]], None] | None = None,
                 on_saved: Callable[[int, Path, int], None] | None = None):
        self.cfg = cfg or MarkConfig()
        self.dir = Path(out_dir)
        self.meta = meta or {}
        self.run_io = run_io or (lambda fn: fn())
        self.on_saved = on_saved
        self._ring: deque[LandmarkFrame] = deque()
        self._pending: list[dict] = []
        self._lock = threading.Lock()
        self.count = 0
        self.saved: list[Path] = []

    def add_frame(self, f: LandmarkFrame) -> None:
        ring = self._ring
        ring.append(f)
        limit = f.t_capture_ns - int(self.cfg.ring_s * 1e9)
        while ring and ring[0].t_capture_ns < limit:
            ring.popleft()
        if self._pending:
            with self._lock:
                for p in self._pending:
                    if f.t_capture_ns > p["t"]:
                        p["post"].append(f)
            self.poll(f.t_capture_ns)

    def mark(self, t_ns: int | None = None) -> tuple[int, Path]:
        """印を付ける。返り値は (番号, 保存先)。保存は post_s 秒後（フレームが来なければ poll の時刻で）。"""
        t = time.perf_counter_ns() if t_ns is None else t_ns
        with self._lock:
            self.count += 1
            n = self.count
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = self.dir / f"mark-{stamp}-{n}.jsonl"
            pre_from = t - int(self.cfg.pre_s * 1e9)
            pre = [f for f in list(self._ring) if pre_from <= f.t_capture_ns <= t]
            self._pending.append({"n": n, "t": t, "path": path, "pre": pre, "post": [], "wall": wall_iso()})
        return n, path

    def poll(self, now_ns: int | None = None) -> None:
        """後ろの post_s 秒がそろった印を書き出す。カメラが止まっていてフレームが来ないときは、時刻で締める。"""
        now = time.perf_counter_ns() if now_ns is None else now_ns
        done = []
        with self._lock:
            for p in list(self._pending):
                if now >= p["t"] + int(self.cfg.post_s * 1e9):
                    self._pending.remove(p)
                    done.append(p)
        for p in done:
            self.run_io(lambda p=p: self._save(p))

    def flush(self) -> None:
        """終了時: 残っている印を、そろった分だけで書き出す。"""
        with self._lock:
            done, self._pending = self._pending, []
        for p in done:
            self.run_io(lambda p=p: self._save(p))

    def _save(self, p: dict) -> None:
        end = p["t"] + int(self.cfg.post_s * 1e9)
        frames = p["pre"] + [f for f in p["post"] if f.t_capture_ns <= end]
        self.dir.mkdir(parents=True, exist_ok=True)
        head = {"format": FORMAT, "version": VERSION, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "mark": {"n": p["n"], "t_ns": p["t"], "wall": p["wall"], "pre_s": self.cfg.pre_s,
                         "post_s": self.cfg.post_s}}
        head.update(self.meta)
        with p["path"].open("w") as fp:
            fp.write(json.dumps(head, ensure_ascii=False) + "\n")
            for f in frames:
                fp.write(json.dumps(frame_to_dict(f), separators=(",", ":")) + "\n")
        self.saved.append(p["path"])
        if self.on_saved:
            self.on_saved(p["n"], p["path"], len(frames))
