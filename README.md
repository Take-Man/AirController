# AirController

設計は [SPEC.md](SPEC.md)、現状の要約は [STATUS.md](STATUS.md)。段階6まで完了し、次は段階7（総合評価）。実装中の仕様変更は SPEC.md 第13節。

## 構成
- `aircontroller/types.py` 層の間のデータ（`LandmarkFrame`, `Event`）
- `aircontroller/input.py` 入力層（取得スレッド＋推論スレッド。最新フレームのみ処理）
- `aircontroller/gesture.py` ジェスチャー層（フレーム時刻と active だけで動く状態機械）
- `aircontroller/filters.py` One Euro Filter
- `aircontroller/peace.py` ピースモード（スクロール、戻る・進む）といいねモード（デスクトップ移動など）
- `aircontroller/zoom.py` ズームモード（指先の前後の測り方の候補と、ズームの段の送出）
- `aircontroller/calibration.py` 校正・学習の計算
- `aircontroller/features.py` 手の形の特徴量（候補のジェスチャーを比べる）
- `aircontroller/output/` 出力層: `mouse.py`（CGEvent）、`activation.py`（CGEventTap による有効化キー・緊急停止）、`overlay.py`、`permissions.py`
- `aircontroller/recording.py` ランドマークの記録・再生（JSON Lines。有効化の状態も記録）
- `aircontroller/latency.py` フレーム時刻ログ（CSV）と集計
- `config.toml` 設定

## 準備
```bash
uv venv --python 3.12 .venv && uv pip install --python .venv -r requirements.txt
```
モデルの取得方法は requirements.txt の末尾。

## 使い方
```bash
.venv/bin/python -m aircontroller run --timeout 120              # 右 Command（単独で押して離す）か ¥ で開始・終了
.venv/bin/python -m aircontroller run --record --no-output --activation hold --name pinch01   # ラベル付き記録（¥ 押下中 = ラベル）
.venv/bin/python -m aircontroller calibrate                      # 基準長・操作範囲・ピンチの閾値 → calibration.toml
.venv/bin/python -m aircontroller calibrate --pinch-only         # ピンチ・ピースの閾値と中立方向だけ（基準長・操作範囲は測らない）
.venv/bin/python -m aircontroller learn-thumbsup                 # いいねモードを上下左右 1回分の記録から学習 → calibration.toml
.venv/bin/python -m aircontroller learn-zoom                     # ズームモードを手招きの記録から学習 → calibration.toml
.venv/bin/python -m aircontroller lab --name lab01                # ジェスチャーの候補を試してラベル付きで記録
.venv/bin/python -m aircontroller lab-report recordings/lab01.jsonl  # 候補ごとの区別のしやすさ
open tools/targets.html                                          # クリック精度テスト（ブラウザで開く）
.venv/bin/python -m aircontroller modkeys --old --new             # 修飾キーが押されたままと扱われていないか確認
.venv/bin/python -m aircontroller keytest                        # キーコードと CGEventTap の確認
.venv/bin/python -m aircontroller camlatency                     # カメラ側の遅延
.venv/bin/python -m aircontroller view --record --name hand02    # 段階0の描画
.venv/bin/python -m aircontroller analyze recordings/pinch01.jsonl
.venv/bin/python -m aircontroller replay recordings/xxx.jsonl
.venv/bin/python -m aircontroller stats logs/latency-YYYYmmdd-HHMMSS.csv
.venv/bin/python -m pytest -q
```

- 緊急停止: Control+Option+Command+ピリオド（ボタンを離して終了）
- 待機中（有効化オフ）は推論を 10fps に間引き、5分でカメラを止める。オーバーレイのカメラのボタンでも止められる（No signal）。ボタンか有効化で再開
- 無効化すると、押下中のボタンとモードを解除する。Ctrl+C・終了時・例外時も解除する。

記録を再現テストに加える: `recordings/` から `tests/fixtures/` にコピーし、`python -m tests.make_golden tests/fixtures/xxx.jsonl` で期待値を作る。

## 権限（実行元のターミナルに付与）
システム設定 → プライバシーとセキュリティ で、次の3つをオンにする。
- カメラ
- 入力監視（キー監視）
- アクセシビリティ（イベント送出、キーの握りつぶし）
