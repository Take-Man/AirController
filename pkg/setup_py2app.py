"""py2app の設定（TASKS-20261027-package 第3節の試作）。使い方: pkg/build.sh py2app

PyInstaller 版（pkg/AirControllerProbe.spec）と同じ中身・同じ Info.plist にして比べる。bundle id は同じにしない
（権限の付与は bundle id と署名で決まるので、2 つを別の .app として確かめる）。
"""
import sys
from pathlib import Path

from setuptools import setup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLIST = {
    "CFBundleName": "AirController Probe py2app",
    "CFBundleDisplayName": "AirController Probe (py2app)",
    "CFBundleIdentifier": "com.takeman.aircontroller.probe.py2app",
    "CFBundleShortVersionString": "0.0.1",
    "LSMinimumSystemVersion": "14.0",
    "LSUIElement": True,
    "NSCameraUsageDescription": "手の形と位置をカメラで読み取り、マウスとキーの操作に変えるために使います。映像は保存も送信もしません。",
    "NSHighResolutionCapable": True,
}

setup(
    name="AirController Probe (py2app)",
    app=[str(ROOT / "pkg/probe_main.py")],
    data_files=[("", [str(ROOT / "config.toml"), str(ROOT / "calibration.toml")]),
                ("models", [str(ROOT / "models/hand_landmarker.task")]),
                ("aircontroller/web", [str(p) for p in (ROOT / "aircontroller/web").iterdir()])],
    options={"py2app": {
        "plist": PLIST,
        "packages": ["aircontroller", "mediapipe", "cv2", "numpy", "matplotlib"],
        "includes": ["AVFoundation", "CoreMedia", "Quartz", "AppKit", "Foundation", "ApplicationServices", "libdispatch"],
        "excludes": ["tkinter", "pytest", "sounddevice", "jax", "jaxlib", "PyInstaller", "_pyinstaller_hooks_contrib", "PyQt5", "PyQt6",
                     "PySide2", "PySide6", "IPython"],  # 同じ venv の PyInstaller の hooks をたどらない
        "arch": "arm64",
    }},
)
