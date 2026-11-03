#!/bin/sh
# .app に同梱するライブラリのライセンス文を 1 つのファイルにまとめる（LP 側の licenses/collect.sh を元に、KGProject の中だけで作り直せる形にした）。
# 使い方: pkg/licenses/collect.sh      出力: pkg/build/licenses/THIRD_PARTY_NOTICES.txt（pkg/build.sh app が先に呼ぶ）
# 一覧は pkg/licenses/components.txt。版は .venv-pkg に入っているものから読む。Python は .venv-pkg の元の Python のもの
set -e
cd "$(dirname "$0")/../.."
PY=.venv-pkg/bin/python
SP=$($PY -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
PYVER=$($PY -c "import platform; print(platform.python_version())")
PYLIC=$($PY -c "import sys; print(sys.base_prefix)")/lib/python${PYVER%.*}/LICENSE.txt
APPVER=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' pkg/AirController.spec)
mkdir -p pkg/build/licenses
OUT=pkg/build/licenses/THIRD_PARTY_NOTICES.txt
TMP=$OUT.tmp
# 名前|版|ライセンス|ライセンス文の場所（; 区切り）に直す
{
  echo "Python|$PYVER|PSF-2.0|$PYLIC"
  grep -v '^#' pkg/licenses/components.txt | grep . | while IFS='|' read -r name dist lic files; do
    di=$(ls -d "$SP/$dist"-*.dist-info 2>/dev/null | head -1)
    [ -n "$di" ] || { echo "見つかりません: $dist（.venv-pkg に入っていない）" >&2; exit 1; }
    ver=$(basename "$di" .dist-info); ver=${ver#"$dist"-}
    echo "$name|$ver|$lic|$(echo "$files" | tr ';' '\n' | sed "s|^|$di/|" | paste -sd ';' -)"
  done
} > $TMP.list
{
  # 画面では <pre> で出すが、ブラウザが文字コードを決めつけても読めるよう、英語だけにする
  echo "Third-party software included in AirController $APPVER"
  echo
  echo "MediaPipe Hand Landmarker model (hand_landmarker.task, float16/latest): Apache-2.0 (per the official model card)"
  while IFS='|' read -r name ver lic files; do echo "$name $ver: $lic"; done < $TMP.list
  echo
  while IFS='|' read -r name ver lic files; do
    printf '%s\n' "================================================================================"
    printf '%s %s - %s\n' "$name" "$ver" "$lic"
    printf '%s\n\n' "================================================================================"
    echo "$files" | tr ';' '\n' | while read -r f; do
      [ -f "$f" ] || { echo "見つかりません: $f" >&2; exit 1; }
      cat "$f"; echo
    done
  done < $TMP.list
} > $TMP
rm $TMP.list
mv $TMP $OUT
echo "$OUT: $(wc -l < $OUT | tr -d ' ') 行"
