"""AirController.app の入口（TASKS-20261027-package 第5節、SPEC 13.59）。

- 引数なし（Finder・Dock から開いた）: アプリ（`app`。ホームのウィンドウ）。出力は <データ>/logs/app-YYYYmmdd-HHMMSS.log に時刻付きで書く。
- 引数あり: そのまま aircontroller のコマンドとして動かす（アプリが子プロセスで起動する `run`、測定の `calibrate`、見積もりの
  `settings-eval` など）。出力は標準出力のまま（起動したアプリが受け取る）。
設定・ログ・記録は `~/Library/Application Support/AirController`（.app の中は書き込めない。AIRCONTROLLER_HOME）。
初回は、同梱の config.toml（既定値）とモデルを写す。calibration.toml（利用者ごとの値）は同梱しない。
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

T0 = time.time()
HOME = Path(os.environ.get("AIRCONTROLLER_HOME") or Path.home() / "Library/Application Support/AirController")


def bundle_dir() -> Path:
    """同梱した資源の場所（PyInstaller は sys._MEIPASS。ソースから動かすときは pkg/ の親）。"""
    return Path(getattr(sys, "_MEIPASS", "") or Path(__file__).resolve().parent.parent)


def seed() -> None:
    src = bundle_dir()
    for rel in ("config.toml", "models/hand_landmarker.task"):
        dst = HOME / rel
        if not dst.exists() and (src / rel).exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dst)


class Stamped:
    """行ごとに、時刻と起動からの秒数を付けてファイルに書く（アプリとして開いたときの出力）。"""

    def __init__(self, fp):
        self.fp, self.buf = fp, ""

    def write(self, s: str) -> int:
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            now = time.time()
            self.fp.write(f"{time.strftime('%H:%M:%S', time.localtime(now))}.{int(now * 1000) % 1000:03d} "
                          f"+{now - T0:6.2f}s  {line}\n")
            self.fp.flush()
        return len(s)

    def flush(self) -> None:
        self.fp.flush()

    def isatty(self) -> bool:
        return False


def main() -> int:
    HOME.mkdir(parents=True, exist_ok=True)
    os.environ["AIRCONTROLLER_HOME"] = str(HOME)
    # mediapipe の vision が matplotlib を読み込む。PyInstaller の実行時フックは matplotlib の設定の場所を毎回一時ディレクトリに
    # するので、フォントの一覧を毎回作り直して起動が約 10 秒遅れた（SPEC 13.55）。残る場所に置き直す
    os.environ["MPLCONFIGDIR"] = str(HOME / "cache" / "matplotlib")
    seed()
    args = [a for a in sys.argv[1:] if not a.startswith("-psn")]  # Finder が付ける引数は捨てる
    if not args:
        (HOME / "logs").mkdir(exist_ok=True)
        log = open(HOME / "logs" / f"app-{time.strftime('%Y%m%d-%H%M%S')}.log", "a", encoding="utf-8")
        sys.stdout = sys.stderr = Stamped(log)
        print(f"起動: {sys.executable}  データ {HOME}")
        args = ["app"]
    else:
        # .app では PYTHONUNBUFFERED が効かない（PyInstaller は PYTHON* の環境変数を読まない）。子プロセスの run の出力を、
        # 起動したアプリが 1 行ずつ受け取れるように（ホームの終了の理由の表示とログ）
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    from aircontroller.__main__ import main as ac_main  # AIRCONTROLLER_HOME を決めてから読む

    try:
        return int(ac_main(args) or 0)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


if __name__ == "__main__":
    sys.exit(main())
