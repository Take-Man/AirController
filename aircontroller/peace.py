"""ピースモード（段階4）: 人差し指と中指を立て、親指を畳んだ姿勢で、指先の向きを4領域に分ける。

- 上下: 連続スクロール。中央の不感帯を超えたら、離れるほど速くする（速度カーブ）。
  上に向けると下へスクロール（ページの先へ進む）、下に向けると上へ。
- 左右: 戻る・進む。領域に入ったら一度だけ発火し、中央付近（再武装の範囲）に戻るまで再発火しない。

指先の向きの取り方を2つ持つ（未確定事項3: 単眼カメラで仰角が安定して取れるか）。
- "angle": world landmarks の付け根→指先のベクトルから、方位角と仰角（度）を求め、中立方向との差を使う。
  中立方向は校正で測る（作者の所見では、カメラ正面より少し内旋した方向が楽、SPEC 13.7）。
- "displacement": モードに入ったときの指先の画像上の位置を基準に、そこからの変位（手の大きさで正規化）を使う。

どちらも左右は利用者から見た向き（右が正）、上下は上が正。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .filters import OneEuroFilter2D
from .types import (INDEX_MCP, INDEX_TIP, MIDDLE_MCP, MIDDLE_TIP, PALM, PINKY_MCP, THUMB_MCP, THUMB_TIP, WRIST, Event,
                    Hand)


def direction_angles(hand: Hand) -> tuple[float, float]:
    """人差し指と中指の付け根→指先の平均方向の (方位角, 仰角)（度）。world 座標は x 右, y 下, z 奥。
    方位角は利用者から見て右が正（画像は鏡像でないので x を反転）、仰角は上が正。カメラを指すと (0, 0)。"""
    w = hand.world
    v = (w[INDEX_TIP] - w[INDEX_MCP]) + (w[MIDDLE_TIP] - w[MIDDLE_MCP])
    n = float(np.linalg.norm(v))
    if n == 0:
        return 0.0, 0.0
    x, y, z = v / n
    yaw = math.degrees(math.atan2(-x, -z))
    pitch = math.degrees(math.atan2(-y, math.hypot(x, z)))
    return yaw, pitch


def fingertip_position(hand: Hand, image_size: tuple[int, int]) -> tuple[float, float, float]:
    """人差し指と中指の先の中点（画像ピクセル）と、画像上の手首→中指付け根の長さ。"""
    p = hand.landmarks[:, :2] * np.asarray(image_size, dtype=np.float64)
    tip = (p[INDEX_TIP] + p[MIDDLE_TIP]) / 2
    return float(tip[0]), float(tip[1]), float(np.linalg.norm(p[MIDDLE_MCP] - p[WRIST]))


def palm_position(hand: Hand, image_size: tuple[int, int]) -> tuple[float, float, float]:
    """手のひら中心（手首と4本の指の付け根の平均、画像ピクセル）と、画像上の手首→中指付け根の長さ。"""
    p = hand.landmarks[:, :2] * np.asarray(image_size, dtype=np.float64)
    c = p[list(PALM)].mean(axis=0)
    return float(c[0]), float(c[1]), float(np.linalg.norm(p[MIDDLE_MCP] - p[WRIST]))


def hand_angles(hand: Hand, image_size: tuple[int, int]) -> tuple[float, float]:
    """利用者から見た（左右反転した）画像上の角度（度）: 親指（付け根 MCP→先）と、人差し指付け根→小指付け根。"""
    p = hand.landmarks[:, :2] * np.asarray(image_size, dtype=np.float64)

    def ang(v):
        return math.degrees(math.atan2(-v[1], -v[0]))  # 右（反転後）を 0、上を 90

    return ang(p[THUMB_TIP] - p[THUMB_MCP]), ang(p[PINKY_MCP] - p[INDEX_MCP])


def _wrap(d: float) -> float:
    return (d + 180.0) % 360.0 - 180.0


@dataclass
class PeaceView:
    """オーバーレイ用。sx, sy は max で割った値（-1..1 付近）。"""

    sx: float = 0.0
    sy: float = 0.0
    dead: float = 0.0
    fire: float = 0.0
    rearm: float = 0.0
    region: str = "center"  # center / up / down / left / right
    nav_progress: float = 0.0  # 戻る・進むの、発火までの経過（0..1）
    raw: tuple[float, float] = field(default=(0.0, 0.0))


class PeaceController:
    """ピースモード: 上下は連続スクロール、左右は戻る・進む（とどまって発火）。"""

    discrete = ("left", "right")  # とどまって一度だけ発火する方向
    event_kind = "nav"
    fire_by_dir: dict = {}  # 方向ごとの発火の閾値（無ければ self.fire）

    def __init__(self, cfg):
        self.cfg = cfg
        c = cfg
        self.dwell_ms, self.cooldown_ms = c.nav_dwell_ms, c.nav_cooldown_ms
        self.angle = c.peace_method == "angle"
        if self.angle:
            self.dead, self.max, self.fire, self.rearm = (c.peace_dead_deg, c.peace_max_deg,
                                                          c.peace_fire_deg, c.peace_rearm_deg)
        else:
            self.dead, self.max, self.fire, self.rearm = (c.peace_dead_disp, c.peace_max_disp,
                                                          c.peace_fire_disp, c.peace_rearm_disp)
        self._filter = OneEuroFilter2D(c.peace_min_cutoff, c.peace_beta, 1.0)
        self.reset()

    def reset(self) -> None:
        self._filter.reset()
        self._origin: tuple[float, float] | None = None
        self._last_t: float | None = None
        self._armed = True
        self._cooldown_until = 0.0
        self._remainder = 0.0
        self._nav_since: float | None = None
        self._nav_side: str | None = None
        self.view = PeaceView(dead=self._n(self.dead), fire=self._n(self.fire), rearm=self._n(self.rearm))

    def _n(self, v: float) -> float:
        return v / self.max if self.max else 0.0

    def enter(self) -> None:
        """モードに入った。変位方式の基準は、次に手が見えたフレームで取る。左右は、中央を一度見るまで発火しない。"""
        self.reset()
        self._armed = False

    def _signal(self, hand: Hand, image_size, t_s: float) -> tuple[float, float]:
        c = self.cfg
        if self.angle:
            yaw, pitch = direction_angles(hand)
            sx, sy = yaw - c.peace_neutral_yaw_deg, pitch - c.peace_neutral_pitch_deg
        else:
            x, y, ref = fingertip_position(hand, image_size)
            if self._origin is None:
                self._origin = (x, y)
            ref = max(ref, 1e-6)
            sx, sy = -(x - self._origin[0]) / ref, -(y - self._origin[1]) / ref  # 鏡像に合わせて x を反転、上を正
        return self._filter(sx, sy, t_s)

    def update(self, hand: Hand | None, image_size, t_ns: int, active: bool) -> list[Event]:
        c = self.cfg
        t_s = t_ns / 1e9
        dt = t_s - self._last_t if self._last_t is not None else 0.0
        self._last_t = t_s
        if hand is None:
            return []
        sx, sy = self._signal(hand, image_size, t_s)
        ax, ay = abs(sx), abs(sy)
        if max(ax, ay) < self.dead:
            region = "center"
        elif ay >= ax:
            region = "up" if sy > 0 else "down"
        else:
            region = "right" if sx > 0 else "left"
        v = self.view
        v.sx, v.sy, v.region, v.raw = self._n(sx), self._n(sy), region, (sx, sy)

        events: list[Event] = []
        # 中央付近に戻ったら、次の発火を許す（上下も離散の方向なら、上下の変位も見る）
        rearm_amount = max(ax, ay) if "up" in self.discrete else ax
        if rearm_amount < self.rearm:
            self._armed = True
        if region in ("up", "down") and "up" not in self.discrete and dt > 0:
            u = min(1.0, (ay - self.dead) / max(self.max - self.dead, 1e-9))
            speed = c.scroll_max_speed * u ** c.scroll_exponent  # px/秒
            # 上に向けると、ページの先（下）へ進む。dy > 0 = ページの先へ
            amount = speed * dt * (1 if sy > 0 else -1) * (-1 if c.scroll_invert else 1) + self._remainder
            px = int(amount)
            self._remainder = amount - px
            if px and active:
                events.append(Event("scroll", t_ns, {"dy": px}))
        else:
            self._remainder = 0.0
        # 離散の方向（戻る・進むなど）: 発火の閾値を超えたら計時を始め、同じ方向の領域にとどまって dwell_ms 経ったら一度だけ発火する。
        # 計時中は発火の閾値を下回っても続ける（閾値付近で行き来すると計時が毎回やり直しになり、発火しなかった: peace03）
        side = region if region in self.discrete else None
        amount = ay if side in ("up", "down") else ax
        fire = self.fire_by_dir.get(side, self.fire) if side else self.fire
        if self._nav_since is not None and side != self._nav_side:
            self._nav_since = None
        if side and self._armed and t_s >= self._cooldown_until and (self._nav_since is not None or amount >= fire):
            if self._nav_since is None:
                self._nav_since, self._nav_side = t_s, side
            held = (t_s - self._nav_since) * 1000
            v.nav_progress = min(1.0, held / self.dwell_ms) if self.dwell_ms > 0 else 1.0
            if held >= self.dwell_ms:
                self._armed = False
                self._nav_since = None
                v.nav_progress = 0.0
                self._cooldown_until = t_s + self.cooldown_ms / 1000
                if active:
                    events.append(Event(self.event_kind, t_ns, {"dir": self._dir_name(side)}))
        else:
            self._nav_since = None
            v.nav_progress = 0.0
        return events

    def _dir_name(self, side: str) -> str:
        return "back" if side == "left" else "forward"


class ThumbsUpController(PeaceController):
    """いいねモード（段階5、SPEC 13.16）: 親指を立て、ほかの指を握る。上・左・右とも、とどまって一度だけ発火する。
    左右 = デスクトップ（操作スペース）の移動、上 = Mission Control（もう一度で閉じる）。下は使わない（発火しにくく、
    App Exposé は範囲外とした: SPEC 13.19）。中央に戻るまで再発火しない。
    方向は、モードに入ったときの手のひら中心からの変位（手の大きさで正規化）で判定する。"""

    discrete = ("left", "right", "up")
    event_kind = "thumbsup"

    def __init__(self, cfg):
        self.cfg = cfg
        c = cfg
        self.dwell_ms, self.cooldown_ms = c.thumbsup_dwell_ms, c.thumbsup_cooldown_ms
        self.angle = False
        self.dead, self.max, self.fire, self.rearm = (c.thumbsup_dead_disp, c.thumbsup_max_disp,
                                                      c.thumbsup_fire_left, c.thumbsup_rearm_disp)
        # 方向ごとに閾値を持つ（下へは動かしにくい: lab01 で上 +2.0 に対し下 -1.3）
        self.fire_by_dir = {"left": c.thumbsup_fire_left, "right": c.thumbsup_fire_right, "up": c.thumbsup_fire_up}
        self._filter = OneEuroFilter2D(c.peace_min_cutoff, c.peace_beta, 1.0)
        self.reset()

    def reset(self) -> None:
        super().reset()
        self._origin_ref: float | None = None
        self._origin_angles: tuple[float, float] = (0.0, 0.0)
        self._prev_sig: tuple[float, float, float] | None = None
        self._still_since: float | None = None
        self._after_fire: str | None = None  # None / "wait_move" / "wait_still"
        self._last_pose: tuple[float, float, float, float] | None = None

    def update(self, hand: Hand | None, image_size, t_ns: int, active: bool) -> list[Event]:
        """正面（基準の位置・角度）の扱い:
        - 発火した後は、手が動き出してから次に止まった場所を新しい正面にして、次の発火を許す。戻る動きは発火しない。
          元の位置に正確に戻らないと再武装されず、2回目以降が発火しなかった（learn-thumbsup-052411）。
        - 不感帯の中にいる間は、正面がゆっくり手に追従する（姿勢のずれを吸収する）。"""
        c = self.cfg
        t_s = t_ns / 1e9
        events = super().update(hand, image_size, t_ns, active)
        if events:
            self._after_fire = "wait_move"
        elif self._armed:
            self._after_fire = None  # 正面付近を通って再武装済みなら、取り直しは要らない
        if hand is None:
            return events
        sx, sy = self.view.raw
        moving = False
        if self._prev_sig is not None and t_s > self._prev_sig[2]:
            dt = t_s - self._prev_sig[2]
            moving = math.hypot(sx - self._prev_sig[0], sy - self._prev_sig[1]) / dt >= c.thumbsup_still_speed
            # 不感帯の中にいる間だけ、正面をゆっくり手に寄せる（不感帯の外で寄せると、意図して止めた量が目減りする）
            if self._armed and self.view.region == "center" and c.thumbsup_center_follow_s > 0:
                self._follow(min(1.0, dt / c.thumbsup_center_follow_s))
        self._still_since = None if moving else (self._still_since or t_s)
        self._prev_sig = (sx, sy, t_s)
        if self._after_fire == "wait_move" and moving:
            self._after_fire = "wait_still"
        elif (self._after_fire == "wait_still" and self._still_since is not None
              and (t_s - self._still_since) * 1000 >= c.thumbsup_still_ms):
            self._origin = None  # 次のフレームで、今の位置・角度を正面として取り直す
            self._filter.reset()
            self._armed = True
            self._after_fire = None
            self._prev_sig = None
        return events

    def _follow(self, a: float) -> None:
        """正面（位置・角度）を、いまの手の方へ割合 a だけ寄せる。"""
        if self._origin is None or self._last_pose is None:
            return
        x, y, at, ak = self._last_pose
        self._origin = (self._origin[0] + a * (x - self._origin[0]), self._origin[1] + a * (y - self._origin[1]))
        self._origin_angles = (self._origin_angles[0] + a * _wrap(at - self._origin_angles[0]),
                               self._origin_angles[1] + a * _wrap(ak - self._origin_angles[1]))

    def _signal(self, hand: Hand, image_size, t_s: float) -> tuple[float, float]:
        c = self.cfg
        dx, dy, rt, rk = self.raw_signal(hand, image_size)
        sx = dx + (c.thumbsup_x_w_thumb * rt + c.thumbsup_x_w_knuckle * rk) / 45.0
        sy = dy + (c.thumbsup_y_w_thumb * rt + c.thumbsup_y_w_knuckle * rk) / 45.0
        return self._filter(sx, sy, t_s)

    def raw_signal(self, hand: Hand, image_size) -> tuple[float, float, float, float]:
        """（右への変位, 上への変位, 親指の角度の変化, 付け根の線の角度の変化）。角度は利用者から見た反時計回りが正（度）。
        変位は、モードに入ったときの手の大きさで正規化する。毎フレームの大きさは、検出が乱れたフレームで極端に小さくなり、
        変位が跳ねて誤発火した（thumbsup01 の 66.0秒: -14.5）。"""
        x, y, ref = palm_position(hand, image_size)
        at, ak = hand_angles(hand, image_size)
        self._last_pose = (x, y, at, ak)
        if self._origin is None:
            self._origin, self._origin_ref, self._origin_angles = (x, y), max(ref, 1e-6), (at, ak)
        ref = self._origin_ref
        return (-(x - self._origin[0]) / ref, -(y - self._origin[1]) / ref,
                _wrap(at - self._origin_angles[0]), _wrap(ak - self._origin_angles[1]))

    def _dir_name(self, side: str) -> str:
        return side
