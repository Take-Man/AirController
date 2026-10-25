# AirController

設計は [SPEC.md](SPEC.md)、現状の要約は [STATUS.md](STATUS.md)。段階6まで完了し、2026-10-25 の改善（TASKS-20261025.md）を反映した。次は段階7（総合評価）。実装中の仕様変更は SPEC.md 第13節（2026-10-25 の改善は 13.24〜）。

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
- `aircontroller/eventlog.py` 常時のイベントログと、¥ の印の前後の記録。`report.py` その集計
- `aircontroller/power.py` 手の不在による自動オフ、オフの間の 1Hz、パーでのオン
- `aircontroller/cpustat.py` スレッド別の CPU 時間（負荷の計測用）
- `aircontroller/latency.py` フレーム時刻ログ（CSV）と集計
- `config.toml` 設定（`calibration.toml` が上書きする。オーバーレイを畳んだかは `ui_state.json`）
- `tools/` 計測・分析の道具（各ファイルの先頭に使い方）

## 準備
```bash
uv venv --python 3.12 .venv && uv pip install --python .venv -r requirements.txt
```
モデルの取得方法は requirements.txt の末尾。

## 使い方
```bash
.venv/bin/python -m aircontroller run                            # 右 Command（単独で押して離す）で開始・終了、¥ で印
.venv/bin/python -m aircontroller report logs/events-*.jsonl     # イベントログの集計（印、取り消し、誤ドラッグの候補など）
.venv/bin/python -m aircontroller report --from-recording recordings/xxx.jsonl  # 記録から集計
.venv/bin/python -m aircontroller run --record --no-output --activation hold --name pinch01   # ラベル付き記録（¥ 押下中 = ラベル）
.venv/bin/python -m aircontroller calibrate                      # 基準長・操作範囲・ピンチの閾値 → calibration.toml
.venv/bin/python -m aircontroller calibrate --pinch-only         # ピンチ・ピースの閾値と中立方向だけ（基準長・操作範囲は測らない）
.venv/bin/python -m aircontroller learn-thumbsup                 # いいねモードを上下左右 1回分の記録から学習 → calibration.toml
.venv/bin/python -m aircontroller learn-zoom                     # ズームモードを手招きの記録から学習 → calibration.toml
.venv/bin/python -m aircontroller lab --name lab01                # ジェスチャーの候補を試してラベル付きで記録
.venv/bin/python -m aircontroller lab-position                   # 位置ごとに カーソル・クリック・ピース を記録（集計は tools/position_report.py）
.venv/bin/python -m aircontroller lab-position --suite edge      # 右端・右下・下端への往復・静止・ピースで、意図しないピンチが出るかを記録
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

計測・分析（`tools/`、各ファイルの先頭に詳しい使い方）:
```bash
.venv/bin/python tools/monitor.py                                # run の負荷・メモリ・電池を記録（別のターミナルで）
.venv/bin/python tools/load_report.py logs/events-....jsonl logs/monitor-....csv --out logs/load-YYYYmmdd.md
.venv/bin/python tools/cpu_states.py                             # 30Hz / 1Hz / 取得だけ の CPU
.venv/bin/python tools/latency_ab.py                             # 記録の有無・手の数による遅延の差
.venv/bin/python tools/golden_diff.py --golden-config --set key=value   # 期待値との差（--write で書き直す）
.venv/bin/python tools/cursor_smoothing.py                       # カーソルの平滑化の係数の比較
.venv/bin/python tools/handedness_report.py                      # 左右ラベルの反転率
```

- 緊急停止: Control+Option+Command+ピリオド（ボタンを離して終了）
- 使う手は右手（`[gesture] hand`）。左手が映っていても、右手を出せば操作できる。オーバーレイに手のラベル（R / L、使わない手は ✕）を出す
- 操作中に右手が 5 秒見えないと自動でオフ。オフの間は推論を 1Hz に間引き、手のひらをカメラに向けてパーを 0.5 秒保つとオン（`[power]`）
- ¥ で印を付けると、前 6 秒・後 3 秒のランドマークを `recordings/mark-*.jsonl` に残し、イベントログにも書く
- イベントログ `logs/events-*.jsonl` は常に書く（20MB で次のファイル、30 本を超えたら古いものを消す。`--no-eventlog` で書かない）
- オフのまま 5 分経つとカメラを止める（止めている間はパーでオンにできない）。オーバーレイのカメラのボタンでも止められる（No signal）。ボタンか右 Command で再開。待機中に出る ▲ のボタンでオーバーレイを畳む（次回に引き継ぐ）
- 無効化すると、押下中のボタンとモードを解除する。Ctrl+C・終了時・例外時も解除する。

記録を再現テストに加える: `recordings/` から `tests/fixtures/` にコピーし、`python -m tests.make_golden tests/fixtures/xxx.jsonl` で期待値を作る。

## 権限（実行元のターミナルに付与）
システム設定 → プライバシーとセキュリティ で、次の3つをオンにする。
- カメラ
- 入力監視（キー監視）
- アクセシビリティ（イベント送出、キーの握りつぶし）
