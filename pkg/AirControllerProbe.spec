# PyInstaller の設定（TASKS-20261027-package 第3節の試作）。使い方: pkg/build.sh pyinstaller
# onedir の .app（onefile は起動のたびに展開して遅い）。署名は ad hoc（PyInstaller が codesign -s - をかける）
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = SPECPATH + "/.."
INFO = {
    "CFBundleName": "AirController Probe",
    "CFBundleDisplayName": "AirController Probe",
    "CFBundleIdentifier": "com.takeman.aircontroller.probe",
    "CFBundleShortVersionString": "0.0.1",
    "LSMinimumSystemVersion": "14.0",
    "LSUIElement": True,  # Dock に出さない（オーバーレイだけ。終了は Control+Option+Command+ピリオド）
    "NSCameraUsageDescription": "手の形と位置をカメラで読み取り、マウスとキーの操作に変えるために使います。映像は保存も送信もしません。",
    "NSHighResolutionCapable": True,
}

a = Analysis(
    [ROOT + "/pkg/probe_main.py"],
    pathex=[ROOT],
    datas=[(ROOT + "/config.toml", "."), (ROOT + "/calibration.toml", "."), (ROOT + "/models/hand_landmarker.task", "models"),
           (ROOT + "/aircontroller/web", "aircontroller/web")] + collect_data_files("mediapipe"),
    hiddenimports=collect_submodules("aircontroller") + ["AVFoundation", "CoreMedia", "Quartz", "AppKit", "Foundation",
                                                          "ApplicationServices", "libdispatch"],
    # matplotlib は mediapipe の vision が読み込むので外せない
    excludes=["tkinter", "pytest", "sounddevice", "jax", "jaxlib"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AirControllerProbe", console=False,
          codesign_identity=None, target_arch="arm64")
coll = COLLECT(exe, a.binaries, a.datas, name="AirControllerProbe")
app = BUNDLE(coll, name="AirController Probe (PyInstaller).app", bundle_identifier=INFO["CFBundleIdentifier"], info_plist=INFO)
