"""環境のチェック（TASKS-20261027-package 第4節）。設定画面のサーバーの中で、項目を 1 つずつ動かす。

権限は「実行元」に付く（ターミナルから動かせばそのターミナル、.app ならその .app）。どれを確かめているかを、最初の項目で必ず出す。
各項目は {id, title, status, summary, detail, fix} を返す。status は "ok"（合格）/ "warn"（要確認）/ "fail"（失敗）/ "skip"（前の項目の結果で
確かめられない）。fix は失敗・要確認のときの直し方の案内。
対話が要る項目（印のキー）は、画面が先に指示（ITEMS の prompt）を出してから呼ぶ。手の検出は外した（作者の判断）。

副作用: アクセシビリティの確かめで、カーソルを 1pt 動かしてすぐ戻す。カメラは確かめる間だけ使う。印のキーの確かめと設定では、
その間だけキーのイベントを受け取る（印のキー・押したキーは握りつぶす）。クリックやキーの送出はしない。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

ITEMS = [  # desc: 進捗の下に出す、何を確かめているかの短い説明
    {"id": "source", "title": "実行元", "desc": "権限が付く先（ターミナルか .app か）を調べています"},
    {"id": "system", "title": "macOS・書き込み先・熱", "desc": "macOS の版、空き容量、Mac の熱の状態を調べています"},
    {"id": "files", "title": "モデルと設定ファイル", "desc": "手の検出のモデルと、設定ファイルの中身を調べています"},
    {"id": "camera", "title": "カメラ", "desc": "カメラの権限と、映像が届くか（約 3 秒）を調べています"},
    {"id": "accessibility", "title": "アクセシビリティ", "desc": "マウスの操作を送れるか（カーソルを 1pt 動かして戻す）を調べています"},
    {"id": "input_monitoring", "title": "入力監視", "desc": "キーの監視（右 Command・印のキー・緊急停止）ができるかを調べています"},
    {"id": "keyboard", "title": "キーボード配列", "desc": "配列と、戻る・進む・ズームに使うキーを調べています"},
    {"id": "mark_key", "title": "印のキー", "desc": "印のキーが押せるか、文字が入力されないかを調べています",
     "prompt": "印のキー（{key}）を 1 回押してください（10 秒待ちます）。"},
    {"id": "shortcuts", "title": "システムのショートカット", "desc": "デスクトップ移動と Mission Control のショートカットを調べています"},
]
GUIDE = "システム設定 → プライバシーとセキュリティ → {}"
# 印のキーに使えないキー（操作・入力に要る）。修飾キーは押しただけでは文字のキーとして届かないので使えない
REFUSE = {36: "Return", 48: "Tab", 49: "Space", 51: "Delete", 53: "Esc", 123: "←", 124: "→", 125: "↓", 126: "↑"}


def result(item: str, status: str, summary: str, detail: list[str] | None = None, fix: str = "", **extra) -> dict:
    title = next(i["title"] for i in ITEMS if i["id"] == item)
    return {"id": item, "title": title, "status": status, "summary": summary, "detail": detail or [], "fix": fix, **extra}


def _ancestors() -> list[tuple[int, str]]:
    """自分から上の親プロセス（pid, コマンド）。実行元のアプリ（ターミナルなど）を探す。"""
    out, pid = [], os.getpid()
    for _ in range(12):
        try:
            line = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True,
                                  timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            break
        if not line:
            break
        ppid, _, comm = line.partition(" ")
        out.append((pid, comm.strip()))
        if not ppid.isdigit() or int(ppid) <= 1:
            break
        pid = int(ppid)
    return out


def source_info() -> dict:
    """実行元: .app として動いているか、どのアプリ（ターミナルなど）から動いているか。"""
    exe = sys.executable
    app = next((p.split(".app/")[0] + ".app" for p in [exe] if ".app/" in p and getattr(sys, "frozen", False)), None)
    host = None
    if app is None:
        for _pid, comm in _ancestors()[1:]:
            if ".app/" in comm:
                host = comm.split(".app/")[0].rsplit("/", 1)[-1] + ".app"
                break
    return {"frozen": bool(getattr(sys, "frozen", False)), "app": app, "host": host, "python": exe}


def check_source(ctx) -> dict:
    s = source_info()
    if s["app"]:
        who = Path(s["app"]).name
        return result("source", "ok", f"{who} として動いています。権限は {who} に付きます。", [f"実行ファイル: {s['python']}"])
    who = s["host"] or "不明（ターミナルが見つからない）"
    return result("source", "ok" if s["host"] else "warn",
                  f"ターミナルから動いています: {who}。権限はこのアプリに付きます（.app にしたときは .app に付け直し）。",
                  [f"Python: {s['python']}"],
                  "" if s["host"] else "どのアプリから動いているか分かりませんでした。権限の一覧で、実行に使ったアプリを確かめてください。",
                  host=who)


def check_system(ctx) -> dict:
    import Foundation

    ver = platform.mac_ver()[0]
    free = shutil.disk_usage(ctx.root).free / 2**30
    thermal = int(Foundation.NSProcessInfo.processInfo().thermalState())
    thermal_ja = {0: "正常", 1: "やや高い", 2: "高い", 3: "危険"}.get(thermal, "?")
    detail = [f"macOS {ver}（{platform.machine()}）", f"書き込み先 {ctx.root}: 空き {free:.1f}GB", f"熱の状態: {thermal_ja}"]
    status, fix = "ok", ""
    major = int(ver.split(".")[0]) if ver else 0
    if major and major < 14:
        status, fix = "warn", "macOS 14 以降で確かめています。古い macOS では動かない可能性があります。"
    if free < 1.0:
        status, fix = "warn", "空きが 1GB を切っています。ログ（logs/）と記録（recordings/）は増えていくので、空きを作ってください。"
    if thermal >= 2:
        status, fix = "warn", "Mac が熱くなっています。推論が遅れることがあります。しばらく冷ましてから使ってください。"
    return result("system", status, f"macOS {ver}、空き {free:.1f}GB、熱 {thermal_ja}", detail, fix)


def check_files(ctx) -> dict:
    """モデル（存在と大きさ）と、設定ファイル（台帳にない項目、範囲の外の値）。"""
    from .params import BY_ID

    cfg = ctx.config()
    detail, problems = [], []
    model = Path(cfg["tracker"]["model_path"])
    model = model if model.is_absolute() else ctx.root / model
    if not model.exists():
        problems.append(f"モデルがありません: {model}")
    else:
        size = model.stat().st_size
        detail.append(f"モデル: {model.name}（{size:,} バイト）")
        if size != 7_819_105:
            detail.append("  大きさが、確かめたモデル（7,819,105 バイト）と違います（別の版の可能性）")
    unknown, out_of_range = [], []
    for sec, vals in cfg.items():
        if not isinstance(vals, dict) or sec in ("position", "base"):
            continue
        for k, v in vals.items():
            p = BY_ID.get(f"{sec}.{k}")
            if p is None:
                unknown.append(f"{sec}.{k}")
            elif p.lo is not None and isinstance(v, (int, float)) and not isinstance(v, bool) and not (p.lo <= v <= p.hi):
                out_of_range.append(f"{sec}.{k} = {v}（範囲 {p.lo}〜{p.hi}）")
    detail.append(f"設定: config.toml + calibration.toml + auto.toml（{ctx.root}）")
    if unknown:
        detail.append("台帳にない項目: " + "、".join(unknown))
    if out_of_range:
        detail.append("範囲の外の値: " + "、".join(out_of_range))
    if problems:
        return result("files", "fail", problems[0], detail, "モデルを取得してください（README の「準備」、requirements.txt の末尾）。")
    if unknown or out_of_range:
        return result("files", "warn", "設定ファイルに、台帳にない項目か範囲の外の値があります。", detail,
                      "書き間違いでなければ、そのままで構いません。設定画面の「履歴」から前の状態に戻すこともできます。")
    return result("files", "ok", "モデルと設定ファイルは揃っています。", detail)


def check_camera(ctx) -> dict:
    from .input import CameraConfig, CameraError, open_camera
    from .output.permissions import camera_status

    st = camera_status()
    if st in ("拒否", "制限あり"):
        return result("camera", "fail", f"カメラの権限: {st}", [], GUIDE.format("カメラ") + f" で、{ctx.who()} をオンにして、開き直してください。")
    cfg = ctx.config()
    try:
        cam = open_camera(CameraConfig(**cfg["camera"]))
        cam.start()
    except CameraError as e:
        return result("camera", "fail", str(e), [f"権限: {st}"],
                      GUIDE.format("カメラ") + f" で、{ctx.who()} をオンにしてください。初めてのときは、確認のダイアログで「許可」を押してください。")
    try:
        t0, n, last, bright = time.time(), 0, 0, []
        while time.time() - t0 < 3.0:
            got = cam.wait_newer(last, 1.0)
            if got is None:
                continue
            last, _t, img = got
            n += 1
            if img is not None and n % 5 == 0:
                bright.append(float(np.mean(img)))
        fps = n / (time.time() - t0)
        w, h = cam.actual_size
    finally:
        cam.stop()
    b = float(np.median(bright)) if bright else 0.0
    detail = [f"{w}x{h}、実際の fps {fps:.1f}（設定 {cfg['camera']['fps']}）", f"画像の明るさ（0〜255 の平均）: {b:.0f}", f"権限: {st}"]
    if fps < 20:
        return result("camera", "warn", f"fps が低い（{fps:.1f}）", detail,
                      "暗いとカメラが露出を伸ばして fps が落ちます。部屋を明るくするか、ほかのアプリがカメラを使っていないか確かめてください。")
    if b < 50:
        return result("camera", "warn", f"画像が暗い（{b:.0f}）", detail, "暗いと手の検出が揺れます。手元を明るくしてください。")
    if b > 220:
        return result("camera", "warn", f"画像が明るすぎる（{b:.0f}）", detail, "逆光（窓や照明がカメラに入っている）を避けてください。")
    return result("camera", "ok", f"{fps:.1f}fps、明るさ {b:.0f}", detail)


def check_accessibility(ctx) -> dict:
    """AXIsProcessTrusted と、カーソルを 1pt 動かして戻すテスト（クリックはしない）。"""
    import Quartz
    from ApplicationServices import AXIsProcessTrusted

    trusted = bool(AXIsProcessTrusted())
    if not trusted:
        Quartz.CGRequestPostEventAccess()  # システム設定の一覧に実行元を載せる（オンにするのは利用者）
        return result("accessibility", "fail", "アクセシビリティの権限がありません。", [],
                      GUIDE.format("アクセシビリティ") + f" で、{ctx.who()} をオンにしてください（一覧になければ「＋」で追加）。"
                      "オンにした後、開き直してください。")
    p0 = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    target = Quartz.CGPointMake(p0.x + (1 if p0.x < 100 else -1), p0.y)

    def move(pt):
        ev = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, pt, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    move(target)
    time.sleep(0.05)
    p1 = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    move(p0)
    moved = abs(p1.x - target.x) < 0.6 and abs(p1.y - target.y) < 0.6
    detail = [f"カーソルを ({p0.x:.0f}, {p0.y:.0f}) から 1pt 動かして戻した（動いた先 ({p1.x:.0f}, {p1.y:.0f})）"]
    if not moved:
        return result("accessibility", "warn", "権限はありますが、カーソルを動かせませんでした。", detail,
                      "確かめている間にマウスを動かしていれば、もう一度試してください。続くときは、権限を一度オフにしてオンにし直してください。")
    return result("accessibility", "ok", "イベントを送れます（カーソルを 1pt 動かして戻した）。", detail)


def check_input_monitoring(ctx) -> dict:
    import Quartz

    pre = bool(Quartz.CGPreflightListenEventAccess())
    if not pre:
        Quartz.CGRequestListenEventAccess()
    tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap, Quartz.kCGEventTapOptionListenOnly,
                                  Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown), lambda *a: a[2], None)
    made = tap is not None
    if made:
        Quartz.CFMachPortInvalidate(tap)
    detail = [f"入力監視の権限: {'あり' if pre else 'なし'}", f"キーの監視（CGEventTap）: {'作れた' if made else '作れなかった'}"]
    if not (pre and made):
        return result("input_monitoring", "fail", "キーを監視できません（右 Command での切り替え・印のキー・緊急停止が効きません）。", detail,
                      GUIDE.format("入力監視") + f" で、{ctx.who()} をオンにして、開き直してください。")
    return result("input_monitoring", "ok", "キーを監視できます。", detail)


def check_keyboard(ctx) -> dict:
    """配列と、戻る・進む・ズームのキー（"auto" なら run の起動時に今の配列から決める。SPEC 13.57）。"""
    from .keyboard import SHORTCUTS, auto_keycode, char_of, key_label, layout, resolve_shortcut
    from .output.activation import keycode_of

    lay = layout()
    if lay is None:
        return result("keyboard", "warn", "キーボード配列を読み取れませんでした。", [],
                      "戻る・進む・ズームは JIS 配列の位置のキーで送ります。印のキーは、下の項目で確かめてください。")
    cfg = ctx.config()
    detail = [f"物理配列: {lay['physical']}（キーボードの種類 {lay['kbd_type']}）", f"キー配列: {lay['layout_id']}",
              f"入力ソース: {lay['input_id']}"]
    names = {"back": "戻る ⌘[", "forward": "進む ⌘]", "in": "ズームイン ⌘+", "out": "ズームアウト ⌘−"}
    fixed_mismatch = []
    for n, (ch, sec, key) in SHORTCUTS.items():
        code, how = resolve_shortcut(n, cfg[sec].get(key, "auto"))
        detail.append(f"{names[n]}: {key_label(code)}（キーコード {code}、{how}）")
        want = auto_keycode(n)
        if how == "設定の値" and want is not None and want != code:
            fixed_mismatch.append(f"{names[n]}（設定 {code} = 今の配列で「{char_of(code) or '?'}」、配列からは {want}）")
    mark = cfg["activation"].get("mark_key", "jis_yen")
    mcode = keycode_of(mark) if mark else None
    if mcode is not None:
        detail.append(f"印のキー: {key_label(mcode)}（キーコード {mcode}）")
    status, summary, fix = "ok", f"{lay['physical']} 配列。戻る・進む・ズームのキーは今の配列に合っています。", ""
    if fixed_mismatch:
        status, summary = "warn", f"{lay['physical']} 配列。設定で固定したキーが、今の配列と合っていません。"
        detail += ["合っていないキー: " + "、".join(fixed_mismatch)]
        fix = "config.toml / calibration.toml の該当のキーコードを \"auto\" にすると、今の配列から決めます。"
    if mcode == 93 and lay["physical"] != "JIS":
        status, summary = "warn", f"{lay['physical']} 配列には ¥ のキーがありません。"
        fix = "「印のキー」の「設定する」で、代わりのキーを設定してください。"
    return result("keyboard", status, summary, detail, fix, physical=lay["physical"])


def mark_key_info(ctx) -> dict:
    from .keyboard import key_label
    from .output.activation import keycode_of

    mark = ctx.config()["activation"].get("mark_key", "jis_yen")
    code = keycode_of(mark) if mark else None
    return {"keycode": code, "label": key_label(code) if code is not None else "（なし）"}


class KeyWaiter:
    """キーのイベントを一定時間だけ受け取る CGEventTap（印のキーの確かめと、キーの設定）。
    mode = "mark": 印のキーが押されるのを待ち、そのキーは握りつぶす（run と同じ）。
    mode = "any": 次に押された修飾キー以外のキーを受け取り、押下と解放を握りつぶす（設定するキーを選ぶ）。"""

    def __init__(self, mode: str, keycode: int | None = None):
        self.mode, self.keycode = mode, keycode
        self.got: int | None = None
        self.swallowed = 0
        self.error: str | None = None
        self._done = threading.Event()
        self._loop = None

    def _cb(self, proxy, etype, event, refcon):
        import Quartz

        if etype in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
            Quartz.CGEventTapEnable(self._tap, True)
            return event
        code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
        if self.mode == "mark":
            if code != self.keycode:
                return event
            if etype == Quartz.kCGEventKeyDown and self.got is None:
                self.got = code
                self._done.set()
            self.swallowed += 1
            return None
        if self.got is None and etype == Quartz.kCGEventKeyDown:
            self.got = code
            self.swallowed += 1
            return None
        if self.got is not None and code == self.got and etype == Quartz.kCGEventKeyUp:
            self.swallowed += 1
            self._done.set()
            return None
        return event

    def wait(self, timeout_s: float) -> int | None:
        import Quartz

        mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
        ready = threading.Event()

        def run():
            self._tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                                Quartz.kCGEventTapOptionDefault, mask, self._cb, None)
            if self._tap is None:
                self.error = "キーの監視を作れませんでした（入力監視・アクセシビリティの権限）"
                ready.set()
                return
            src = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            self._loop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(self._loop, src, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(self._tap, True)
            ready.set()
            Quartz.CFRunLoopRun()

        th = threading.Thread(target=run, name="keywait", daemon=True)
        th.start()
        ready.wait(3.0)
        if self.error is None:
            self._done.wait(timeout_s)
            time.sleep(0.15 if self.mode == "mark" else 0.0)  # 印のキーの解放も握りつぶしてから止める
            Quartz.CGEventTapEnable(self._tap, False)
            Quartz.CFMachPortInvalidate(self._tap)
            if self._loop is not None:
                Quartz.CFRunLoopStop(self._loop)
        th.join(2.0)
        return self.got


def check_mark_key(ctx) -> dict:
    info = mark_key_info(ctx)
    if info["keycode"] is None:
        return result("mark_key", "warn", "印のキーが設定されていません。", [], "「設定する」で、印に使うキーを選んでください。", key=info)
    w = KeyWaiter("mark", info["keycode"])
    got = w.wait(10.0)
    if w.error:
        return result("mark_key", "fail", w.error, [], GUIDE.format("入力監視") + " と アクセシビリティ をオンにしてください。", key=info)
    detail = [f"印のキー: {info['label']}（キーコード {info['keycode']}）"]
    if got is None:
        return result("mark_key", "warn", f"10 秒の間に {info['label']} が押されませんでした。", detail,
                      f"{info['label']} のキーがないキーボードなら、「設定する」で代わりのキーを選んでください。", key=info)
    # 握りつぶし（文字が入らなかったか）は、画面の入力欄で確かめる（画面が swallow_ok を付けて結果を出す）
    return result("mark_key", "ok", f"{info['label']} を検出しました。", detail, key=info)


def capture_key(timeout_s: float = 10.0) -> dict:
    """次に押されたキーを受け取り（握りつぶす）、印のキーに使えるかを返す。"""
    from .keyboard import key_label

    w = KeyWaiter("any")
    got = w.wait(timeout_s)
    if w.error:
        return {"error": w.error}
    if got is None:
        return {"timeout": True}
    return {"keycode": got, "label": key_label(got), **judge_key(got)}


def judge_key(code: int, emergency_keycode: int = 47, toggle_codes: tuple[int, ...] = (54,)) -> dict:
    """印のキーに使えるか: "refuse"（使えない）/ "warn"（使えるが、ほかの用途と衝突する）/ "ok"。"""
    from .keyboard import FUNCTION_CODES, MODIFIER_CODES, char_of

    if code in MODIFIER_CODES or code in toggle_codes:
        return {"level": "refuse", "reason": "修飾キーと切り替えのキーは、印に使えません。"}
    if code in REFUSE:
        return {"level": "refuse", "reason": f"{REFUSE[code]} は操作・入力に要るので、印に使えません。"}
    if code == emergency_keycode:
        return {"level": "warn", "reason": "緊急停止（Control+Option+Command と一緒に押す）と同じキーです。Controller の動作中は、このキーの文字が入力できなくなります。"}
    if code == 93:
        return {"level": "ok", "reason": "既定の ¥ です。Controller の動作中は ¥ が入力できなくなります（\\ は Option+¥）。"}
    if code in FUNCTION_CODES:
        return {"level": "ok", "reason": "ファンクションキーです。Controller の動作中は、このキーのほかの用途（明るさ・音量など）が効かなくなります。"}
    if code in (102, 104):
        return {"level": "warn", "reason": "英数・かなのキーです。Controller の動作中は、入力モードの切り替えに使えなくなります。"}
    ch = char_of(code) or ""
    if ch.strip():
        return {"level": "warn", "reason": f"文字（{ch}）を入力するキーです。Controller の動作中は、この文字が入力できなくなります。"}
    return {"level": "ok", "reason": "Controller の動作中は、このキーのほかの用途が効かなくなります。"}


def read_hotkeys() -> dict:
    """システム設定のキーボードショートカット（com.apple.symbolichotkeys。読むだけ）。変えていない項目は載らない。"""
    from Foundation import NSUserDefaults

    d = NSUserDefaults.standardUserDefaults().persistentDomainForName_("com.apple.symbolichotkeys")
    return dict(d.get("AppleSymbolicHotKeys", {})) if d else {}


def check_shortcuts(ctx) -> dict:
    """デスクトップ移動（⌃← ⌃→）と Mission Control（⌃↑）が、システム設定で有効で、送るキーと合っているか。"""
    hk = read_hotkeys()
    cfg = ctx.config()["thumbsup"]
    # id: 32 = Mission Control、79 = 左の操作スペースへ、81 = 右の操作スペースへ
    # 手を右へ → ⌃←（right_keycode）、手を左へ → ⌃→（left_keycode）
    want = [("32", "Mission Control（⌃↑）", cfg["up_keycode"]), ("79", "左の操作スペースへ（⌃←）", cfg["right_keycode"]),
            ("81", "右の操作スペースへ（⌃→）", cfg["left_keycode"])]
    detail, bad = [], []
    for ident, name, code in want:
        v = hk.get(ident)
        if v is None:
            detail.append(f"{name}: 既定のまま（有効のはず）")
            continue
        params = list(v.get("value", {}).get("parameters", []))
        on = bool(v.get("enabled"))
        key_ok = len(params) >= 3 and int(params[1]) == code and int(params[2]) & 0x40000  # Control
        detail.append(f"{name}: {'有効' if on else '無効'}、キー {params[1] if len(params) > 1 else '?'}"
                      f"{'' if key_ok else '（送るキーと違う）'}")
        if not on or not key_ok:
            bad.append(name)
    if bad:
        return result("shortcuts", "warn", "いいねの操作で使うショートカットが、無効か別のキーです: " + "、".join(bad), detail,
                      "システム設定 → キーボード → キーボードショートカット → Mission Control で、該当の項目をオンにし、既定のキーに戻してください。")
    return result("shortcuts", "ok", "デスクトップ移動と Mission Control のショートカットが有効です。", detail)


CHECKS = {"source": check_source, "system": check_system, "files": check_files, "camera": check_camera,
          "accessibility": check_accessibility, "input_monitoring": check_input_monitoring, "keyboard": check_keyboard,
          "mark_key": check_mark_key, "shortcuts": check_shortcuts}


class CheckContext:
    """1 回の「テスト開始」の間の状態（設定の読み込み、実行元）。"""

    def __init__(self, root: Path, load_config):
        self.root = Path(root)
        self._load = load_config

    def config(self) -> dict:
        return self._load()

    def who(self) -> str:
        s = source_info()
        return Path(s["app"]).name if s["app"] else (s["host"] or "実行に使ったターミナル")

    def run(self, item: str) -> dict:
        if item not in CHECKS:
            raise ValueError(f"知らない項目: {item}")
        try:
            return CHECKS[item](self)
        except Exception as e:  # noqa: BLE001  1 つの項目の失敗で、ほかの項目を止めない
            return result(item, "fail", f"確かめる途中でエラー: {e}", [], "もう一度試してください。続くときは、この内容を報告してください。")
