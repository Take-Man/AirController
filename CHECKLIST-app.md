# AirController.app の実機確認（TASKS-20261027-package 第5節、SPEC 13.59）

`pkg/dist/AirController.dmg`（ダウンロードしたときと同じ隔離の属性つき）と `pkg/dist/AirController.app`。作り直しは `pkg/build.sh app` → `pkg/build.sh dmg`。
データ（設定・ログ・記録）は `~/Library/Application Support/AirController/`（ソースから動かすときのデータとは別）。

## 0. 準備
- ターミナルから動かしているアプリ・run があれば、終了しておく（⌘Q、Ctrl+C）。
- （任意）今の設定を使うなら、初回に開く前に、今の calibration.toml を写す（写さなければ、既定の値で動く。設定画面の「測定」で測り直せる）:
  ```bash
  mkdir -p ~/Library/Application\ Support/AirController && cp calibration.toml ~/Library/Application\ Support/AirController/
  ```

## 1. 入れる（.dmg から）
1. `pkg/dist/AirController.dmg` を開き、AirController を Applications へドラッグする。
2. Applications の AirController を開く → 「開いていません」の警告 →「完了」→ システム設定 → プライバシーとセキュリティ の「このまま開く」。

## 2. 確かめること
| # | 確かめること | 結果 |
|---|---|---|
| 1 | ホームが出る（Dock に AirController のアイコン） | |
| 2 | 環境のチェック: 「実行元」が AirController.app。足りない権限の案内が出る | |
| 3 | 権限（アクセシビリティ・入力監視・カメラ）の一覧に「AirController」として載る。オンにした後、開き直す | |
| 4 | ホームの「開始」で run が動く（オーバーレイ・右 Command・カーソル・クリック）。**子プロセスの run に権限が付くか**（いちばん大事） | |
| 5 | 環境のチェックがすべて合格（印のキーの握りつぶしを含む） | |
| 6 | 設定のウィンドウ、ライブ（操作に反映する）、測定（calibrate の窓が出るか） | |
| 7 | ギアのボタンで設定のウィンドウが出る。ウィンドウを閉じても run が続き、⌘Q で run も止まる | |
| 8 | 出たダイアログ（ローカルネットワークなど）の文言と、選んだもの | |

うまく動かないときは、`~/Library/Application Support/AirController/logs/`（app-*.log、run-app-*.log）を見る（私に渡してもよい）。
