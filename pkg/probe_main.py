"""パッケージ化の実現性の試作（TASKS-20261027-package 第3節）: .app の起動用の入口。

.app の中は書き込めないので、設定・ログ・記録の置き場所を ~/Library/Application Support/AirController-probe にする
（AIRCONTROLLER_HOME）。初回は、.app に同梱した config.toml・calibration.toml・モデルをそこへ写す（試作では作者の今の設定で動かし、
ターミナルで動かしたときと遅延・CPU を比べるため）。

そのまま `run` を動かす（カメラ、MediaPipe、オーバーレイ、CGEventTap、CGEvent の送出がすべて入っている）。標準出力・標準エラーは
<HOME>/logs/app-YYYYmmdd-HHMMSS.log に、壁時計の時刻を付けて書く（起動の時間を測るため）。権限が足りずに終わったときは、
ダイアログで知らせる（.app にはターミナルがない）。
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

T0 = time.time()
HOME = Path.home() / "Library/Application Support/AirController-probe"


def bundle_dir() -> Path:
    """同梱した資源の場所（PyInstaller は sys._MEIPASS、py2app は RESOURCEPATH、ソースから動かすときは pkg/ の親）。"""
    for v in (getattr(sys, "_MEIPASS", None), os.environ.get("RESOURCEPATH")):
        if v:
            return Path(v)
    return Path(__file__).resolve().parent.parent


class Stamped:
    """行ごとに、起動からの秒数と壁時計の時刻を付けてファイルに書く。"""

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


def seed() -> None:
    src = bundle_dir()
    for rel in ("config.toml", "calibration.toml", "models/hand_landmarker.task"):
        dst = HOME / rel
        if not dst.exists() and (src / rel).exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dst)


def alert(text: str) -> None:
    import AppKit

    AppKit.NSApplication.sharedApplication()
    a = AppKit.NSAlert.alloc().init()
    a.setMessageText_("AirController（試作）")
    a.setInformativeText_(text)
    a.runModal()


def selftest() -> int:
    """同梱の確認（権限は要らない）: OpenCV、pyobjc、MediaPipe のモデルの読み込みと、黒い画像での推論の時間。ターミナルから
    `<.app>/Contents/MacOS/<実行ファイル> --selftest` で動かす。"""
    import importlib

    for name in ("numpy", "cv2", "Quartz", "AppKit", "mediapipe", "mediapipe.tasks.python.vision"):
        s = time.time()
        importlib.import_module(name)
        print(f"selftest: import {name} {time.time() - s:.2f}s")
    import cv2
    import mediapipe as mp
    import numpy as np
    from mediapipe.tasks.python import BaseOptions, vision

    t = time.time()
    lm = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(HOME / "models/hand_landmarker.task")), num_hands=1))
    img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.zeros((720, 1280, 3), np.uint8))
    lm.detect(img)
    t1 = time.time()
    ms = []
    for _ in range(20):
        s = time.perf_counter()
        lm.detect(img)
        ms.append((time.perf_counter() - s) * 1000)
    print(f"selftest: cv2 {cv2.__version__}  mediapipe {mp.__version__}  モデルの読み込みと 1 回目 {t1 - t:.2f}s  "
          f"推論（手なし）中央値 {sorted(ms)[10]:.1f}ms  起動から {time.time() - T0:.2f}s")
    lm.close()  # 終了時の __del__ に任せると、py2app 版で後始末の例外が出た
    return 0


def main() -> int:
    HOME.mkdir(parents=True, exist_ok=True)
    # mediapipe の vision が matplotlib を読み込む。PyInstaller の実行時フック（pyi_rth_mplconfig）は matplotlib の設定の場所を
    # 毎回新しい一時ディレクトリにするので、フォントの一覧を毎回作り直して、起動が約 10 秒遅れた。残る場所に置き直す
    os.environ["MPLCONFIGDIR"] = str(HOME / "cache" / "matplotlib")
    if "--selftest" in sys.argv:
        os.environ["AIRCONTROLLER_HOME"] = str(HOME)
        seed()
        return selftest()
    (HOME / "logs").mkdir(exist_ok=True)
    os.environ["AIRCONTROLLER_HOME"] = str(HOME)
    seed()
    log = open(HOME / "logs" / f"app-{time.strftime('%Y%m%d-%H%M%S')}.log", "a", encoding="utf-8")
    sys.stdout = sys.stderr = Stamped(log)
    print(f"起動: {sys.executable}  frozen={getattr(sys, 'frozen', False)}  資源 {bundle_dir()}  データ {HOME}")
    from aircontroller.__main__ import main as ac_main  # AIRCONTROLLER_HOME を決めてから読む

    print("読み込み完了")
    try:
        code = ac_main(["run", *sys.argv[1:]] if not any(a.startswith("-psn") for a in sys.argv[1:]) else ["run"])
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except Exception:
        import traceback

        traceback.print_exc()
        code = 1
    print(f"終了: {code}")
    if code:
        log.flush()
        alert(f"終了しました（コード {code}）。権限が足りない場合は、システム設定 → プライバシーとセキュリティ で、"
              f"この .app をオンにしてから開き直してください。\n詳細: {log.name}")
    return int(code or 0)


if __name__ == "__main__":
    sys.exit(main())
