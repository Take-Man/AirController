"""オーバーレイのギアのボタンから設定画面を開く（SPEC 13.51）。

設定画面のサーバー（`python -m aircontroller settings`）は、起動すると settings-server.json（所有者だけ読める。終了時に消す。
git の管理外）に PID・ポート・トークン付きの URL を書く。ギアを押したとき、そのサーバーが動いていて応答すれば、新しく立てずに
その URL をブラウザで開く。なければ別プロセス（sys.executable、start_new_session で切り離す）で起動し、そのプロセスが
いつもどおりブラウザを開く（webbrowser.open）。確認と起動はスレッドで行い、オーバーレイのメインスレッドを止めない。
run を終えても、起動した設定画面のサーバーは止めない（止めるときは設定画面のヘッダの「終了」。13.52）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

INFO = "settings-server.json"
LOG = "logs/settings-server.log"
LAUNCH_GUARD_S = 10.0  # 起動してから settings-server.json ができるまでの間に、もう一度押されても 2 つ目を立てない


def write_info(root: Path, port: int, url: str) -> None:
    fd = os.open(root / INFO, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fp:
        json.dump({"pid": os.getpid(), "port": port, "url": url}, fp)


def remove_info(root: Path) -> None:
    """自分が書いたものだけ消す（後から別のサーバーが書き直していれば残す）。"""
    try:
        if json.loads((root / INFO).read_text()).get("pid") == os.getpid():
            (root / INFO).unlink()
    except (OSError, ValueError):
        pass


def _probe(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


def running_url(root: Path, probe: Callable[[str], bool] = _probe) -> str | None:
    """動いている設定画面のサーバーの URL（トークン付き）。PID が生きていて、その URL が応答するときだけ（PID の使い回しに備える）。"""
    try:
        info = json.loads((root / INFO).read_text())
        os.kill(int(info["pid"]), 0)
        url = str(info["url"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return url if probe(url) else None


class SettingsLauncher:
    def __init__(self, root: Path, config: str | None = None, *, open_url: Callable[[str], object] | None = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen, probe: Callable[[str], bool] = _probe,
                 background: bool = True):
        if open_url is None:
            import webbrowser

            open_url = webbrowser.open
        self.root, self.config = Path(root), config
        self.open_url, self.popen, self.probe, self.background = open_url, popen, probe, background
        self._lock = threading.Lock()
        self._launched_at = -1e9

    def argv(self) -> list[str]:
        return [sys.executable, "-m", "aircontroller", *(["--config", self.config] if self.config else []), "settings"]

    def __call__(self) -> None:
        """ギアのボタン（メインスレッド）。確認と起動はスレッドで。"""
        if self.background:
            threading.Thread(target=self.open, name="settings-launch", daemon=True).start()
        else:
            self.open()

    def open(self) -> None:
        try:
            with self._lock:
                url = running_url(self.root, self.probe)
                if url:
                    print("[settings] 動いている設定画面を開きます", flush=True)
                    self.open_url(url)
                    return
                if time.monotonic() - self._launched_at < LAUNCH_GUARD_S:
                    print("[settings] 設定画面を起動中です", flush=True)
                    return
                log_path = self.root / LOG
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "a") as log:
                    proc = self.popen(self.argv(), cwd=self.root, stdin=subprocess.DEVNULL, stdout=log,
                                      stderr=subprocess.STDOUT, start_new_session=True)
                self._launched_at = time.monotonic()
                print(f"[settings] 設定画面を起動しました（PID {proc.pid}。出力は {LOG}）", flush=True)
            if self.background:  # すぐ終わったら（起動の失敗）端末に知らせる
                time.sleep(3.0)
                if proc.poll() is not None:
                    print(f"[settings] 設定画面のサーバーが終了しました（終了コード {proc.returncode}。{LOG} を見てください）",
                          flush=True)
        except Exception as e:  # オーバーレイは落とさない
            print(f"[settings] 設定画面を開けませんでした: {e}", flush=True)
