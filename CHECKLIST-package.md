# パッケージ化の試作の実機確認（TASKS-20261027-package 第3節）

試作の .app は 2 つ（中身は同じ `run`。設定・ログは `~/Library/Application Support/AirController-probe/`）。
- `pkg/dist/AirController Probe (PyInstaller).app`（bundle id `com.takeman.aircontroller.probe`）
- `pkg/dist/AirController Probe py2app.app`（bundle id `com.takeman.aircontroller.probe.py2app`）

どちらも ad hoc 署名（Apple のアカウントなし）。Dock には出ない。終了は Control+Option+Command+ピリオド。
初回は、同梱の config.toml・calibration.toml（今の設定）を上の場所に写して使う。出力は `.../AirController-probe/logs/app-*.log`。

## A. 権限が .app に付くか（PyInstaller 版）
1. Finder で `pkg/dist` を開き、`AirController Probe (PyInstaller)` をダブルクリックする（ターミナルからは開かない）。
2. 出るダイアログを記録する: カメラの許可は「AirController Probe」の名前で出るか。入力監視・アクセシビリティは、
   システム設定の一覧に「AirController Probe」として載るか（ターミナルではなく）。
3. 権限が足りないと、ダイアログ「終了しました（コード …）」が出て終わる。システム設定 → プライバシーとセキュリティ の
   カメラ・入力監視・アクセシビリティで「AirController Probe」をオンにして、もう一度開く。
4. オーバーレイが出たら、右 Command でオン → カーソル、クリック、¥（文字が入力されないこと）を試す。

| 確かめること | 結果 |
|---|---|
| カメラのダイアログに出た名前 | |
| 入力監視の一覧に載った名前 | |
| アクセシビリティの一覧に載った名前 | |
| ターミナルの権限を使っていないか（一覧のターミナルはそのまま） | |
| オーバーレイ・カーソル・クリック | |
| ¥ が入力されない（キーの握りつぶし） | |
| 開いてからオーバーレイが出るまでの時間（体感） | |

## B. 遅延と CPU（ターミナルとの比較）
同じ場所・明るさで、それぞれ 3 分ほど、オンにしてカーソルとクリックを続ける（時刻をメモ）。
1. .app で 3 分（A の後そのまま）。終わったら Control+Option+Command+ピリオド。
2. ターミナルで `.venv/bin/python -m aircontroller run` で 3 分。
比べるのは私が行う（両方のイベントログの 10 秒ごとの状態の行: 遅延 p95・CPU）。

## C. 作り直したときに付与が残るか
A・B の後に、私が PyInstaller 版を作り直す（`pkg/build.sh pyinstaller`。ad hoc 署名なので、署名の中身〔cdhash〕が変わる）。
その後に .app を開き、権限のダイアログが出直すか、システム設定でオンのままでも効かないか、をメモする。

## D. （任意）py2app 版
A の 1〜4 を `AirController Probe py2app` で行う（別の .app として、別に権限を求めるはず）。

## E. .dmg とダウンロードしたときの警告（第8節の前提）
`pkg/dist/AirControllerProbe-pyinstaller.dmg` には、ダウンロードしたときと同じ隔離の属性（quarantine）を付けてある。
1. .dmg をダブルクリックして開き、中の .app を、デスクトップなど書き込める場所へドラッグする（/Applications でもよい）。
2. コピーした .app をダブルクリックする。出た警告の文言と、出たボタン（「ゴミ箱に入れる」「完了」など）を記録する。
3. 「完了」を押し、システム設定 → プライバシーとセキュリティ の下の方に出る「このまま開く」を押す。もう一度確認が出たら「このまま開く」。
4. 起動できたか、権限を求め直されたか（A と同じ bundle id・同じ署名なら、そのまま使えるかもしれない）を記録する。

| 確かめること | 結果 |
|---|---|
| 警告の文言とボタン | |
| 「このまま開く」が出たか、押して起動できたか | |
| 2 回目からは警告なしで開けるか | |
| 権限を求め直されたか | |

## 片付け（確認の後）
試作の .app と .dmg は `pkg/dist/`、データは `~/Library/Application Support/AirController-probe/`。消すときは私が確認を取ってから消す。
システム設定の権限の一覧に残った「AirController Probe」は、一覧で選んで「−」で外せる（作者の操作）。
