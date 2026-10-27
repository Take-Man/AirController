# AirController.app の PyInstaller の設定（TASKS-20261027-package 第5節、SPEC 13.59）。使い方: pkg/build.sh app
# onedir の .app。署名は ad hoc（Apple のアカウントなし。作り直すと権限は付け直し: SPEC 13.55）
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = SPECPATH + "/.."
VERSION = "0.1.0"
INFO = {
    "CFBundleName": "AirController",
    "CFBundleDisplayName": "AirController",
    "CFBundleIdentifier": "com.takeman.aircontroller",
    "CFBundleShortVersionString": VERSION,
    "CFBundleVersion": VERSION,
    "LSMinimumSystemVersion": "14.0",
    "NSCameraUsageDescription": "手の形と位置をカメラで読み取り、マウスとキーの操作に変えるために使います。映像は保存も送信もしません。",
    "NSHighResolutionCapable": True,
}

a = Analysis(
    [ROOT + "/pkg/main.py"],
    pathex=[ROOT],
    # calibration.toml（利用者ごとの値）は入れない。config.toml は既定値として、初回にデータの場所へ写す
    datas=[(ROOT + "/config.toml", "."), (ROOT + "/models/hand_landmarker.task", "models"),
           (ROOT + "/aircontroller/web", "aircontroller/web")] + collect_data_files("mediapipe"),
    hiddenimports=collect_submodules("aircontroller") + ["AVFoundation", "CoreMedia", "Quartz", "AppKit", "Foundation",
                                                          "ApplicationServices", "libdispatch", "WebKit"],
    # matplotlib は mediapipe の vision が読み込むので外せない
    excludes=["tkinter", "pytest", "sounddevice", "jax", "jaxlib"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AirController", console=False,
          codesign_identity=None, target_arch="arm64")
coll = COLLECT(exe, a.binaries, a.datas, name="AirController")
app = BUNDLE(coll, name="AirController.app", bundle_identifier=INFO["CFBundleIdentifier"], info_plist=INFO)
