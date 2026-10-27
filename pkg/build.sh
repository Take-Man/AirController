#!/bin/sh
# パッケージ化の試作をビルドする（TASKS-20261027-package 第3節）。.venv-pkg（requirements.txt + PyInstaller + py2app）を使う。
# 使い方: pkg/build.sh app | dmg | pyinstaller | py2app      出力: pkg/dist/*.app（ビルドの途中物は pkg/build/）
#   app: AirController.app（SPEC 13.59）。dmg: それを .dmg にする（Applications へのリンクつき）。pyinstaller / py2app: 第3節の試作
set -e
cd "$(dirname "$0")/.."
PY=.venv-pkg/bin/python
case "$1" in
  app)
    $PY -m PyInstaller --noconfirm --clean --workpath pkg/build/app --distpath pkg/dist pkg/AirController.spec ;;
  dmg)
    rm -rf pkg/build/dmg && mkdir -p pkg/build/dmg/src
    cp -R pkg/dist/AirController.app pkg/build/dmg/src/
    ln -s /Applications pkg/build/dmg/src/Applications
    hdiutil create -quiet -volname "AirController" -srcfolder pkg/build/dmg/src -fs APFS -format UDZO -ov pkg/dist/AirController.dmg
    echo "pkg/dist/AirController.dmg" ;;
  pyinstaller)
    $PY -m PyInstaller --noconfirm --clean --workpath pkg/build/pyinstaller --distpath pkg/dist pkg/AirControllerProbe.spec ;;
  py2app)
    rm -rf pkg/build/py2app "pkg/dist/AirController Probe py2app.app"
    $PY pkg/setup_py2app.py py2app --bdist-base pkg/build/py2app --dist-dir pkg/dist
    # py2app は署名しないので、ad hoc で署名する。--deep だけでは、py2app が書き換えたライブラリ（Frameworks/*.dylib）の検証が
    # 通らなかったので、中のライブラリから順に署名し、最後に .app を署名する
    APP="pkg/dist/AirController Probe py2app.app"
    # py2app は Pillow の liblzma を Frameworks に写し、書き換えたときに壊した（codesign が「main executable failed strict validation」）。
    # Python の _lzma が作られたときの Homebrew の xz の liblzma に差し替える
    cp -f /opt/homebrew/opt/xz/lib/liblzma.5.dylib "$APP/Contents/Frameworks/liblzma.5.dylib"
    chmod u+w "$APP/Contents/Frameworks/liblzma.5.dylib"
    install_name_tool -id "@executable_path/../Frameworks/liblzma.5.dylib" "$APP/Contents/Frameworks/liblzma.5.dylib" 2>/dev/null
    find "$APP/Contents" -type f \( -name "*.so" -o -name "*.dylib" \) -exec codesign --force -s - {} \;
    codesign --force -s - "$APP/Contents/Frameworks/Python.framework"
    codesign --force -s - "$APP" ;;
  *) echo "使い方: $0 app | dmg | pyinstaller | py2app"; exit 2 ;;
esac
