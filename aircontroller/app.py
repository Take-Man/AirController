"""アプリの枠組み（TASKS-20261027-package 第5節、SPEC 13.58）: 1 つのウィンドウ（WKWebView）に 4 つのタブ（起動・チュートリアル・
環境のチェック・設定）。画面は設定画面のサーバー（settings_server.py）が出す Web の画面で、このプロセスの中のスレッドで動かす。

run は子プロセス（同じ実行ファイルに引数 run。.app でなければ python -m aircontroller run）。起動タブの「開始」で起動し、「停止」で
SIGINT を送る（Ctrl+C と同じ終わり方）。状態は run のライブのサーバーの GET /status から読む。オーバーレイは run のもの（ネイティブ）。

ウィンドウ: ホーム（起動。環境のチェックもここで開く）は大きさを固定。設定とチュートリアルは、別の大きなウィンドウで開く（作者の FB）。
ウィンドウを閉じても、アプリと run は動き続ける（Dock のアイコンで開き直す）。アプリを終了する（⌘Q）と、run も止める。
オーバーレイのギアのボタンは、このアプリが動いていれば、設定のウィンドウを前に出す（settings_launch.py）。
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .settings_launch import cli_argv
from .settings_server import App, run_pids, serve

TABS = {"launch": "/launch", "tutorial": "/tutorial", "check": "/check", "settings": "/"}
SEPARATE = {"settings": "AirController 設定", "tutorial": "AirController チュートリアル"}  # 別のウィンドウで開く画面
HOME_SIZE = (744, 540)  # ホームの大きさ（固定。作者が示した大きさ。状態の 1 行と案内の 1 行が出てもスクロールしない高さ）。環境のチェックも、項目を畳んだ状態でこの中に収める
# 設定・チュートリアルの大きさ: 設定画面のライブを開いても、本文（最大 1040）の左に余白が残らない幅（1040 + ライブ 380）。画面に収める
PAGE_SIZE = (1420, 900)


class RunProcess:
    """子プロセスの run。開始・停止と、出力の最後の行（終わった理由を出すため）。"""

    def __init__(self, root: Path, config: str | None = None):
        self.root = Path(root)
        self.config = config
        self.proc: subprocess.Popen | None = None
        self.tail: deque[str] = deque(maxlen=40)
        self.started_at = 0.0
        self.log_path: Path | None = None
        self._lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> dict:
        with self._lock:
            if self.running():
                return {"ok": True, "already": True}
            others = [p for p in run_pids() if self.proc is None or p != self.proc.pid]
            if others:
                return {"ok": False, "error": f"ほかの run が動いています（PID {', '.join(map(str, others))}）。先に止めてください。",
                        "others": others}
            logs = self.root / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            self.log_path = logs / f"run-app-{time.strftime('%Y%m%d-%H%M%S')}.log"
            self.tail.clear()
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            argv = cli_argv(*(["--config", self.config] if self.config else []), "run")
            self.proc = subprocess.Popen(argv, cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, env=env)
            self.started_at = time.time()
            threading.Thread(target=self._pump, args=(self.proc, self.log_path), name="run-output", daemon=True).start()
            return {"ok": True, "pid": self.proc.pid}

    def _pump(self, proc: subprocess.Popen, path: Path) -> None:
        with open(path, "a", encoding="utf-8") as log:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                line = line.rstrip()
                if line and not line.startswith(("W0000", "I0000")):  # MediaPipe の内部の行は画面に出さない
                    self.tail.append(line)

    def stop(self, others: bool = False, timeout_s: float = 8.0) -> dict:
        """SIGINT で止める（押下中のボタンとモードを解除して終わる）。others なら、ターミナルから起動した run も止める。"""
        pids = [self.proc.pid] if self.running() else []
        if others:
            pids += [p for p in run_pids() if p not in pids]
        for pid in pids:
            try:
                os.kill(pid, signal.SIGINT)
            except OSError:
                pass
        t_end = time.time() + timeout_s
        while time.time() < t_end and (self.running() or (others and run_pids())):
            time.sleep(0.1)
        if self.running():
            self.proc.terminate()
        return {"ok": True, "stopped": pids}

    def status(self, app: App) -> dict:
        out = {"running": self.running(), "pid": self.proc.pid if self.proc else None,
               "code": self.proc.poll() if self.proc else None, "uptime_s": round(time.time() - self.started_at) if self.running() else 0,
               "others": [p for p in run_pids() if self.proc is None or p != self.proc.pid],
               "tail": list(self.tail)[-8:], "log": str(self.log_path.relative_to(self.root)) if self.log_path else None}
        if app.live_info() is not None:
            try:
                out["live"] = app.live_request("GET", "/status")
            except (ValueError, OSError):
                pass
        return out


def run_app(root: Path, config: str | None = None, tab: str = "launch") -> int:
    """アプリを動かす（メインスレッドで AppKit のイベントループ）。"""
    import AppKit
    import Foundation
    import WebKit
    from PyObjCTools import AppHelper

    from .settings_launch import remove_info, write_info

    app = App(root, config)
    runner = RunProcess(root, config)
    app.runner = runner
    httpd, url = serve(app, 0)
    base = url.split("/?")[0]
    token = app.token
    write_info(root, httpd.server_address[1], url, app=True)  # ギアのボタンが、このアプリのウィンドウを前に出すため
    threading.Thread(target=httpd.serve_forever, name="settings-server", daemon=True).start()
    print(f"アプリ: 画面のサーバー {base}（このプロセスの中）", flush=True)

    ns_app = AppKit.NSApplication.sharedApplication()
    ns_app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

    def page(name: str, separate: bool = False) -> Foundation.NSURL:
        # 別のウィンドウ（設定・チュートリアル）には win=1 を付ける（「ホームに戻る」を出さない）
        return Foundation.NSURL.URLWithString_(f"{base}{TABS.get(name, '/launch')}?t={token}{'&win=1' if separate else ''}")

    def make_window(title: str, w: float, h: float, resizable: bool, autosave: str):
        style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable | AppKit.NSWindowStyleMaskMiniaturizable
        if resizable:
            style |= AppKit.NSWindowStyleMaskResizable
        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            Foundation.NSMakeRect(0, 0, w, h), style, AppKit.NSBackingStoreBuffered, False)
        win.setTitle_(title)
        win.setReleasedWhenClosed_(False)  # 閉じても作り直さずに使う
        win.center()
        if autosave:
            win.setFrameAutosaveName_(autosave)
        conf = WebKit.WKWebViewConfiguration.alloc().init()
        web = WebKit.WKWebView.alloc().initWithFrame_configuration_(win.contentView().bounds(), conf)
        web.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        win.contentView().addSubview_(web)
        return win, web

    # ホーム（起動・環境のチェック）: 大きさを固定（作者の FB）。設定・チュートリアル: 別の大きなウィンドウ
    home, home_web = make_window("AirController", *HOME_SIZE, False, "")
    pages: dict[str, tuple] = {}

    class PageWindowDelegate(AppKit.NSObject):
        """設定などのウィンドウを閉じたら（赤いボタン・⌘W）、中の画面を空にする。画面が run のライブにつながったままだと、
        run が設定モード（出力を止める）のままになり、「開始」してもカーソルが動かなかった（作者の実機、2026-10-28）。"""

        def windowWillClose_(self, note):
            for win, web in pages.values():
                if win is note.object():
                    web.loadHTMLString_baseURL_("", None)

    page_delegate = PageWindowDelegate.alloc().init()

    def show(name: str) -> None:
        """その画面を前に出す（メインスレッドで呼ぶ）。設定・チュートリアルは別のウィンドウ（一度作ったら使い回す）。"""
        if name in SEPARATE:
            if name not in pages:
                vis = AppKit.NSScreen.mainScreen().visibleFrame()
                w, h = min(PAGE_SIZE[0], vis.size.width), min(PAGE_SIZE[1], vis.size.height)
                pages[name] = make_window(SEPARATE[name], w, h, True, f"AirController-{name}")
                pages[name][0].setDelegate_(page_delegate)
            win, web = pages[name]
            if not win.isVisible():  # 閉じていたウィンドウは読み直す（「終了」の後の表示や、古い値を残さない）
                web.loadRequest_(Foundation.NSURLRequest.requestWithURL_(page(name, True)))
        else:
            home_web.loadRequest_(Foundation.NSURLRequest.requestWithURL_(page(name)))
            win = home
        win.makeKeyAndOrderFront_(None)
        ns_app.activateIgnoringOtherApps_(True)

    class Delegate(AppKit.NSObject):
        def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
            return False  # ウィンドウを閉じても、run は動き続ける

        def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, flag):
            if not flag:
                home.makeKeyAndOrderFront_(None)  # Dock のアイコンで開き直す
            return True

        def applicationWillTerminate_(self, note):
            runner.stop()
            httpd.shutdown()
            remove_info(root)

    delegate = Delegate.alloc().init()
    ns_app.setDelegate_(delegate)

    # メニュー: アプリ（終了）と編集（Web の入力欄でコピー・貼り付けを使うため）
    menubar = AppKit.NSMenu.alloc().init()
    for title, items in (("AirController", [("AirController を終了", "terminate:", "q")]),
                         ("編集", [("取り消す", "undo:", "z"), ("カット", "cut:", "x"), ("コピー", "copy:", "c"),
                                  ("ペースト", "paste:", "v"), ("すべてを選択", "selectAll:", "a")]),
                         ("表示", [("再読み込み", "reload:", "r")]),
                         ("ウインドウ", [("閉じる", "performClose:", "w"), ("しまう", "performMiniaturize:", "m")])):
        top = AppKit.NSMenuItem.alloc().init()
        menu = AppKit.NSMenu.alloc().initWithTitle_(title)
        for t, sel, key in items:
            menu.addItemWithTitle_action_keyEquivalent_(t, sel, key)
        top.setSubmenu_(menu)
        menubar.addItem_(top)
    ns_app.setMainMenu_(menubar)

    home_web.loadRequest_(Foundation.NSURLRequest.requestWithURL_(page("launch")))
    home.makeKeyAndOrderFront_(None)
    if tab != "launch":
        show(tab)
    ns_app.activateIgnoringOtherApps_(True)

    def focus(tab_name: str) -> None:
        """ギアのボタン・ホームの入口から: その画面のウィンドウを前に出す（メインスレッドで）。"""
        AppHelper.callAfter(show, tab_name)

    app.focus = focus

    def close_window(name: str) -> None:
        """設定画面の「終了」: そのウィンドウを閉じる（アプリと run は動き続ける）。"""
        def do():
            if name in pages:
                pages[name][0].performClose_(None)  # 赤いボタンと同じ（中の画面を空にして、run とのつながりを切る）
        AppHelper.callAfter(do)

    app.close_window = close_window

    def on_sigint(*_):
        AppHelper.callAfter(ns_app.terminate_, None)

    signal.signal(signal.SIGINT, on_sigint)
    signal.signal(signal.SIGTERM, on_sigint)
    # Python のシグナルの処理は、Python のコードが動くときに走る。AppKit のイベントループの間も届くように、0.5 秒ごとに起こす
    def wake(timer):
        pass

    wake_timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.5, True, wake)  # noqa: F841  ループの間は持っておく
    AppHelper.runEventLoop(installInterrupt=False)
    return 0
