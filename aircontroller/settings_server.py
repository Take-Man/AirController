"""設定画面のサーバー（設定画面の第5節、SPEC 13.46）。標準ライブラリだけで、127.0.0.1 に Web 画面を出す。

  python -m aircontroller settings

画面（aircontroller/web/）は素の HTML/JS で、ビルドしない。できることは、設定の閲覧と変更（保存・元に戻す・履歴・名前を付けた状態）、
測定（calibrate を別プロセスで起動）、効果の見積もり（settings-eval を別プロセスで起動）だけ。
セキュリティ: Host と Origin の確認（DNS リバインディングと、別のサイトからの要求を防ぐ）、起動時のトークン（API はヘッダーで必須）。
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .params import BY_ID, CHOICES, FEATURE_TOGGLES, OFF_AT_ZERO, PARAMS, SECTION_JA
from .settings import SettingsStore

WEB = Path(__file__).parent / "web"
STATIC = {"/app.js": "application/javascript; charset=utf-8", "/app.css": "text/css; charset=utf-8"}
DIST_KEYS = {"pinch": ("rest_ti", "pinch"), "right": ("rest_tm", "rpinch"), "peace": ("rest_thumb", "peace_thumb")}
RUN_RE = re.compile(r"-m aircontroller(\s+--config\s+\S+)?\s+run(\s|$)")


def screen_size() -> tuple[float, float]:
    try:
        import Quartz

        b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return float(b.size.width), float(b.size.height)
    except Exception:  # noqa: BLE001  画面のない環境（テスト）
        return 1440.0, 900.0


def run_pids() -> list[int]:
    """動いている run のプロセス（測定はカメラを使うので、run を止めてから行う）。"""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if RUN_RE.search(cmd) and pid.isdigit() and int(pid) != os.getpid():
            pids.append(int(pid))
    return pids


class Job:
    def __init__(self, kind: str, argv: list[str], cwd: Path, result: Path | None = None):
        self.id = secrets.token_hex(4)
        self.kind, self.result = kind, result
        self.t0 = time.time()
        self.log = tempfile.NamedTemporaryFile("w+", prefix=f"airctl-{kind}-", suffix=".log", delete=False)
        # 出力を逐次ファイルに書かせる（画面の進み具合は、出力の行で知る）
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        self.proc = subprocess.Popen(argv, cwd=cwd, stdout=self.log, stderr=subprocess.STDOUT, text=True, env=env)

    def info(self) -> dict:
        code = self.proc.poll()
        self.log.flush()
        text = Path(self.log.name).read_text(errors="replace")
        out = {"id": self.id, "kind": self.kind, "status": "running" if code is None else ("done" if code == 0 else "failed"),
               "code": code, "elapsed_s": round(time.time() - self.t0, 1), "log": text[-20000:]}
        if code == 0 and self.result is not None and self.result.exists():
            out["result"] = json.loads(self.result.read_text())
        return out


class App:
    def __init__(self, root: Path, config: str | None = None):
        self.root = Path(root)
        self.config = config
        self.store = SettingsStore(self.root, Path(config) if config else None)
        self.calib_history = self.root / "calibration-history"
        self.token = secrets.token_urlsafe(24)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self._dist_cache: dict = {}

    # ------------------------------------------------------------ 閲覧
    def _cli(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "aircontroller", *(["--config", self.config] if self.config else []), *args]

    def _history(self):
        """測定の履歴（古い順）。--from で以前の記録を計算し直したもの（*-from-*）は、新しい測定ではないので使わない。"""
        for p in sorted(self.calib_history.glob("*.json")) if self.calib_history.exists() else []:
            if "-from-" in p.stem:
                continue
            try:
                yield p, json.loads(p.read_text())
            except ValueError:
                continue

    def units(self) -> dict[str, dict]:
        """ユニットごとの、最新の測定の結果（候補・統計・警告）。"""
        units = {}
        for p, d in self._history():
            for u, r in d.get("results", {}).items():
                units[u] = {"file": p.name, "t": d.get("t"), "candidate": r.get("candidate"), "stats": r.get("stats"),
                            "warnings": r.get("warnings"), "report": r.get("report")}
        return units

    def measured(self) -> dict[str, dict]:
        """項目ごとの、最新の測定の候補（calibration-history/ の新しいものが優先）。"""
        out: dict[str, dict] = {}
        for p, d in self._history():
            for unit, r in d.get("results", {}).items():
                for sec, vals in (r.get("candidate") or {}).items():
                    for k, v in vals.items():
                        out[f"{sec}.{k}"] = {"value": v, "t": d.get("t", ""), "file": p.name, "unit": unit,
                                             "warnings": r.get("warnings", [])}
        return out

    def state(self) -> dict:
        default = self.store.load(calibration=False)
        cal = self.store.layer("calibration")
        saved = self.store.load()
        below = {s: dict(v) for s, v in default.items() if isinstance(v, dict)}
        for s, v in cal.items():
            if isinstance(v, dict):
                below.setdefault(s, {}).update(v)
        auto = self.store.valid_auto(below)
        measured = self.measured()
        params = []
        for p in PARAMS:
            if p.cls not in "AB":  # 画面に出すのは A と B だけ（作者の判断）
                continue
            src = "auto" if p.key in auto.get(p.section, {}) else "calibration" if p.key in cal.get(p.section, {}) else "config"
            params.append({"id": p.id, "section": p.section, "key": p.key, "name": p.name, "cls": p.cls, "lo": p.lo,
                           "hi": p.hi, "step": p.step, "unit": p.unit, "desc": p.desc, "writers": list(p.writers),
                           "position": p.position, "choices": list(CHOICES.get(p.id, ())),
                           "default": default[p.section][p.key], "saved": saved[p.section][p.key], "source": src,
                           "measured": measured.get(p.id)})
        cam = default.get("camera", {})
        return {"params": params, "sections": SECTION_JA, "units": self.units(), "off_at_zero": list(OFF_AT_ZERO),
                "image_size": [cam.get("width", 1280), cam.get("height", 720)], "screen_size": list(screen_size()),
                "toggles": {k: list(v) for k, v in FEATURE_TOGGLES.items()},
                "history": [{"id": s["id"], "route": s["route"], "t": s["t"]} for s in reversed(self.store.snapshots())],
                "named": self.store.named_states(), "run": run_pids(),
                "jobs": [j.info() | {"log": ""} for j in self.jobs.values()]}

    def _source(self, unit: str) -> tuple[Path, dict] | None:
        """分布の元の記録と、その時刻など。最新の測定（calibration-history）。いいね・ズームは、測定が無ければ
        最新の learn-thumbsup / learn-zoom の記録（ラベル付き）を使う（作者の判断）。"""
        st = self.units().get(unit)
        if st:
            d = json.loads((self.calib_history / st["file"]).read_text())
            return Path(d["recording"]), {"file": st["file"], "t": d.get("t")}
        if unit in ("thumbsup", "zoom"):
            recs = sorted(p for p in (self.root / "recordings").glob(f"learn-{unit}-*.jsonl")
                          if p.with_suffix(".labels.json").exists())
            if recs:
                return recs[-1], {"file": recs[-1].name, "t": recs[-1].stem.split("-", 2)[-1]}
        return None

    def dist(self, unit: str) -> dict:
        """測定の分布（設定画面の図に重ねる）。range: 手のひら中心の点、pinch / right / peace: 普段の姿勢と操作の距離・親指の畳み
        （peace はピースの中立方向の点も）、thumbsup: 各方向の方向の量と親指の離れ、zoom: 指先の前後の変化。"""
        from .calibration import collect_samples, reconstruct_old_segments, thumbsup_points, zoom_points
        from .recording import iter_frames, read_header

        if unit not in ("range", "pinch", "right", "peace", "thumbsup", "zoom"):
            raise ValueError("unit")
        src = self._source(unit)
        if src is None:
            return {"unit": unit, "available": False}
        rec, meta = src
        saved = self.store.load()
        key = (str(rec), unit, json.dumps(saved.get("zoom", {}), sort_keys=True) if unit == "zoom" else "",
               json.dumps(saved.get("thumbsup", {}), sort_keys=True) if unit == "thumbsup" else "")
        if key in self._dist_cache:
            return self._dist_cache[key]
        if not rec.exists():
            return {"unit": unit, "available": False, "why": f"記録がありません: {rec.name}"}
        frames = list(iter_frames(rec))
        lab = rec.with_suffix(".labels.json")
        segs = json.loads(lab.read_text())["segments"] if lab.exists() else reconstruct_old_segments(frames)
        hand = saved.get("gesture", {}).get("hand", "both")
        out = {"unit": unit, "available": True, "recording": rec.name, **meta}
        if unit == "thumbsup":
            t = saved["thumbsup"]
            r = thumbsup_points(frames, segs, hand, (t["thumbsup_x_w_thumb"], t["thumbsup_x_w_knuckle"],
                                                     t["thumbsup_y_w_thumb"], t["thumbsup_y_w_knuckle"]))
            out.update(points=r["points"], rest=r["far"]["neg"], op=r["far"]["thumbsup"])
        elif unit == "zoom":
            z = saved["zoom"]
            out["points"] = zoom_points(frames, segs, hand, z["zoom_signal"], z["zoom_sign"])
        else:
            s = collect_samples(frames, segs, hand)
            if unit == "range":
                out["points"] = [[round(x, 4), round(y, 4)] for x, y in s["range"]]
                out["image_size"] = read_header(rec).get("actual_size") or list(frames[0].image_size)
                out["screen_size"] = list(screen_size())
            else:
                rk, ok = DIST_KEYS[unit]
                out["rest"] = [round(v, 4) for v in s[rk] if v == v]
                out["op"] = [round(v, 4) for v in s[ok] if v == v]
                if unit == "peace":
                    out["neutral"] = [[round(a, 2), round(b, 2)] for a, b in s["peace_dir"]]
            if not out.get("points") and not out.get("op"):
                out = {"unit": unit, "available": False, "why": "その記録にこのユニットの区間がありません"}
        self._dist_cache[key] = out
        return out

    # ------------------------------------------------------------ 変更
    def coerce(self, values: dict) -> dict[str, dict]:
        """画面から来た値を、項目の型と範囲で確かめて {節: {キー: 値}} にする。A・B 以外と知らない項目は受け付けない。"""
        default = self.store.load(calibration=False)
        out: dict[str, dict] = {}
        for pid, v in values.items():
            p = BY_ID.get(pid)
            if p is None or p.cls not in "AB":
                raise ValueError(f"変更できない項目: {pid}")
            d = default[p.section][p.key]
            if isinstance(d, bool):
                if not isinstance(v, bool):
                    raise ValueError(f"{pid}: 真偽値ではない")
            elif pid in CHOICES:
                v = type(d)(v)
                if v not in CHOICES[pid]:
                    raise ValueError(f"{pid}: 選択肢にない値 {v}")
            elif isinstance(d, (int, float)):
                if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v:
                    raise ValueError(f"{pid}: 数値ではない")
                if p.lo is not None and not (p.lo <= v <= p.hi) and not (pid in OFF_AT_ZERO and v == 0):
                    raise ValueError(f"{pid}: 範囲 {p.lo}〜{p.hi} の外（{v}）")
                v = int(round(v)) if isinstance(d, int) else round(float(v), 6)
            elif isinstance(d, str):
                v = str(v)
            else:
                raise ValueError(f"変更できない項目: {pid}")
            out.setdefault(p.section, {})[p.key] = v
        return out

    def changed(self, values: dict) -> dict[str, dict]:
        saved = self.store.load()
        upd = self.coerce(values)
        return {s: {k: v for k, v in vals.items() if saved.get(s, {}).get(k) != v} for s, vals in upd.items()
                if any(saved.get(s, {}).get(k) != v for k, v in vals.items())}

    def save(self, values: dict, note: str = "") -> dict:
        upd = self.changed(values)
        if not upd:
            return {"changes": []}
        return {"changes": self.store.save(upd, "settings", note or "設定画面で保存した値")}

    def undo(self) -> dict:
        return {"changes": self.store.undo()}

    def restore(self, ident: str) -> dict:
        if ident in self.store.named_states():
            return {"changes": self.store.restore_named(ident)}
        p = self.store.history / f"{ident}.toml"
        if not re.fullmatch(r"\d{8}-\d{6}(-\d+)?", ident) or not p.exists():
            raise ValueError(f"履歴がありません: {ident}")
        return {"changes": self.store.restore(p, f"履歴から戻す（{ident}）")}

    def name(self, name: str) -> dict:
        self.store.name(name)
        return {"named": self.store.named_states()}

    # ------------------------------------------------------------ 別プロセスの仕事
    def _busy(self, kind: str) -> bool:
        return any(j.kind == kind and j.proc.poll() is None for j in self.jobs.values())

    def measure(self, units: list[str], stop_run: bool) -> dict:
        from .calibration import UNIT_ORDER

        if not units or any(u not in UNIT_ORDER for u in units):
            raise ValueError("知らないユニット")
        if self._busy("measure"):
            raise ValueError("測定の途中です")
        pids = run_pids()
        if pids and not stop_run:
            return {"need_stop": True, "run": pids}
        for pid in pids:  # Ctrl+C と同じ（押下中のボタンとモードを解除して終わる）
            os.kill(pid, signal.SIGINT)
        t_end = time.time() + 10
        while run_pids() and time.time() < t_end:
            time.sleep(0.2)
        if run_pids():
            raise ValueError("run が止まりません")
        job = Job("measure", self._cli("calibrate", "--unit", ",".join(units)), self.root)
        with self.lock:
            self.jobs[job.id] = job
        return {"job": job.id}

    def evaluate(self, values: dict, label: str) -> dict:
        if self._busy("eval"):
            raise ValueError("見積もりの途中です")
        upd = self.changed(values)
        if not upd:
            raise ValueError("保存済みと同じ値です（見積もる差がありません）")
        sets = [a for s, vals in upd.items() for k, v in vals.items() for a in ("--set", f"{s}.{k}={json.dumps(v)}")]
        out = Path(tempfile.mkdtemp(prefix="airctl-eval-")) / "result.json"
        from .settings_eval import load_manifest

        job = Job("eval", self._cli("settings-eval", *sets, "--json", str(out)), self.root, out)
        job.label = label
        # 進み具合の目安: 記録ごとのフレーム数（settings-eval は記録を 1 本流し始めるたびに名前を出す）
        job.plan = [[e.path.name, e.frames or 0] for e in load_manifest(self.root / "eval-manifest.toml", self.root)
                    if e.path.exists()]
        with self.lock:
            self.jobs[job.id] = job
        return {"job": job.id}

    def job(self, ident: str) -> dict:
        j = self.jobs.get(ident)
        if j is None:
            raise ValueError("仕事がありません")
        return j.info() | {"label": getattr(j, "label", ""), "plan": getattr(j, "plan", None)}


def make_handler(app: App, port_ref: list[int]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AirControllerSettings"

        def log_message(self, fmt, *args):  # 要求ごとの行は出さない
            pass

        def _allowed_hosts(self):
            return {f"127.0.0.1:{port_ref[0]}", f"localhost:{port_ref[0]}"}

        def _check(self, api: bool) -> bool:
            if self.headers.get("Host") not in self._allowed_hosts():
                self._send(403, {"error": "Host が違います"})
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {f"http://{h}" for h in self._allowed_hosts()}:
                self._send(403, {"error": "Origin が違います"})
                return False
            if api:
                tok = self.headers.get("X-Token", "")
                if not hmac.compare_digest(tok.encode(), app.token.encode()):
                    self._send(403, {"error": "トークンが違います"})
                    return False
            return True

        def _send(self, code: int, body, ctype: str = "application/json; charset=utf-8") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in STATIC:
                if self._check(False):
                    self._send(200, (WEB / u.path.lstrip("/")).read_bytes(), STATIC[u.path])
                return
            if u.path == "/":
                if not self._check(False):
                    return
                tok = parse_qs(u.query).get("t", [""])[0]
                if not hmac.compare_digest(tok.encode(), app.token.encode()):
                    self._send(403, "起動したときに表示した URL で開いてください".encode(), "text/plain; charset=utf-8")
                    return
                self._send(200, (WEB / "settings.html").read_bytes(), "text/html; charset=utf-8")
                return
            if not self._check(True):
                return
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/api/state":
                    self._send(200, app.state())
                elif u.path == "/api/run":
                    self._send(200, {"run": run_pids()})
                elif u.path == "/api/dist":
                    self._send(200, app.dist(q.get("unit", "")))
                elif u.path == "/api/job":
                    self._send(200, app.job(q.get("id", "")))
                else:
                    self._send(404, {"error": "ありません"})
            except ValueError as e:
                self._send(400, {"error": str(e)})

        def do_POST(self):
            if not self._check(True):
                return
            if self.headers.get("Origin") is None or not self.headers.get("Content-Type", "").startswith("application/json"):
                self._send(403, {"error": "Origin と JSON が要ります"})
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(n, 1_000_000)) or b"{}")
                path = urlparse(self.path).path
                if path == "/api/save":
                    out = app.save(body.get("values", {}), body.get("note", ""))
                elif path == "/api/undo":
                    out = app.undo()
                elif path == "/api/restore":
                    out = app.restore(str(body.get("id", "")))
                elif path == "/api/name":
                    out = app.name(str(body.get("name", "")))
                elif path == "/api/measure":
                    out = app.measure(list(body.get("units", [])), bool(body.get("stop_run")))
                elif path == "/api/eval":
                    out = app.evaluate(body.get("values", {}), str(body.get("label", "")))
                else:
                    self._send(404, {"error": "ありません"})
                    return
                self._send(200, out)
            except (ValueError, TypeError) as e:
                self._send(400, {"error": str(e)})
    return Handler


def serve(app: App, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    port_ref = [port]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port_ref))
    port_ref[0] = httpd.server_address[1]
    return httpd, f"http://127.0.0.1:{port_ref[0]}/?t={app.token}"
