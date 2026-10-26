"""パラメータ台帳（設定画面の土台、SPEC 13.43）。config.toml の全項目を 1 項目 1 定義で持つ。

画面の項目と測定ユニットは、ここから作る。既定値は config.toml にある（台帳には持たない）。
区分（2026-10-26、作者が確認）:
  A = 利用者が触る（好みの違いで、効果を体感で説明できる）。画面に常に出す
  B = 測定で決まる（体や手の違い）。測定ユニット（unit）が書く。画面では測定の値と並べる
  C = 詳細（変えると壊れやすい細部）。画面では折りたたむ
  D = 内部・環境の都合（キー、パス、ログ、カメラ）。画面に出さない
範囲（lo〜hi）と刻み（step）は画面のスライダー用。既定値は範囲の中で、刻みの格子の上にある（テストで確かめる）。
writers は calibration.toml / auto.toml にこの項目を書く経路（空なら config.toml だけ）。
position は位置依存の候補（SPEC 13.41 の (b)。第7節で閾値を取る場所を 1 か所にする）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Param:
    section: str
    key: str
    name: str  # 日本語名
    cls: str  # "A" / "B" / "C" / "D"
    lo: float | None = None  # 範囲（数値の項目だけ）
    hi: float | None = None
    step: float | None = None
    unit: str = ""
    desc: str = ""
    writers: tuple[str, ...] = ()  # "calibrate" / "learn-thumbsup" / "learn-zoom" / "run-auto"
    position: bool = False  # 位置依存の候補

    @property
    def id(self) -> str:
        return f"{self.section}.{self.key}"


P = Param
PARAMS: tuple[Param, ...] = (
    # [camera]
    P('camera', 'index', 'カメラ番号', 'D', None, None, None, '', '使うカメラ', ()),
    P('camera', 'width', '取得の幅', 'D', None, None, None, 'px', '', ()),
    P('camera', 'height', '取得の高さ', 'D', None, None, None, 'px', '', ()),
    P('camera', 'fps', '取得の fps', 'D', None, None, None, 'fps', '', ()),
    P('camera', 'backend', '取得経路', 'D', None, None, None, '', 'opencv / avf', ()),
    # [power]
    P('power', 'off_detect_hz', 'オフの間の推論頻度', 'C', 0.5, 5, 0.5, 'Hz', '上げると CPU が増える', ()),
    P('power', 'wake_enabled', 'パーでオン', 'A', None, None, None, '真偽', '話しながら手のひらを見せて誤ってオンになるなら切る', ()),
    P('power', 'wake_hold_s', 'パーを保つ時間', 'A', 0.2, 2.0, 0.1, '秒', '長いほど誤ってオンになりにくいが、オンにするのが遅い', ()),
    P('power', 'wake_probe_s', '確認の窓', 'C', 0.5, 5, 0.5, '秒', '', ()),
    P('power', 'wake_palm_max', 'パー: 手のひらの向き', 'C', -1.0, 0.0, 0.05, '-1..1', 'B の候補（測定ユニットなし）', ()),
    P('power', 'wake_open_min', 'パー: 指の真っすぐさ', 'C', 0.6, 1.0, 0.01, '0..1', 'B の候補', ()),
    P('power', 'wake_thumb_max', 'パー: 親指が開いている', 'C', -1.0, 0.5, 0.05, '親指の畳み', 'B の候補', ()),
    P('power', 'auto_off_after_s', '自動オフまでの時間', 'A', 0, 30, 1, '秒', '手が見えない時間。0 で無効', ()),
    P('power', 'idle_stop_s', 'カメラを止めるまでの時間', 'A', 0, 1800, 30, '秒', 'オフのままの時間。0 で無効', ()),
    # [tracker]
    P('tracker', 'model_path', 'モデルのパス', 'D', None, None, None, '', '', ()),
    P('tracker', 'num_hands', '同時に検出する手の数', 'D', None, None, None, '', '', ()),
    P('tracker', 'auto_two_hands', '違う手だけのとき 2 手で探す', 'C', None, None, None, '真偽', '', ()),
    P('tracker', 'two_hands_hz', '2 手で探す頻度', 'C', 0, 30, 1, 'Hz', '', ()),
    P('tracker', 'min_detection_confidence', '検出の信頼度', 'C', 0.1, 0.9, 0.05, '0..1', '', ()),
    P('tracker', 'min_presence_confidence', '存在の信頼度', 'C', 0.1, 0.9, 0.05, '0..1', '', ()),
    P('tracker', 'min_tracking_confidence', '追跡の信頼度', 'C', 0.1, 0.9, 0.05, '0..1', '', ()),
    # [activation]
    P('activation', 'method', '有効化の方式', 'D', None, None, None, '', 'toggle / hold', ()),
    P('activation', 'toggle_keys', '切り替えのキー', 'D', None, None, None, '', '', ()),
    P('activation', 'modifier_tap_ms', '右 Command の単独押しの上限', 'C', 100, 1000, 50, 'ms', '', ()),
    P('activation', 'mark_key', '印のキー', 'D', None, None, None, '', '', ()),
    P('activation', 'hold_key', 'hold のキー', 'D', None, None, None, '', '', ()),
    P('activation', 'swallow', 'キーを握りつぶす', 'D', None, None, None, '真偽', '', ()),
    # [eventlog]
    P('eventlog', 'enabled', 'イベントログ', 'D', None, None, None, '真偽', '', ()),
    P('eventlog', 'max_bytes', '1 ファイルの上限', 'D', None, None, None, 'バイト', '', ()),
    P('eventlog', 'max_files', '残すファイル数', 'D', None, None, None, '本', '', ()),
    P('eventlog', 'flush_s', '書き出しの間隔', 'D', None, None, None, '秒', '', ()),
    P('eventlog', 'status_s', '状態の行の間隔', 'D', None, None, None, '秒', '', ()),
    # [marks]
    P('marks', 'ring_s', '印: 持っておく長さ', 'D', None, None, None, '秒', '', ()),
    P('marks', 'pre_s', '印の前に残す長さ', 'D', None, None, None, '秒', '', ()),
    P('marks', 'post_s', '印の後に残す長さ', 'D', None, None, None, '秒', '', ()),
    # [safety]
    P('safety', 'emergency_keycode', '緊急停止のキー', 'D', None, None, None, '', '', ()),
    P('safety', 'emergency_mods', '緊急停止の修飾', 'D', None, None, None, '', '', ()),
    P('safety', 'timeout_s', '自動終了', 'D', None, None, None, '秒', '', ()),
    # [gesture]
    P('gesture', 'min_score', '手の信頼度の下限', 'C', 0.1, 0.9, 0.05, '0..1', '', ()),
    P('gesture', 'hand', '使う手', 'A', None, None, None, 'right/left/both', '再起動が要る（2 手の探索の方針を起動時に作る）', ()),
    P('gesture', 'hand_switch_frames', '別の手とみなすフレーム数', 'C', 1, 30, 1, 'フレーム', '', ()),
    P('gesture', 'hand_switch_jump', '別の手とみなす跳び', 'C', 0, 0.5, 0.01, '画像幅比', '', ()),
    P('gesture', 'hand_settle_frames', '跳びを問うまでのフレーム数', 'C', 0, 120, 5, 'フレーム', '', ()),
    P('gesture', 'acquire_hold_ms', '手の獲得までの時間', 'C', 0, 500, 10, 'ms', '', ()),
    P('gesture', 'lose_hold_ms', '見失いまでの時間', 'C', 50, 1000, 10, 'ms', '', ()),
    P('gesture', 'base_length_m', '基準長', 'D', 0.12, 0.25, 0.001, 'm', 'pinch_metric="world" のときだけ使う（今は image）。測定ユニットは作らない（2026-10-26 作者の判断）', ("calibrate",)),
    # [mode]
    P('mode', 'posture_hold_ms', '姿勢の成立時間', 'A', 40, 400, 10, 'ms', 'モードの切り替わりの速さ。短いと誤判定が増える', ()),
    P('mode', 'posture_release_ms', '姿勢の解除時間', 'C', 40, 400, 10, 'ms', '', ()),
    P('mode', 'index_up_enter', '指が立っている（入る）', 'C', 0.6, 0.98, 0.01, '真っすぐさ 0..1', '人差し指・中指（ピース）で共用。B の候補', (), True),
    P('mode', 'index_up_exit', '指が立っていない（抜ける）', 'C', 0.4, 0.95, 0.01, '真っすぐさ 0..1', '同上', (), True),
    # [click]
    P('click', 'pinch_metric', 'ピンチ距離の測り方', 'C', None, None, None, '', 'image / world', ()),
    P('click', 'pinch_on', 'ピンチの成立', 'B', 0.08, 0.4, 0.005, '手の大きさ比', 'U2。親指先-人差し指先 / 手首-中指付け根（画像上）', ("calibrate",), True),
    P('click', 'pinch_off', 'ピンチの解除', 'B', 0.1, 0.45, 0.005, '手の大きさ比', 'U2', ("calibrate",), True),
    P('click', 'click_min_ms', '最短のピンチ', 'C', 0, 150, 10, 'ms', '', ()),
    P('click', 'drag_ms', 'ドラッグに移る時間', 'A', 250, 1000, 25, 'ms', '短いとクリックのつもりがドラッグに', ()),
    P('click', 'drag_off', 'ドラッグ中の解除', 'B', 0.1, 0.5, 0.005, '手の大きさ比', 'U2', ("calibrate",), True),
    P('click', 'drag_release_ms', 'ドラッグを離すまでの時間', 'C', 0, 300, 10, 'ms', '', ()),
    P('click', 'drag_lost_release_ms', '見失ったドラッグを離す時間', 'C', 100, 2000, 50, 'ms', '', ()),
    P('click', 'right_on', '右クリックの成立', 'B', 0.08, 0.4, 0.005, '手の大きさ比', 'U3。親指先-中指先', ("calibrate",), True),
    P('click', 'right_off', '右クリックの解除', 'B', 0.1, 0.45, 0.005, '手の大きさ比', 'U3', ("calibrate",), True),
    P('click', 'right_hold_ms', '右クリックの待ち時間', 'A', 150, 1000, 25, 'ms', '付けたまま待つ時間', ()),
    P('click', 'right_arm', '右クリックの直前に離れている距離', 'B', 0.2, 0.6, 0.01, '手の大きさ比', 'U3（calibrate は 解除+0.08 以上にする）', ("calibrate",), True),
    P('click', 'right_arm_window_ms', '離れていた時刻の窓', 'C', 200, 1500, 50, 'ms', '', ()),
    P('click', 'calib_on_frac', '校正: 成立の位置', 'C', 0.1, 0.9, 0.05, '割合', '測定の計算のパラメータ', ()),
    P('click', 'calib_off_frac', '校正: 解除の位置', 'C', 0.1, 0.95, 0.05, '割合', '同上', ()),
    # [cursor]
    P('cursor', 'roi_cx', '操作範囲の中心 x', 'B', 0.0, 1.0, 0.005, '画像座標', 'U1', ("calibrate", "run-auto")),
    P('cursor', 'roi_cy', '操作範囲の中心 y', 'B', 0.0, 1.0, 0.005, '画像座標', 'U1', ("calibrate", "run-auto")),
    P('cursor', 'roi_width', '操作範囲の幅（移動倍率）', 'B', 0.15, 0.6, 0.005, '画像幅比', 'U1。小さいほど少ない動きで大きく動く。上限は roi_width_max', ("calibrate", "run-auto")),
    P('cursor', 'cursor_min_cutoff', 'カーソルの安定度', 'A', 0.1, 5.0, 0.1, 'Hz', '小さいほど揺れが減り、遅れが増える', ()),
    P('cursor', 'cursor_beta', 'カーソルの追従', 'A', 0.0, 0.1, 0.005, '', '大きいほど速い動きに追従', ()),
    P('cursor', 'cursor_d_cutoff', '速度の平滑化', 'C', 0.1, 5.0, 0.1, 'Hz', '', ()),
    P('cursor', 'roi_adapt', '操作範囲の自動補正', 'A', None, None, None, '真偽', '', ()),
    P('cursor', 'roi_adapt_tau_s', '自動補正の時定数', 'C', 0.1, 5.0, 0.1, '秒', '', ()),
    P('cursor', 'roi_adapt_shift', '自動補正: 位置の追従', 'A', 0.0, 1.0, 0.05, '割合', '', ()),
    P('cursor', 'roi_adapt_grow', '自動補正: 大きさの追従', 'A', 0.0, 0.5, 0.01, '割合', '', ()),
    P('cursor', 'roi_width_min', '操作範囲の幅の下限', 'C', 0.05, 0.4, 0.01, '画像幅比', '', ()),
    P('cursor', 'roi_width_max', '操作範囲の幅の上限', 'C', 0.3, 0.9, 0.01, '画像幅比', '画角の端で検出が途切れないための余白（13.36）', ()),
    # [zoom]
    P('zoom', 'zoom_enabled', 'ズーム', 'A', None, None, None, '真偽', '', ()),
    P('zoom', 'zoom_back_min', 'ズーム: 手の甲（入る）', 'C', 0.0, 1.0, 0.05, '-1..1', '', ()),
    P('zoom', 'zoom_stay_back_min', 'ズーム: 手の甲（保つ）', 'C', -0.5, 1.0, 0.05, '-1..1', '', ()),
    P('zoom', 'zoom_stay_open_min', 'ズーム: 指の真っすぐさ（保つ）', 'C', 0.4, 1.0, 0.01, '0..1', '', ()),
    P('zoom', 'zoom_open_min', 'ズーム: 指の真っすぐさ（入る）', 'C', 0.5, 1.0, 0.01, '0..1', '', ()),
    P('zoom', 'zoom_enter_hold_ms', 'ズームに入る時間', 'C', 100, 1000, 50, 'ms', '', ()),
    P('zoom', 'zoom_thumb_max', 'ズーム: 親指が開いている（入る）', 'C', -1.0, 0.5, 0.05, '親指の畳み', '', ()),
    P('zoom', 'zoom_stay_thumb_max', 'ズーム: 親指（保つ）', 'C', -1.0, 0.5, 0.05, '親指の畳み', '', ()),
    P('zoom', 'zoom_signal', 'ズームの測り方', 'B', None, None, None, 'depth/curl/length', 'U6', ("learn-zoom",)),
    P('zoom', 'zoom_sign', 'ズームの符号', 'B', None, None, None, '±1', 'U6', ("learn-zoom",)),
    P('zoom', 'zoom_dead', 'ズームの不感帯', 'B', 0.0, 0.5, 0.005, '測り方による', 'U6', ("learn-zoom",)),
    P('zoom', 'zoom_max', 'ズームの外端', 'B', 0.02, 1.0, 0.005, '測り方による', 'U6', ("learn-zoom",)),
    P('zoom', 'zoom_fire_level', 'ズームの発火の位置', 'A', 0.1, 1.0, 0.05, '棒の割合', '近すぎると誤って送る（0.25 は近すぎた）', ()),
    P('zoom', 'zoom_fire_hold_ms', 'ズームの発火までの時間', 'A', 0, 1000, 50, 'ms', '', ()),
    P('zoom', 'zoom_rearm_frac', 'ズームの再武装', 'C', 0.2, 2.0, 0.1, '不感帯の倍率', '', ()),
    P('zoom', 'zoom_min_cutoff', 'ズームの安定度', 'C', 0.1, 5.0, 0.1, 'Hz', '', ()),
    P('zoom', 'zoom_beta', 'ズームの追従', 'C', 0.0, 2.0, 0.05, '', '', ()),
    P('zoom', 'in_keycode', 'ズームインのキー', 'D', None, None, None, '', '', ()),
    P('zoom', 'in_mods', 'ズームインの修飾', 'D', None, None, None, '', '', ()),
    P('zoom', 'out_keycode', 'ズームアウトのキー', 'D', None, None, None, '', '', ()),
    P('zoom', 'out_mods', 'ズームアウトの修飾', 'D', None, None, None, '', '', ()),
    # [fist]
    P('fist', 'fist_enabled', 'グー（何もしない）', 'A', None, None, None, '真偽', '', ()),
    P('fist', 'fist_enter', 'グー（入る）', 'C', 0.3, 0.9, 0.01, '真っすぐさ 0..1', '', ()),
    P('fist', 'fist_exit', 'グー（抜ける）', 'C', 0.4, 0.95, 0.01, '真っすぐさ 0..1', '', ()),
    # [thumbsup]
    P('thumbsup', 'thumbsup_enabled', 'いいね', 'A', None, None, None, '真偽', '', ()),
    P('thumbsup', 'thumbsup_index_max', 'いいね: 人差し指を握る（入る）', 'C', 0.5, 1.0, 0.01, '0..1', '', ()),
    P('thumbsup', 'thumbsup_up_min', 'いいね: 親指が上（入る）', 'C', 0.0, 1.0, 0.05, '-1..1', '', ()),
    P('thumbsup', 'thumbsup_fold_max', 'いいね: 親指が外（入る）', 'C', -1.5, 0.5, 0.05, '親指の畳み', '', ()),
    P('thumbsup', 'thumbsup_thumb_far_min', 'いいね: 親指の離れ（入る）', 'B', 0.5, 2.0, 0.01, '手の大きさ比', 'U5', ("learn-thumbsup",)),
    P('thumbsup', 'thumbsup_stay_thumb_far_min', 'いいね: 親指の離れ（保つ）', 'B', 0.3, 2.0, 0.01, '手の大きさ比', 'U5', ("learn-thumbsup",)),
    P('thumbsup', 'thumbsup_stay_up_min', 'いいね: 親指が上（保つ）', 'C', -0.5, 1.0, 0.05, '-1..1', '', ()),
    P('thumbsup', 'thumbsup_stay_index_max', 'いいね: 人差し指（保つ）', 'C', 0.5, 1.0, 0.01, '0..1', '', ()),
    P('thumbsup', 'thumbsup_dead_disp', 'いいねの不感帯', 'B', 0.1, 1.5, 0.01, '手の大きさ比', 'U5', ("learn-thumbsup",)),
    P('thumbsup', 'thumbsup_max_disp', 'いいねの外周', 'B', 0.5, 4.0, 0.01, '手の大きさ比', 'U5（表示だけ）', ("learn-thumbsup",)),
    P('thumbsup', 'thumbsup_fire_left', 'いいねの発火: 左', 'B', 0.3, 3.0, 0.01, '手の大きさ比', 'U5', ("learn-thumbsup",)),
    P('thumbsup', 'thumbsup_fire_right', 'いいねの発火: 右', 'B', 0.3, 3.0, 0.01, '手の大きさ比', 'U5', ("learn-thumbsup",)),
    P('thumbsup', 'thumbsup_fire_up', 'いいねの発火: 上', 'B', 0.3, 3.0, 0.01, '手の大きさ比', 'U5', ("learn-thumbsup",)),
    P('thumbsup', 'thumbsup_rearm_disp', 'いいねの再武装', 'B', 0.05, 1.5, 0.01, '手の大きさ比', 'U5', ("learn-thumbsup",)),
    P('thumbsup', 'thumbsup_x_w_thumb', 'いいね左右: 親指の角度の重み', 'B', -2.0, 2.0, 0.25, '', 'U5（今は 0）', ("learn-thumbsup",)),
    P('thumbsup', 'thumbsup_x_w_knuckle', 'いいね左右: 付け根の角度の重み', 'B', -2.0, 2.0, 0.25, '', 'U5', ("learn-thumbsup",)),
    P('thumbsup', 'thumbsup_y_w_thumb', 'いいね上下: 親指の角度の重み', 'B', -2.0, 2.0, 0.25, '', 'U5', ("learn-thumbsup",)),
    P('thumbsup', 'thumbsup_y_w_knuckle', 'いいね上下: 付け根の角度の重み', 'B', -2.0, 2.0, 0.25, '', 'U5', ("learn-thumbsup",)),
    P('thumbsup', 'thumbsup_dwell_ms', 'いいねの発火までの時間', 'A', 100, 1500, 50, 'ms', '', ()),
    P('thumbsup', 'thumbsup_cooldown_ms', 'いいねのクールダウン', 'C', 0, 2000, 50, 'ms', '', ()),
    P('thumbsup', 'thumbsup_still_speed', 'いいね: 止まった判定の速さ', 'C', 0.1, 5.0, 0.1, '単位/秒', '', ()),
    P('thumbsup', 'thumbsup_still_ms', 'いいね: 正面を取り直す時間', 'C', 50, 1000, 50, 'ms', '', ()),
    P('thumbsup', 'thumbsup_center_follow_s', 'いいね: 正面の追従', 'C', 0, 10, 0.5, '秒', '0 で無効', ()),
    P('thumbsup', 'left_keycode', 'いいね左のキー', 'D', None, None, None, '', '', ()),
    P('thumbsup', 'left_mods', 'いいね左の修飾', 'D', None, None, None, '', '', ()),
    P('thumbsup', 'right_keycode', 'いいね右のキー', 'D', None, None, None, '', '', ()),
    P('thumbsup', 'right_mods', 'いいね右の修飾', 'D', None, None, None, '', '', ()),
    P('thumbsup', 'up_keycode', 'いいね上のキー', 'D', None, None, None, '', '', ()),
    P('thumbsup', 'up_mods', 'いいね上の修飾', 'D', None, None, None, '', '', ()),
    # [peace]
    P('peace', 'peace_enabled', 'ピース', 'A', None, None, None, '真偽', '', ()),
    P('peace', 'peace_method', 'ピースの方向の取り方', 'C', None, None, None, '', 'displacement / angle', ()),
    P('peace', 'thumb_fold_on', '親指の畳み（入る）', 'B', -0.5, 1.0, 0.005, '親指の畳み', 'U4', ("calibrate",), True),
    P('peace', 'thumb_fold_off', '親指の畳み（抜ける）', 'B', -0.5, 1.0, 0.005, '親指の畳み', 'U4', ("calibrate",), True),
    P('peace', 'ring_down_max', 'ピース: 薬指を畳む', 'C', 0.5, 1.0, 0.01, '真っすぐさ 0..1', '', (), True),
    P('peace', 'peace_exit_straight', 'ピースを保つ指の真っすぐさ', 'C', 0.3, 0.9, 0.01, '真っすぐさ 0..1', '右下でピースが抜けにくい件（13.41）', (), True),
    P('peace', 'peace_neutral_yaw_deg', 'ピースの中立: 方位角', 'B', -90, 90, 0.5, '度', 'U4。angle のときだけ使う（今は displacement）', ("calibrate",)),
    P('peace', 'peace_neutral_pitch_deg', 'ピースの中立: 仰角', 'B', -90, 90, 0.5, '度', '同上', ("calibrate",)),
    P('peace', 'peace_dead_deg', '不感帯（角度）', 'C', 0, 45, 1, '度', 'angle のときだけ', ()),
    P('peace', 'peace_max_deg', '最大速度の角度', 'C', 10, 90, 1, '度', 'angle のときだけ', ()),
    P('peace', 'peace_fire_deg', '戻る・進むの角度', 'C', 5, 90, 1, '度', 'angle のときだけ', ()),
    P('peace', 'peace_rearm_deg', '再武装の角度', 'C', 0, 45, 1, '度', 'angle のときだけ', ()),
    P('peace', 'peace_dead_disp', 'スクロールの不感帯', 'A', 0.05, 1.0, 0.01, '手の大きさ比', '', ()),
    P('peace', 'peace_max_disp', '最大速度になる変位', 'A', 0.3, 3.0, 0.05, '手の大きさ比', '', ()),
    P('peace', 'peace_fire_disp', '戻る・進むの変位', 'A', 0.2, 2.0, 0.05, '手の大きさ比', '', ()),
    P('peace', 'peace_rearm_disp', '戻る・進むの再武装', 'C', 0.05, 1.0, 0.01, '手の大きさ比', '', ()),
    P('peace', 'peace_min_cutoff', 'スクロールの安定度', 'A', 0.1, 5.0, 0.1, 'Hz', 'いいねの方向の平滑化にも使う', ()),
    P('peace', 'peace_beta', 'スクロールの追従', 'C', 0.0, 0.5, 0.005, '', 'いいねにも使う', ()),
    P('peace', 'scroll_max_speed', 'スクロールの最大速度', 'A', 200, 6000, 100, 'px/秒', '', ()),
    P('peace', 'scroll_exponent', 'スクロールの速度カーブ', 'A', 1.0, 4.0, 0.1, '指数', '大きいほど中央付近が遅い', ()),
    P('peace', 'scroll_invert', 'スクロールの反転', 'A', None, None, None, '真偽', '', ()),
    P('peace', 'nav_dwell_ms', '戻る・進むの発火までの時間', 'A', 0, 1500, 50, 'ms', '', ()),
    P('peace', 'nav_cooldown_ms', '戻る・進むのクールダウン', 'C', 0, 2000, 50, 'ms', '', ()),
    P('peace', 'nav_swap', '戻る・進むの向き', 'A', None, None, None, '真偽', 'true: 手を左へ → 進む', ()),
    P('peace', 'back_keycode', '戻るのキー', 'D', None, None, None, '', '', ()),
    P('peace', 'back_mods', '戻るの修飾', 'D', None, None, None, '', '', ()),
    P('peace', 'forward_keycode', '進むのキー', 'D', None, None, None, '', '', ()),
    P('peace', 'forward_mods', '進むの修飾', 'D', None, None, None, '', '', ()),
    # [log]
    P('log', 'dir', 'ログの場所', 'D', None, None, None, '', '', ()),
    P('log', 'recordings_dir', '記録の場所', 'D', None, None, None, '', '', ()),
)

BY_ID: dict[str, Param] = {p.id: p for p in PARAMS}


def get(section: str, key: str) -> Param:
    return BY_ID[f"{section}.{key}"]
