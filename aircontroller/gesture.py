"""ジェスチャー層。

LandmarkFrame の列を受け取り、Event の列を返す状態機械。
時刻はフレームの t_capture_ns だけを使い、壁時計・カメラ・OS に依存しない。
有効化の状態もフレームの active として入力されるので、記録したランドマークを流し込めば、
同じイベント列が再現される。

段階1: 手の獲得・見失い、カーソルモード（人差し指が立っている）、手のひら中心の絶対マッピング、
One Euro Filter、親指と人差し指の短いピンチでのクリック。
段階2: 長いピンチでのドラッグ、親指と中指を付けたまま待つ右クリック（段階4で変更）、ドラッグ中に手を見失ったときの解除。
段階4: ピースモード（peace.py）。スクロールと、戻る・進む。グーは何もしない。
段階5: いいねモード（peace.py の ThumbsUpController）。デスクトップ移動、Mission Control。
段階6: ズームモード（zoom.py）。手の甲を向け、指先の前後でズームイン・アウト。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

import numpy as np

from .filters import OneEuroFilter2D
from .peace import PeaceController, ThumbsUpController
from .zoom import ZoomController
from .types import (INDEX_MCP, INDEX_TIP, MIDDLE_MCP, MIDDLE_TIP, PALM, PINKY_MCP, RING_MCP, THUMB_MCP, THUMB_TIP,
                    WRIST, Event, Hand, LandmarkFrame)


@dataclass
class GestureConfig:
    # 検出
    min_score: float = 0.5  # これ未満の信頼度の手は「検出なし」として扱う（イベントを出さない）
    # 使う手（TASKS-20261025 2.5）: "right" / "left" / "both"。MediaPipe の左右ラベルで選び、別のラベルの手は「手がない」と
    # 同じに扱う。ラベルは一時的に反転する（記録では 1〜3 フレームのものが多い）ので、別のラベルが hand_switch_frames
    # フレーム続いたときだけ別の手とみなす。ピンチ・ドラッグ・右クリック待ち・ピース・いいね・ズームの途中では切り替えない。
    # 既定値は互換のため "both"。config.toml で "right" にしている
    # さらに、ラベルが変わった瞬間に手のひら中心が hand_switch_jump（画像の幅に対する比）以上跳んだときだけ別の手とみなす。
    # 位置が続いたままラベルだけが変わるのは、同じ手の誤判定（記録では跳びは 0.08 以下で、8〜175 フレーム続くこともあった）。
    # 別の手が映ったときは 0.3〜0.45 跳んでいた。0 で無効（フレーム数だけで決める）
    hand: str = "both"
    hand_switch_frames: int = 5
    hand_switch_jump: float = 0.15
    # 跳びの条件で守るのは、hand_settle_frames フレーム以上続いたラベルだけ。手が入ってきた直後は、画像の端などで
    # ラベルを誤ることがある（use03: 端から入った右手の最初の 4 フレームが Left）ので、フレーム数だけで切り替える
    hand_settle_frames: int = 30
    acquire_hold_ms: float = 120.0  # 検出がこの時間続いたら手の獲得
    lose_hold_ms: float = 150.0  # 未検出がこの時間続いたら見失い
    # 姿勢（モード）
    posture_hold_ms: float = 120.0  # 姿勢がこの時間続いたら成立
    posture_release_ms: float = 150.0  # 姿勢が崩れてこの時間続いたら解除
    index_up_enter: float = 0.85  # 人差し指の真っすぐさ（付け根→先の直線距離 / 関節の長さの和）がこれを超えたら「立っている」
    index_up_exit: float = 0.70  # これを下回ったら「立っていない」（ヒステリシス）
    # 基準長とピンチ
    base_length_m: float = 0.19  # 手を開いた状態の手首→中指先（world）。pinch_metric="world" のとき使う。段階3で校正する
    # ピンチ距離の測り方。"image": 画像座標の距離 / 画像上の手首→中指付け根（既定）、"world": world 距離 / base_length_m
    # world は手が横向きのとき、親指が人差し指に隠れて距離が縮まらない（pinch01 の検証で 14 回中 0 回しか成立しなかった）
    pinch_metric: str = "image"
    pinch_on: float = 0.15  # 成立の閾値（上の比）
    pinch_off: float = 0.22  # 解除の閾値
    click_min_ms: float = 40.0  # これより短いピンチはブレとして無視
    # 右クリック（親指と中指）は「離れていた指を近づける動作」だけを成立させる。中指との距離が right_arm を
    # 超えた状態を、直前 right_arm_window_ms 以内（有効化中）に見ていることを条件にする。さらに、
    # その瞬間の親指と人差し指が pinch_off より離れていること（親指が両方の間にある曖昧な姿勢を除く）。
    # 親指を曲げた中指に乗せて休ませる姿勢や、左クリックのつもりの動作が、右クリックに誤認されるのを防ぐ。
    # 左クリックは、有効化後に親指と人差し指が pinch_off より離れたのを一度見ていればよい（段階1と同じ）
    right_on: float = 0.15  # 親指と中指のピンチの成立・解除の閾値（左とは別に持つ）
    right_off: float = 0.22
    right_arm: float = 0.35
    right_arm_window_ms: float = 600.0
    drag_ms: float = 500.0  # 親指と人差し指のピンチがこれを超えたらドラッグに移行（ボタンを押す）
    drag_lost_release_ms: float = 500.0  # ドラッグ中に手を見失って、この時間戻らなければボタンを離す
    # ドラッグ中は離したと判定しにくくする: 解除の閾値を上げ、閾値を超えた状態がこの時間続いたら離す。
    # ドラッグ中のピンチ距離は 0.15〜0.20 付近を漂い、pinch_off（0.22）を一瞬超えて意図せず離れていた（use03, use04）
    drag_off: float = 0.26
    drag_release_ms: float = 100.0
    right_hold_ms: float = 400.0  # 親指と中指を付けたまま、この時間待つと右クリック（段階4で長押し方式に変更、SPEC 13.15）
    # ズームモード（手の甲をカメラに向けて指を伸ばす。SPEC 13.20）: 手招きのように指先を遠ざけるとズームイン、近づけるとアウト
    zoom_enabled: bool = False  # 既定値は互換のため False。config.toml で有効にする
    zoom_back_min: float = 0.50  # 入るとき: 手の甲の向き（palm_facing、手の甲がカメラを向くと +1）がこれを超える
    zoom_stay_back_min: float = 0.20  # 入った後は、これを超え、
    zoom_stay_open_min: float = 0.66  # 4本の指の真っすぐさの平均がこれを超えていれば保つ。グーにすると抜ける
    # （zoom01 では、グーにしても手の甲が 0.33 でカメラを向いたままで、抜けにくかった。手招きは 0.71 以上、グーは 0.51 前後）
    zoom_open_min: float = 0.80  # 入るとき: 人差し指・中指・薬指の真っすぐさがすべてこれを超える
    zoom_enter_hold_ms: float = 300.0  # この時間保って入る（ほかの動作の途中で入らないように）
    # 親指が開いている（親指先が手のひらの外、人差し指側へ出ている）こと。thumb_fold_metric がこれ未満。グーでは親指が
    # 握った指の上に来るので、握りかけ（指の真っすぐさが手招きと区別できない）でも抜けられる（作者の提案）。
    # learn-zoom: ズームの手 -0.48〜-0.40、zoom01/02 のグー 中央値 +0.12
    zoom_thumb_max: float = -0.30  # 入るとき
    zoom_stay_thumb_max: float = -0.10  # 入った後（手招きで指を曲げると親指も内側へ寄るので、少し甘くする）
    zoom_signal: str = "depth"  # 指先の前後の測り方（zoom.py）: "depth" / "curl" / "length"。learn-zoom で選ぶ
    zoom_sign: float = 1.0  # ズームインが正になるように掛ける符号
    zoom_dead: float = 0.15  # 入ったときからの変化の不感帯
    zoom_max: float = 0.50  # 棒の外端になる変化
    # 1回の超過で1段（TASKS-20261025 2.1）: 棒の長さ（0 = 不感帯の端、1 = zoom_max）で zoom_fire_level を超え、
    # zoom_fire_hold_ms 保ったら 1 段送る。変化が zoom_dead × zoom_rearm_frac の内側に戻るまで次を送らない
    zoom_fire_level: float = 0.5
    zoom_fire_hold_ms: float = 300.0
    zoom_rearm_frac: float = 1.0
    zoom_min_cutoff: float = 1.0  # One Euro Filter（安定度）
    zoom_beta: float = 0.5
    # グー（握りこぶし）: 何もしない状態。カーソル移動・クリック・スクロールをすべて止める（作者の判断、SPEC 13.13）
    fist_enabled: bool = False  # 既定値は互換のため False。config.toml で有効にする
    fist_enter: float = 0.60  # 4本の指の真っすぐさがすべてこれ未満で「握った」
    fist_exit: float = 0.75  # どれかの指がこれを超えたら「握っていない」
    # いいねモード（親指を立て、ほかの指を握る。SPEC 13.16）: 左右 = デスクトップ移動、上 = Mission Control
    thumbsup_enabled: bool = False  # 既定値は互換のため False。config.toml で有効にする
    thumbsup_index_max: float = 0.80  # 入るとき: 人差し指の真っすぐさがこれ未満（握っている）
    thumbsup_up_min: float = 0.60  # 入るとき: 親指の上向き成分（画像上、-1..1）がこれを超える
    thumbsup_fold_max: float = -0.30  # 入るとき: 親指の位置（thumb_fold_metric）がこれ未満（手のひらの外へ出ている）
    thumbsup_thumb_far_min: float = 1.20  # 入るとき: 親指先が手のひら中心から十分離れている（画像上の距離 / 手首→中指付け根）。
    # 親指が見えないグー（小指側をカメラに向けた）の誤認を防ぐ。lab01: いいね 1.12〜2.69、グー 0.76〜1.27
    thumbsup_stay_thumb_far_min: float = 0.90  # 入った後は、これを超えていれば保つ
    thumbsup_stay_up_min: float = 0.50  # 入った後: 親指の上向き成分がこれを超え、
    thumbsup_stay_index_max: float = 0.90  # 人差し指の真っすぐさがこれ未満なら保つ（下へ動かすと指が伸びて見える: lab01）
    thumbsup_dead_disp: float = 0.50  # 手のひら中心の変位（単位: 画像上の手首→中指付け根の長さ）の不感帯
    thumbsup_max_disp: float = 2.00  # オーバーレイの外周
    thumbsup_fire_left: float = 1.20  # 発火の計時を始める変位（左）。learn-thumbsup で利用者に合わせる
    thumbsup_fire_right: float = 1.20  # 同（右）
    thumbsup_fire_up: float = 1.00  # 同（上）
    thumbsup_rearm_disp: float = 0.30
    # 回転の寄与: 方向の量 = 手のひら中心の変位 + 重み × 角度の変化（度）/ 45。手を動かす代わりに、ひねっても操作できる。
    # thumb = 親指（付け根→先）の画像上の角度、knuckle = 人差し指付け根→小指付け根の角度。learn-thumbsup で決める
    thumbsup_x_w_thumb: float = 0.0
    thumbsup_x_w_knuckle: float = 0.0
    thumbsup_y_w_thumb: float = 0.0
    thumbsup_y_w_knuckle: float = 0.0  # これより中央に戻ったら、次の発火を許す
    thumbsup_dwell_ms: float = 400.0  # 領域にこの時間とどまったら発火
    thumbsup_cooldown_ms: float = 600.0
    # 発火した後、手が動き出してから次に止まった場所を、新しい正面にする（元の位置に正確に戻らなくても次を発火できる）
    thumbsup_still_speed: float = 1.5  # 方向の量の変化がこれ（単位/秒）未満なら「止まっている」
    thumbsup_still_ms: float = 250.0  # 止まった状態がこの時間続いたら、正面を取り直す
    thumbsup_center_follow_s: float = 2.0  # 不感帯の中にいる間、正面がこの時定数で手に追従する（姿勢のずれを吸収。0 で無効）
    # ピースモード（人差し指と中指が立ち、薬指は畳み、親指を畳んだ姿勢）
    peace_enabled: bool = False  # 既定値は互換のため False。config.toml で有効にする
    peace_exit_straight: float = 0.55  # ピースモード中は、人差し指か中指の真っすぐさがこれを下回るまで保つ
    ring_down_max: float = 0.88  # 薬指の真っすぐさがこれ未満なら「畳んでいる」（3本指モードと区別する。peace01 では 0.66〜0.92）
    thumb_fold_on: float = 0.25  # 親指先の位置（thumb_fold_metric）がこれを超えたら「親指を畳んだ」。校正で決める
    thumb_fold_off: float = 0.15  # これを下回ったら「畳んでいない」
    peace_method: str = "angle"  # 指先の向きの取り方: "angle"（world の方位角・仰角）/ "displacement"（入ったときからの指先の変位）
    nav_dwell_ms: float = 0.0  # 戻る・進むの領域にこの時間とどまったら発火する（0 なら入った瞬間）
    peace_neutral_yaw_deg: float = 0.0  # 中立方向（校正で測る）。利用者から見て右が正
    peace_neutral_pitch_deg: float = 0.0  # 上が正
    peace_dead_deg: float = 12.0  # 中央の不感帯（角度方式）
    peace_max_deg: float = 40.0  # スクロールが最大速度になる角度
    peace_fire_deg: float = 25.0  # 戻る・進むが発火する角度
    peace_rearm_deg: float = 10.0  # これより中央に戻ったら、次の発火を許す
    peace_dead_disp: float = 0.30  # 同じく変位方式（単位: 画像上の手首→中指付け根の長さ）
    peace_max_disp: float = 1.20
    peace_fire_disp: float = 0.50
    peace_rearm_disp: float = 0.25
    peace_min_cutoff: float = 1.0  # 指先の向きの One Euro Filter（スクロールの安定度）
    peace_beta: float = 0.02
    scroll_max_speed: float = 2000.0  # スクロールの最大速度（px/秒、移動倍率）
    scroll_exponent: float = 1.8  # 速度カーブの指数（大きいほど中央付近が遅く、端が速い）
    scroll_invert: bool = False  # スクロール方向の反転
    nav_cooldown_ms: float = 500.0  # 戻る・進むの、連続発火を防ぐ最短間隔
    # 戻る・進むの向き。False: 手を左へ → 戻る、右へ → 進む（以前）。True: 左へ → 進む、右へ → 戻る（TASKS-20261025 2.4）。
    # 既定値は互換のため False。config.toml で True にしている
    nav_swap: bool = False
    # カーソル（絶対座標マッピング）
    screen_w: float = 1440.0
    screen_h: float = 900.0
    roi_cx: float = 0.5  # 操作範囲の中心（画像座標 0..1）
    roi_cy: float = 0.5
    roi_width: float = 0.6  # 操作範囲の幅（画像幅に対する比）。高さは画面のアスペクト比に合わせる
    cursor_min_cutoff: float = 1.0  # One Euro Filter（単位: 画面ピクセル）
    cursor_beta: float = 0.01
    cursor_d_cutoff: float = 1.0
    # 操作範囲の自動補正: カーソル操作中に手のひら中心が操作範囲の外へ出たら、範囲を少しずつそちらへ広げ・ずらす。
    # 外へ出た量 e に対し、時定数 roi_adapt_tau_s で、中心を roi_adapt_shift*e、幅を roi_adapt_grow*|e| だけ動かす
    # （既定値では、超えた側の端が 0.75e 外へ、反対側の端が 0.25e 同じ向きへ動く）
    roi_adapt: bool = False  # 既定値は互換のため False。config.toml で有効にする
    roi_adapt_tau_s: float = 0.5
    roi_adapt_shift: float = 0.5
    roi_adapt_grow: float = 0.5
    roi_width_min: float = 0.15
    roi_width_max: float = 0.9

    @classmethod
    def from_toml(cls, cfg: dict, screen_size: tuple[float, float] | None = None) -> "GestureConfig":
        names = {f.name for f in fields(cls)}
        merged: dict = {}
        for section in ("gesture", "mode", "click", "cursor", "peace", "fist", "thumbsup", "zoom"):
            merged.update({k: v for k, v in cfg.get(section, {}).items() if k in names})
        if screen_size:
            merged["screen_w"], merged["screen_h"] = screen_size
        return cls(**merged)


@dataclass
class GestureState:
    """オーバーレイ表示用のスナップショット（イベントではない）。"""

    t_ns: int = 0
    active: bool = False
    hand_present: bool = False
    mode: str = "idle"  # idle / cursor / pinch（左、ドラッグ前）/ drag / right（親指と中指）/ peace / thumbsup / zoom / fist
    score: float = 0.0
    palm: tuple[float, float] | None = None  # 画像座標（非反転）
    landmarks: np.ndarray | None = None
    cursor: tuple[float, float] | None = None  # 画面ピクセル
    pinch_n: float = float("nan")
    pinch_progress: float = 0.0  # ピンチの経過 / drag_ms
    peace: object | None = None  # ピースモード中は PeaceView
    zoom_level: float = 0.0  # ズームモード中の -1..1（ズームインが正）
    zoom_progress: float = 0.0  # 発火までの経過（0..1）
    zoom_armed: bool = True  # 中央に戻って、次の段を送れる状態か
    zoom_fire_ns: int | None = None  # 直近に段を送った時刻（オーバーレイで、棒の色を一瞬変える）
    zoom_fire_dir: int = 0


def preferred_hand(hands, hand: str = "both", min_score: float = 0.0) -> Hand | None:
    """複数の手が映っているとき、設定の手（hand: "right" / "left"）のラベルの手を優先し、その中で信頼度が最も高い手を返す。
    そのラベルの手が無ければ、信頼度が最も高い手（校正・学習・表示用。操作に使う手は GestureEngine が選ぶ）。"""
    hs = [h for h in hands if h.score >= min_score]
    if not hs:
        return None
    want = {"right": "Right", "left": "Left"}.get(hand)
    pref = [h for h in hs if h.handedness == want]
    return max(pref or hs, key=lambda h: h.score)


def thumb_fold_metric(hand: Hand) -> float:
    """親指先の、人差し指付け根→小指付け根の軸上の位置（world）。0 = 人差し指の付け根、1 = 小指の付け根。
    親指を手のひらの上に畳むと正（小指側）へ、立てたり開いたりすると負（親指側）へ動く。
    画像上の距離（親指先→薬指付け根）は、ピースとカーソル操作の姿勢で分布が重なっていた（use04）。"""
    w = hand.world
    axis = w[PINKY_MCP] - w[INDEX_MCP]
    d = float(np.dot(axis, axis))
    return float(np.dot(w[THUMB_TIP] - w[INDEX_MCP], axis)) / d if d > 0 else 0.0


def thumb_up(hand: Hand) -> float:
    """親指（付け根 MCP → 先）の向きの、画像上の上向き成分（-1..1）。いいねで +1 に近い。"""
    p = hand.landmarks[:, :2]
    v = p[THUMB_TIP] - p[THUMB_MCP]
    n = float(np.linalg.norm(v))
    return float(-v[1] / n) if n > 0 else 0.0


def palm_facing(hand: Hand) -> float:
    """手の甲がカメラを向くと +1、手のひらがカメラを向くと −1（world の手のひらの法線の z 成分。左右の手でそろえる）。
    lab01: 手の甲 +0.71、手のひら −0.91。"""
    w = hand.world
    n = np.cross(w[INDEX_MCP] - w[WRIST], w[PINKY_MCP] - w[WRIST])
    nn = float(np.linalg.norm(n))
    if nn == 0:
        return 0.0
    z = float(n[2] / nn)
    return z if hand.handedness == "Right" else -z


def thumb_far(hand: Hand, image_size: tuple[int, int]) -> float:
    """親指先と手のひら中心の画像上の距離 / 画像上の手首→中指付け根。いいねでは大きく、親指が隠れたグーでは小さい。"""
    p = hand.landmarks[:, :2] * np.asarray(image_size, dtype=np.float64)
    ref = float(np.linalg.norm(p[MIDDLE_MCP] - p[WRIST]))
    return float(np.linalg.norm(p[THUMB_TIP] - p[list(PALM)].mean(axis=0))) / ref if ref > 0 else 0.0


def finger_straightness(world: np.ndarray, mcp: int) -> float:
    p = world[mcp:mcp + 4]
    seg = float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())
    return float(np.linalg.norm(p[3] - p[0])) / seg if seg > 0 else 0.0


def image_pinch_metrics(hand: Hand, image_size: tuple[int, int]) -> dict:
    """画像座標（ピクセル）でのピンチ距離を、画像上の手首→中指付け根の長さで割る。"""
    p = hand.landmarks[:, :2] * np.asarray(image_size, dtype=np.float64)
    ref = float(np.linalg.norm(p[MIDDLE_MCP] - p[WRIST]))
    ti = float(np.linalg.norm(p[THUMB_TIP] - p[INDEX_TIP]))
    tm = float(np.linalg.norm(p[THUMB_TIP] - p[MIDDLE_TIP]))
    return {"thumb_index_n": ti / ref if ref > 0 else float("nan"),
            "thumb_middle_n": tm / ref if ref > 0 else float("nan")}


def pinch_metrics(hand: Hand, base_length_m: float | None = None) -> dict:
    """world landmarks でのピンチ距離。base_length_m を省くと手首→中指付け根（剛体長）で割る。"""
    w = hand.world
    ref = float(np.linalg.norm(w[MIDDLE_MCP] - w[WRIST]))
    base = base_length_m or ref
    ti = float(np.linalg.norm(w[THUMB_TIP] - w[INDEX_TIP]))
    tm = float(np.linalg.norm(w[THUMB_TIP] - w[MIDDLE_TIP]))
    return {"ref_m": ref, "thumb_index_m": ti, "thumb_middle_m": tm,
            "thumb_index_n": ti / base if base > 0 else float("nan"),
            "thumb_middle_n": tm / base if base > 0 else float("nan")}


class Debounce:
    """真偽値を、成立に hold_on、解除に hold_off の保持時間を要求して安定させる。"""

    def __init__(self, hold_on_ms: float, hold_off_ms: float):
        self.hold_on_ns = hold_on_ms * 1e6
        self.hold_off_ns = hold_off_ms * 1e6
        self.reset()

    def reset(self) -> None:
        self.value = False
        self._since: int | None = None

    def update(self, raw: bool, t: int) -> bool:
        """値が切り替わったフレームで True を返す。"""
        if raw == self.value:
            self._since = None
            return False
        if self._since is None:
            self._since = t
        if t - self._since >= (self.hold_on_ns if raw else self.hold_off_ns):
            self.value = raw
            self._since = None
            return True
        return False


class GestureEngine:
    def __init__(self, config: GestureConfig | None = None):
        self.cfg = config or GestureConfig()
        c = self.cfg
        self._presence = Debounce(c.acquire_hold_ms, c.lose_hold_ms)
        self._cursor_posture = Debounce(c.posture_hold_ms, c.posture_release_ms)
        self._peace_posture = Debounce(c.posture_hold_ms, c.posture_release_ms)
        self._fist_posture = Debounce(c.posture_hold_ms, c.posture_release_ms)
        self._peace = PeaceController(c)
        self._thumbsup_posture = Debounce(c.posture_hold_ms, c.posture_release_ms)
        self._thumbsup = ThumbsUpController(c)
        self._zoom_posture = Debounce(c.zoom_enter_hold_ms, c.posture_release_ms)
        self._zoom = ZoomController(c)
        self._filter = OneEuroFilter2D(c.cursor_min_cutoff, c.cursor_beta, c.cursor_d_cutoff)
        self.reset()

    def reset(self) -> None:
        self._presence.reset()
        self._cursor_posture.reset()
        self._peace_posture.reset()
        self._fist_posture.reset()
        self._fist_raw = False
        self._thumbsup_raw = False
        self._thumbsup_posture.reset()
        self._thumbsup.reset()
        self._zoom_raw = False
        self._zoom_posture.reset()
        self._zoom.reset()
        self._peace.reset()
        self._middle_up_raw = False
        self._thumb_folded_raw = False
        self._peace_raw = False
        self._filter.reset()
        self._index_up_raw = False
        self._pinch_raw = False
        self._pinch_kind: str | None = None  # 進行中のピンチの種類 "left" / "right"（有効化中に始まったもの）
        self._far_t = {"left": None, "right": None}  # 各ペアが pinch_arm より離れていた最後の時刻（有効化中のみ）
        self._pinch_start: int | None = None  # 進行中のピンチの開始時刻
        self._dragging = False
        self._right_fired = False
        self._drag_open_since: int | None = None  # ドラッグ中に、指が解除の閾値より開き始めた時刻
        self._frozen: tuple[float, float] | None = None  # ピンチ開始時のカーソル位置（ここでボタンを押す）
        self._drag_anchor: tuple[float, float] | None = None  # ドラッグ開始時の、手から求めたカーソル位置
        self._last_hand_t = 0
        self._last_active = False
        self.adapt_paused = False  # 設定画面で操作範囲を変えている間は、自動補正を止める（SPEC 13.47）
        self.state = GestureState()
        # 幅は上限・下限に収める（calibration.toml に、上限を下げる前の広い幅が残っていても使わない）
        self.roi_cx, self.roi_cy = self.cfg.roi_cx, self.cfg.roi_cy
        self.roi_width = min(max(self.cfg.roi_width, self.cfg.roi_width_min), self.cfg.roi_width_max)
        self._last_t: int | None = None
        self._label: str | None = None  # 左右ラベル（ヒステリシスをかけた値）
        self._label_run = 0  # 別のラベルが続いたフレーム数
        self._label_seen_t: int | None = None  # 最後に手が見えた時刻
        self._selected: tuple[int, Hand | None, Hand | None] | None = None  # (時刻, 選んだ手, 生の最良の手)
        self._label_palm: tuple[float, float] | None = None  # 直前のフレームの手のひら中心（ラベルの切り替えの判定用）
        self._flip_jumped = False  # 今続いている別のラベルが、手のひら中心の跳びとともに始まったか（または跳びを問わない時期か）
        self._flip_other = False  # 今続いている別のラベルが、跳びとともに始まった（別の手に乗り換えた）
        self._label_palm_t: int | None = None  # _label_palm を見た時刻
        self._label_age = 0  # 今のラベルになってからのフレーム数

    # --- 実行中の設定の差し替え（設定画面のライブのプレビュー、SPEC 13.47）---
    LIVE_FIXED = ("hand", "screen_w", "screen_h")  # 差し替えない項目（使う手は 2 手の探索の方針と一緒に、起動時に決める）

    def safe_to_reconfigure(self) -> bool:
        """ドラッグ・ピンチ・右クリック待ち・ピース・いいね・ズームの途中でない（設定を差し替えてよい瞬間）。"""
        return self._pinch_start is None and self.state.mode in ("idle", "cursor", "fist")

    def apply_config(self, new: GestureConfig) -> list[str]:
        """設定を、状態を消さずに差し替える。同じ GestureConfig を項目ごとに書き換える（各コントローラーも同じものを見ている）。
        保持時間・平滑化の係数・コントローラーが写した値も作り直す。操作範囲は、設定の値が変わったときだけ置き換える
        （変わっていなければ、自動補正した今の範囲を保つ）。変わった項目の名前を返す。"""
        c = self.cfg
        changed = [f.name for f in fields(c) if f.name not in self.LIVE_FIXED and getattr(c, f.name) != getattr(new, f.name)]
        if not changed:
            return []
        roi_changed = any(k in changed for k in ("roi_cx", "roi_cy", "roi_width", "roi_width_min", "roi_width_max"))
        for k in changed:
            setattr(c, k, getattr(new, k))
        for deb, on, off in ((self._presence, c.acquire_hold_ms, c.lose_hold_ms),
                             (self._cursor_posture, c.posture_hold_ms, c.posture_release_ms),
                             (self._peace_posture, c.posture_hold_ms, c.posture_release_ms),
                             (self._fist_posture, c.posture_hold_ms, c.posture_release_ms),
                             (self._thumbsup_posture, c.posture_hold_ms, c.posture_release_ms),
                             (self._zoom_posture, c.zoom_enter_hold_ms, c.posture_release_ms)):
            deb.hold_on_ns, deb.hold_off_ns = on * 1e6, off * 1e6
        for f in (self._filter.fx, self._filter.fy):
            f.min_cutoff, f.beta, f.d_cutoff = c.cursor_min_cutoff, c.cursor_beta, c.cursor_d_cutoff
        self._zoom._filter.min_cutoff, self._zoom._filter.beta = c.zoom_min_cutoff, c.zoom_beta
        self._peace.derive()
        self._thumbsup.derive()
        if roi_changed:
            self.roi_cx, self.roi_cy = c.roi_cx, c.roi_cy
            self.roi_width = min(max(c.roi_width, c.roi_width_min), c.roi_width_max)
        return changed

    def interrupt(self, t_ns: int) -> list[Event]:
        """進行中のピンチ・ドラッグ・モードを打ち切る（設定モードに入る・抜けるとき）。ドラッグ中ならボタンを離すイベントを返す。
        有効化の状態は変えない。"""
        events: list[Event] = []
        if self._dragging:
            events.append(Event("drag_end", t_ns, {"reason": "interrupted"}))
        elif self._pinch_start is not None:
            events.append(Event("pinch_cancel", t_ns, {"reason": "interrupted"}))
        self._end_pinch()
        for deb in (self._cursor_posture, self._peace_posture, self._fist_posture, self._thumbsup_posture, self._zoom_posture):
            deb.reset()
        self._peace.reset()
        self._thumbsup.reset()
        self._zoom.reset()
        self._far_t = {"left": None, "right": None}  # 打ち切った後は、指を離してから摘まみ直す
        events.append(Event("release_all", t_ns))
        return events

    @property
    def hand_present(self) -> bool:
        return self._presence.value

    @property
    def stable_label(self) -> str | None:
        return self._label

    LOCKED_MODES = ("pinch", "drag", "right", "peace", "thumbsup", "zoom")

    def _best(self, frame: LandmarkFrame) -> Hand | None:
        hands = [h for h in frame.hands if h.score >= self.cfg.min_score]
        return max(hands, key=lambda h: h.score) if hands else None

    def _select(self, frame: LandmarkFrame) -> tuple[Hand | None, Hand | None]:
        """使う手を選ぶ（1 フレームに 1 回、update から呼ぶ）。返り値は (選んだ手, 生の最良の手)。"""
        c = self.cfg
        t = frame.t_capture_ns
        best = self._best(frame)
        if c.hand == "both":
            return best, best
        want = "Right" if c.hand == "right" else "Left"
        hands = [h for h in frame.hands if h.score >= c.min_score]
        if best is None:
            # 見失いが続いたら、ラベルを忘れる（次に見えた手のラベルを、そのまま使う）
            if self._label_seen_t is not None and (t - self._label_seen_t) / 1e6 >= c.lose_hold_ms:
                self._label, self._label_run, self._label_palm = None, 0, None
            return None, None
        self._label_seen_t = t
        matching = [h for h in hands if h.handedness == want]
        if len(hands) > 1:
            # 両方の手が映っている（tracker.num_hands = 2）: 設定の手のラベルの手だけを使う。もう一方の手が先に映っていても、
            # 設定の手を出せばすぐに操作できる。ラベルの状態もその手に合わせる（もう一方の手が去った後も、続けて使えるように）
            if not matching:
                return None, best
            chosen = max(matching, key=lambda h: h.score)
            if self._label != want:
                self._label_age = 0
            self._label, self._label_run = want, 0
            self._label_palm = tuple(float(v) for v in chosen.landmarks[list(PALM), :2].mean(axis=0))
            self._label_palm_t = t
            return chosen, best
        raw = best.handedness
        palm = tuple(float(v) for v in best.landmarks[list(PALM), :2].mean(axis=0))
        prev, self._label_palm = self._label_palm, palm
        # 手のひら中心が画像の外（画角から出かけた手の、推定のランドマーク）にあるときは、同じ手でも位置が大きく揺れる
        # （thumbsup01 の 33.15 秒: 0.15 動いた）ので、跳びの閾値を 2 倍にする。下から出ていった右手の後に、頬杖の左手が
        # 映ったときは 0.58 跳んでいた（段階7の印 12）
        def inside(q):
            return 0.0 <= q[0] <= 1.0 and 0.0 <= q[1] <= 1.0

        jump_thr = c.hand_switch_jump * (1.0 if prev is None or (inside(prev) and inside(palm)) else 2.0)
        self._label_palm_t = t
        self._label_age += 1
        if self._label is None:
            self._label, self._label_age = raw, 0
        elif raw != self._label:
            # 別のラベルが続いている間に手のひら中心が跳んだら、別の手に乗り換えた証拠（1 手の推論器が、頬杖の左手などに
            # 乗り換える: 2026-10-26 段階7の印 6・12。最初の 1 フレームは元の位置のままラベルだけ変わり、次で跳ぶこともある）
            jumped = c.hand_switch_jump > 0 and prev is not None and math.dist(palm, prev) >= jump_thr
            if self._label_run == 0:  # 別のラベルが始まった（今のラベルが落ち着く前なら、跳びを問わない）
                self._flip_other = jumped
                self._flip_jumped = (jumped or c.hand_switch_jump <= 0 or prev is None
                                     or self._label_age < c.hand_settle_frames)
            elif jumped:
                self._flip_other = self._flip_jumped = True
            self._label_run += 1
            # 別の手に乗り換えたときは、モードの途中でも切り替える（同じ手のラベルの反転だけを、モードの途中で無視する）
            if (self._label_run >= c.hand_switch_frames and self._flip_jumped
                    and (self._flip_other or self.state.mode not in self.LOCKED_MODES)):
                self._label, self._label_run, self._label_age = raw, 0, 0
            elif self._flip_other:
                return None, best  # 切り替わるまでの数フレームも、乗り換えた先の手では操作しない（グーと同じく何もしない）
        else:
            self._label_run = 0
        return (best if self._label == want else None), best

    def primary_hand(self, frame: LandmarkFrame) -> Hand | None:
        """そのフレームで使う手。update の後に同じフレームで呼ぶと、update が選んだ手を返す。"""
        if self._selected is not None and self._selected[0] == frame.t_capture_ns:
            return self._selected[1]
        best = self._best(frame)
        if self.cfg.hand == "both" or best is None:
            return best
        return best if (self._label or best.handedness) == ("Right" if self.cfg.hand == "right" else "Left") else None

    def raw_hand(self, frame: LandmarkFrame) -> Hand | None:
        """左右で選ぶ前の、信頼度が最も高い手（ログ・オーバーレイ用）。"""
        if self._selected is not None and self._selected[0] == frame.t_capture_ns:
            return self._selected[2]
        return self._best(frame)

    # --- マッピング ---
    def roi(self, image_size: tuple[int, int]) -> tuple[float, float, float, float]:
        """操作範囲 (x0, y0, w, h)（画像座標 0..1）。"""
        c = self.cfg
        iw, ih = image_size
        w = self.roi_width
        h = w * iw * (c.screen_h / c.screen_w) / ih
        return self.roi_cx - w / 2, self.roi_cy - h / 2, w, h

    def _adapt_roi(self, palm: tuple[float, float], image_size: tuple[int, int], dt_s: float) -> None:
        c = self.cfg
        x0, y0, w, h = self.roi(image_size)
        ex = min(0.0, palm[0] - x0) + max(0.0, palm[0] - (x0 + w))
        ey = min(0.0, palm[1] - y0) + max(0.0, palm[1] - (y0 + h))
        if not (ex or ey) or dt_s <= 0:
            return
        a = 1.0 - math.exp(-dt_s / c.roi_adapt_tau_s)
        self.roi_cx += a * c.roi_adapt_shift * ex
        self.roi_cy += a * c.roi_adapt_shift * ey
        grow = abs(ex) + abs(ey) * (w / h)  # 縦の超過を幅に換算（アスペクト比は画面に合わせたまま）
        self.roi_width = min(max(self.roi_width + a * c.roi_adapt_grow * grow, c.roi_width_min), c.roi_width_max)

    def map_to_screen(self, palm: tuple[float, float], image_size: tuple[int, int]) -> tuple[float, float]:
        x0, y0, w, h = self.roi(image_size)
        u = (palm[0] - x0) / w
        v = (palm[1] - y0) / h
        return (1.0 - u) * self.cfg.screen_w, v * self.cfg.screen_h  # 左右反転（利用者から見た方向に合わせる）

    def _clamp(self, p: tuple[float, float]) -> tuple[float, float]:
        return (min(max(p[0], 0.0), self.cfg.screen_w - 1), min(max(p[1], 0.0), self.cfg.screen_h - 1))

    # --- 本体 ---
    def update(self, frame: LandmarkFrame) -> list[Event]:
        c = self.cfg
        events: list[Event] = []
        t = frame.t_capture_ns
        hand, raw_best = self._select(frame)
        self._selected = (t, hand, raw_best)
        st = self.state
        st.t_ns, st.active = t, frame.active

        if self._presence.update(hand is not None, t):
            events.append(Event("hand_acquired" if self.hand_present else "hand_lost", t))

        # 有効化が切れたら、進行中のピンチを捨てる（クリックしない）。ドラッグ中ならボタンを離す
        if not frame.active:
            self._far_t = {"left": None, "right": None}  # 有効化した後に、指を離してから摘まむ動作だけを数える
        if self._last_active and not frame.active:
            if self._dragging:
                events.append(Event("drag_end", t, {"reason": "deactivated"}))
            elif self._pinch_start is not None:
                events.append(Event("pinch_cancel", t, {"reason": "deactivated"}))
            self._end_pinch()
            events.append(Event("release_all", t))
        self._last_active = frame.active

        ti = tm = float("nan")
        if hand is not None:
            self._last_hand_t = t
            st.score = hand.score
            st.landmarks = hand.landmarks
            palm = tuple(float(v) for v in hand.landmarks[list(PALM), :2].mean(axis=0))
            st.palm = palm
            # 姿勢: 人差し指が立っているか（ヒステリシス）
            s = finger_straightness(hand.world, INDEX_MCP)
            self._index_up_raw = s > (c.index_up_exit if self._index_up_raw else c.index_up_enter)
            # グーの姿勢: 4本の指がすべて曲がっている（ヒステリシス）
            fingers = [finger_straightness(hand.world, m) for m in (INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP)]
            self._fist_raw = c.fist_enabled and (max(fingers) < (c.fist_exit if self._fist_raw else c.fist_enter))
            # ピースの姿勢: 人差し指と中指が立ち、薬指は畳み、親指を畳んでいる
            sm = finger_straightness(hand.world, MIDDLE_MCP)
            self._middle_up_raw = sm > (c.index_up_exit if self._middle_up_raw else c.index_up_enter)
            tf = thumb_fold_metric(hand)
            self._thumb_folded_raw = tf > (c.thumb_fold_off if self._thumb_folded_raw else c.thumb_fold_on)
            # ズームの姿勢: 入るときは手の甲を向けて人差し指・中指・薬指を伸ばす。入った後は手の甲の向きだけ見る
            back = palm_facing(hand)
            if self._zoom_posture.value:
                self._zoom_raw = (c.zoom_enabled and back > c.zoom_stay_back_min and tf < c.zoom_stay_thumb_max
                                  and float(np.mean(fingers)) > c.zoom_stay_open_min)
            else:
                self._zoom_raw = (c.zoom_enabled and back > c.zoom_back_min and min(fingers[:3]) > c.zoom_open_min
                                  and tf < c.zoom_thumb_max)
            # いいねの姿勢: 入るときは、人差し指を握り、親指が上を向いて手のひらの外へ出ている。入った後は条件を緩める
            up = thumb_up(hand)
            far = thumb_far(hand, frame.image_size)
            if self._thumbsup_posture.value:
                self._thumbsup_raw = (c.thumbsup_enabled and up > c.thumbsup_stay_up_min
                                      and fingers[0] < c.thumbsup_stay_index_max
                                      and far > c.thumbsup_stay_thumb_far_min)
            else:
                self._thumbsup_raw = (c.thumbsup_enabled and fingers[0] < c.thumbsup_index_max
                                      and up > c.thumbsup_up_min and tf < c.thumbsup_fold_max
                                      and far > c.thumbsup_thumb_far_min)
            if self._thumbsup_raw:
                self._fist_raw = False  # いいねは、4本の指を握っていてもグーではない
            if self._peace_posture.value:
                # ピースモード中は、人差し指と中指が立っている限り保つ。指先を下に向けると、中指が曲がって見え、
                # 親指の判定も崩れるため（peace01）。入るときだけ、親指と薬指も見る
                self._peace_raw = (c.peace_enabled and s > c.peace_exit_straight
                                   and finger_straightness(hand.world, MIDDLE_MCP) > c.peace_exit_straight)
            else:
                self._peace_raw = (c.peace_enabled and self._index_up_raw and self._middle_up_raw
                                   and self._thumb_folded_raw and not self._thumbsup_raw
                                   and finger_straightness(hand.world, RING_MCP) < c.ring_down_max)
            # ピンチ: 親指と人差し指（左）、親指と中指（右）のうち、距離が小さい方のペアで判定する
            pm = image_pinch_metrics(hand, frame.image_size) if c.pinch_metric == "image" \
                else pinch_metrics(hand, c.base_length_m)
            ti, tm = pm["thumb_index_n"], pm["thumb_middle_n"]
            if self._pinch_kind is not None:  # 進行中: そのペアが離れたら解除
                if self._pinch_kind == "right":
                    self._pinch_raw = tm <= c.right_off
                else:
                    self._pinch_raw = ti <= (c.drag_off if self._dragging else c.pinch_off)
            else:
                self._pinch_raw = ti < c.pinch_on or tm < c.right_on
            if frame.active:
                if ti > c.pinch_off:
                    self._far_t["left"] = t
                if tm > c.right_arm:
                    self._far_t["right"] = t
            # カーソル位置は常に計算し、フィルタを温めておく（有効化した瞬間に古い状態を使わないため）
            raw = self.map_to_screen(palm, frame.image_size)
            st.cursor = self._clamp(self._filter(raw[0], raw[1], t / 1e9))
        st.pinch_n = tm if self._pinch_kind == "right" else ti

        # ピンチの最中は、指が曲がって姿勢が崩れても、カーソルモードを保つ
        pinching = self._pinch_start is not None
        self._cursor_posture.update(self._index_up_raw and hand is not None or pinching, t)
        was_peace = self._peace_posture.value
        if hand is not None:  # 見えないフレームでは、ピースの状態を変えない（見失いの判定に任せる）
            self._peace_posture.update(self._peace_raw and not pinching, t)
        if not self.hand_present and not self._dragging:
            self._cursor_posture.reset()
            self._peace_posture.reset()
        if self._peace_posture.value and not was_peace:
            self._peace.enter()
        was_zoom = self._zoom_posture.value
        if hand is not None:
            self._zoom_posture.update(self._zoom_raw and not pinching, t)
        if not self.hand_present:
            self._zoom_posture.reset()
        if self._zoom_posture.value and not was_zoom:
            self._zoom.enter()
        if self._zoom_posture.value:  # ズームが優先（ほかのモードの判定を止める）
            self._thumbsup_posture.reset()
            self._peace_posture.reset()
        was_thumbsup = self._thumbsup_posture.value
        if hand is not None:
            self._thumbsup_posture.update(self._thumbsup_raw and not pinching, t)
        if not self.hand_present:
            self._thumbsup_posture.reset()
        if self._thumbsup_posture.value and not was_thumbsup:
            self._thumbsup.enter()
        if self._thumbsup_posture.value:  # いいねが優先（ピースの判定を止める）
            self._peace_posture.reset()
        if hand is not None:
            self._fist_posture.update(self._fist_raw and not pinching, t)
        if not self.hand_present:
            self._fist_posture.reset()
        fist_like = self._fist_posture.value or (self._fist_raw and hand is not None)
        # ピースの姿勢になりかけている間（保持時間の途中）も、カーソルとピンチを止める
        peace_like = self._peace_posture.value or (self._peace_raw and hand is not None)
        thumbsup_like = self._thumbsup_posture.value or (self._thumbsup_raw and hand is not None)
        zoom_like = self._zoom_posture.value or (self._zoom_raw and hand is not None)
        thumbsup_like = thumbsup_like or zoom_like  # なりかけの間も、カーソルとピンチを止める
        peace_like = peace_like or thumbsup_like  # なりかけの間も、カーソルとピンチを止める

        if self._dragging:
            if hand is None:
                if (t - self._last_hand_t) / 1e6 >= c.drag_lost_release_ms:
                    events.append(Event("drag_end", t, {"reason": "hand_lost"}))
                    self._end_pinch()
            elif self._pinch_raw:
                self._drag_open_since = None
            else:
                if self._drag_open_since is None:
                    self._drag_open_since = t
                if (t - self._drag_open_since) / 1e6 >= c.drag_release_ms:
                    events.append(Event("drag_end", t, {"reason": "released",
                                                        "duration_ms": round((t - self._pinch_start) / 1e6, 1)}))
                    self._end_pinch()
        elif pinching and self._pinch_kind == "right":
            # 右クリック: 親指と中指を付けたまま right_hold_ms 待つと発火する（離した時ではない）。待つ間はカーソルを固定。
            # 途中でピースの姿勢になったら取り消す（ピースへ移る途中で親指が中指に触れて発火していた: peace04）
            dur_ms = (t - self._pinch_start) / 1e6
            if not self.hand_present:
                events.append(Event("pinch_cancel", t, {"button": "right", "reason": "hand_lost"}))
                self._end_pinch()
            elif peace_like or fist_like:
                if not self._right_fired:
                    events.append(Event("pinch_cancel", t, {"button": "right", "reason": "posture",
                                                            "duration_ms": round(dur_ms, 1)}))
                self._end_pinch()
            elif hand is not None and not self._pinch_raw:  # 指を離した
                if not self._right_fired:
                    events.append(Event("pinch_cancel", t, {"button": "right", "reason": "released_early",
                                                            "duration_ms": round(dur_ms, 1)}))
                self._end_pinch()
            elif not self._right_fired and dur_ms >= c.right_hold_ms:
                self._right_fired = True  # 発火後は、指を離すまでカーソルを固定したまま（メニューの上で動かないように）
                events.append(Event("click", t, {"button": "right", "duration_ms": round(dur_ms, 1)}))
        elif pinching:
            dur_ms = (t - self._pinch_start) / 1e6
            if not self.hand_present:
                events.append(Event("pinch_cancel", t, {"reason": "hand_lost"}))
                self._end_pinch()
            elif hand is not None and not self._pinch_raw:  # 指を離した
                if c.click_min_ms <= dur_ms < c.drag_ms:
                    events.append(Event("click", t, {"button": "left", "duration_ms": round(dur_ms, 1)}))
                else:
                    events.append(Event("pinch_cancel", t, {"button": "left", "duration_ms": round(dur_ms, 1),
                                                            "reason": "too_short"}))
                self._end_pinch()
            elif dur_ms >= c.drag_ms:
                self._dragging = True
                self._drag_anchor = st.cursor
                x, y = self._frozen
                events.append(Event("drag_start", t, {"x": round(x, 2), "y": round(y, 2)}))
        elif frame.active and self._cursor_posture.value and hand is not None and self._pinch_raw \
                and st.cursor is not None and not peace_like and not fist_like:
            # 右（親指と中指）は、親指が人差し指から離れているときだけ。人差し指に触れているなら左として扱う
            # （以前は中指の方が近いと「右」と判定し、右の条件を満たさず何も起きず、左クリックが失われていた）
            kind = "right" if (tm < c.right_on and ti > c.pinch_off) else "left" if ti < c.pinch_on else "right"
            far = self._far_t[kind]
            armed = far is not None and (kind == "left" or ((t - far) / 1e6 <= c.right_arm_window_ms
                                                            and ti > c.pinch_off))
            if armed:
                self._pinch_kind = kind
                self._pinch_start = t
                self._frozen = st.cursor
                self._far_t[kind] = None  # 次のピンチには、もう一度離す必要がある
                events.append(Event("pinch_start", t, {"button": kind}))

        pinching = self._pinch_start is not None
        st.hand_present = self.hand_present
        if self._dragging:
            st.mode = "drag"
        elif pinching:
            st.mode = "pinch" if self._pinch_kind == "left" else "right"
        elif self._zoom_posture.value and self.hand_present:
            st.mode = "zoom"  # 手招きで指を曲げてもグーにしない
        elif fist_like and self.hand_present:
            st.mode = "fist"  # 何もしない（なりかけの間も止める）
        elif self._thumbsup_posture.value and self.hand_present:
            st.mode = "thumbsup"
        elif self._peace_posture.value and self.hand_present:
            st.mode = "peace"
        elif peace_like:
            st.mode = "idle"  # ピースになりかけ: カーソルを止める
        else:
            st.mode = "cursor" if self._cursor_posture.value and self.hand_present else "idle"
        st.peace = None
        if st.mode == "peace":
            events.extend(self._peace.update(hand, frame.image_size, t, frame.active))
            st.peace = self._peace.view
        elif st.mode == "zoom":
            events.extend(self._zoom.update(hand, frame.image_size, t, frame.active))
        elif st.mode == "thumbsup":
            events.extend(self._thumbsup.update(hand, frame.image_size, t, frame.active))
            st.peace = self._thumbsup.view
        st.zoom_level = self._zoom.level if st.mode == "zoom" else 0.0
        st.zoom_progress = self._zoom.progress if st.mode == "zoom" else 0.0
        st.zoom_armed = self._zoom.armed
        st.zoom_fire_ns, st.zoom_fire_dir = self._zoom.last_fire_ns, self._zoom.last_fire_dir
        if pinching:
            hold = c.drag_ms if self._pinch_kind == "left" else c.right_hold_ms
            st.pinch_progress = min(1.0, (t - self._pinch_start) / 1e6 / hold)
        else:
            st.pinch_progress = 0.0

        # 操作範囲の自動補正（有効化中のカーソル操作のときだけ）
        dt_s = (t - self._last_t) / 1e9 if self._last_t is not None else 0.0
        self._last_t = t
        if c.roi_adapt and not self.adapt_paused and frame.active and st.mode == "cursor" and hand is not None and st.palm is not None:
            self._adapt_roi(st.palm, frame.image_size, dt_s)

        # カーソル移動（ピンチ・右クリック中は固定。離しかけている間は、落とす位置を動かさない）
        if frame.active and hand is not None and st.cursor is not None:
            if st.mode == "cursor":
                events.append(Event("move", t, {"x": round(float(st.cursor[0]), 2),
                                                "y": round(float(st.cursor[1]), 2)}))
            elif st.mode == "drag" and self._drag_open_since is None:  # 離しかけている間は、落とす位置を動かさない
                x, y = self._clamp((self._frozen[0] + st.cursor[0] - self._drag_anchor[0],
                                    self._frozen[1] + st.cursor[1] - self._drag_anchor[1]))
                events.append(Event("move", t, {"x": round(float(x), 2), "y": round(float(y), 2)}))
        return events

    def _end_pinch(self) -> None:
        self._pinch_kind = None
        self._pinch_start = None
        self._dragging = False
        self._right_fired = False
        self._drag_open_since = None
        self._frozen = None
        self._drag_anchor = None


class HandCountPolicy:
    """推論で探す手の数を決める（input.TrackerConfig.auto_two_hands）。普段は 1。設定と違う手だけを捉えている間
    （その手で操作できない間）は 2 にして、設定の手を出せばすぐ見つかるようにする。2 の推論は約 2 倍重い
    （p50 13 → 26ms の実測）ので、設定の手を捉えたら return_frames フレームで 1 に戻す（もう一方の手が映ったままでも）。
    戻した直後は、1 つの手しか探さない推論器がもう一方の手を拾い直さないよう、最後に見えたもう一方の手の位置を
    画像上で塗りつぶす（mask_boxes）。設定の手を捉えたら、推論器はその手を追い続けるので、塗るのをやめる。
    設定と違うラベルの手が release_s 秒見えなくなったときも 1 に戻す。"""

    def __init__(self, hand: str = "both", release_s: float = 1.0, min_score: float = 0.5, return_frames: int = 3,
                 mask_s: float = 1.0, mask_margin: float = 0.3):
        self.want = {"right": "Right", "left": "Left"}.get(hand)
        self.release_ns = int(release_s * 1e9)
        self.min_score = min_score
        self.return_frames = return_frames
        self.mask_ns = int(mask_s * 1e9)
        self.mask_margin = mask_margin
        self.num_hands = 1
        self.mask_boxes: list[tuple[float, float, float, float]] = []  # 塗りつぶす範囲 (x0, y0, x1, y1)、画像座標 0..1
        self._mask_until = 0
        self._other_t: int | None = None
        self._other_boxes: list[tuple[float, float, float, float]] = []
        self._ok_run = 0

    def _box(self, hand: Hand) -> tuple[float, float, float, float]:
        p = hand.landmarks[:, :2]
        (x0, y0), (x1, y1) = p.min(axis=0), p.max(axis=0)
        mx, my = (x1 - x0) * self.mask_margin + 0.02, (y1 - y0) * self.mask_margin + 0.02
        return float(x0 - mx), float(y0 - my), float(x1 + mx), float(y1 + my)

    def update(self, frame: LandmarkFrame, engine: GestureEngine) -> int:
        """フレームを処理した後に呼ぶ。切り替わったら新しい数を、変わらなければ 0 を返す。"""
        if self.want is None:
            return 0
        t = frame.t_capture_ns
        others = [h for h in frame.hands if h.handedness != self.want and h.score >= self.min_score]
        if others:
            self._other_t = t
            self._other_boxes = [self._box(h) for h in others]
        selected = engine.primary_hand(frame) is not None
        if self.mask_boxes and (selected or t >= self._mask_until):
            self.mask_boxes = []  # 設定の手を捉えた（または時間切れ）
        if self.num_hands == 1:
            if engine.raw_hand(frame) is not None and not selected:
                self.num_hands, self._ok_run = 2, 0  # 使えない手だけを捉えている
                return 2
            return 0
        self._ok_run = self._ok_run + 1 if selected else 0
        if self._ok_run >= self.return_frames:
            self.num_hands = 1
            if others:  # もう一方の手が映ったまま戻す: その位置を塗って、設定の手を拾わせる
                self.mask_boxes = list(self._other_boxes)
                self._mask_until = t + self.mask_ns
            return 1
        if self._other_t is None or t - self._other_t >= self.release_ns:
            self.num_hands = 1
            return 1
        return 0


def run(engine: GestureEngine, frames) -> list[Event]:
    """フレーム列を流し込み、全イベントを返す（再現テスト用）。"""
    engine.reset()
    out: list[Event] = []
    for f in frames:
        out.extend(engine.update(f))
    return out
