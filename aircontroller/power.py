"""操作のオン・オフと省電力（TASKS-20261025 第3節）。

状態:
- "on": 操作中（有効化オン）。毎フレーム推論する。設定の手が auto_off_after_s 秒見えなかったら、自動でオフにする
  （理由 "hand_absent"）。ドラッグ中なら、無効化の処理（ジェスチャー層の drag_end と release_all）で先にボタンを離す。
- "off": 待機中。推論を off_detect_hz（既定 1Hz）に間引き、ジェスチャーのイベントは出さない。
- "probe": 確認の窓。オフの間の検出で手が新しく見つかったか、手のひらを向けたパーの形が見えたら、wake_probe_s 秒だけ
  毎フレーム推論に上げる。窓の中でパーが wake_hold_s 秒続いたらオンにする（理由 "palm"）。続かなければ "off" に戻る。

1Hz のままでは 0.5 秒の保持を判定できないので、確認の窓を設けた（作者が承認済み）。手を挙げてからオンになるまで、
最大で約 1 秒（次の 1Hz の検出まで）＋ wake_hold_s。
手を映したまま休めていても窓を開け続けないように、窓を開けるのは「手が新しく見えた」か「パーの形が見えた」ときだけにした。

時刻はフレームの t_capture_ns だけを使い、記録を流して再現できる（有効化の状態はフレームの active を入力にする）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .gesture import finger_straightness, palm_facing, thumb_fold_metric
from .types import INDEX_MCP, MIDDLE_MCP, PINKY_MCP, RING_MCP, Hand


@dataclass
class PowerConfig:
    auto_off_after_s: float = 5.0  # 操作中に設定の手がこの秒数見えなかったら、自動でオフ（0 で無効）
    off_detect_hz: float = 1.0  # オフの間の推論の頻度
    wake_enabled: bool = True  # パーでオンにする
    wake_hold_s: float = 0.5  # パーをこの時間保ったらオン
    wake_probe_s: float = 2.0  # 確認の窓の長さ（この間だけ毎フレーム推論する）
    # パー（手のひらをカメラに向け、5本の指を開く）の判定。lab01 の「手のひら」: 手のひらの向き −0.44〜−0.96、
    # 4本の指の真っすぐさ 0.93 以上、親指 −0.20 以下。ズームの手（手の甲）は向きが正、力を抜いた手は指が曲がる（0.76）
    wake_palm_max: float = -0.5  # 手のひらの向き（palm_facing。手のひらがカメラを向くと −1）がこれ未満
    wake_open_min: float = 0.85  # 4本の指の真っすぐさがすべてこれを超える
    wake_thumb_max: float = -0.15  # 親指が開いている（thumb_fold_metric がこれ未満）

    @classmethod
    def from_toml(cls, cfg: dict) -> "PowerConfig":
        e = cfg.get("power", {})
        return cls(**{k: v for k, v in e.items() if k in cls.__dataclass_fields__})


def is_wake_pose(hand: Hand, c: PowerConfig, side: str | None = None) -> bool:
    """side は操作している手の左右（ジェスチャー層の side。手のひらの向きの符号をそろえる）。"""
    if palm_facing(hand, side) >= c.wake_palm_max:
        return False
    if min(finger_straightness(hand.world, m) for m in (INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP)) <= c.wake_open_min:
        return False
    return thumb_fold_metric(hand) < c.wake_thumb_max


class PowerController:
    def __init__(self, cfg: PowerConfig | None = None):
        self.cfg = cfg or PowerConfig()
        self.state = "off"
        self._last_hand_t: int | None = None  # 操作中に、設定の手が最後に見えた時刻
        self._prev_present = False  # オフの間の、直前の検出で手が見えていたか
        self._probe_until = 0
        self._palm_since: int | None = None
        self.wake_progress = 0.0  # パーの保持の経過（0..1）。オーバーレイ用

    def min_interval(self) -> float:
        """推論の最短間隔（秒）。"""
        if self.state == "off" and self.cfg.off_detect_hz > 0:
            return 1.0 / self.cfg.off_detect_hz
        return 0.0

    def activated(self, t_ns: int) -> None:
        """有効化がオンになった（キーでも、パーでも）。手の不在の計時を始め直す。"""
        self.state = "on"
        self._last_hand_t = t_ns
        self._palm_since = None
        self.wake_progress = 0.0

    def deactivated(self) -> None:
        self.state = "off"
        self._prev_present = True  # オフにした直後に、映っている手で窓を開けない（パーなら開く）
        self._palm_since = None
        self.wake_progress = 0.0

    def update(self, t_ns: int, active: bool, hand: Hand | None, side: str | None = None) -> str | None:
        """推論したフレームごとに呼ぶ。hand はジェスチャー層が選んだ手（設定の手でなければ None）。
        返り値: "auto_off"（オフにすべき）/ "wake"（オンにすべき）/ None。"""
        c = self.cfg
        if active:
            if self.state != "on":
                self.activated(t_ns)
            if hand is not None:
                self._last_hand_t = t_ns
            elif c.auto_off_after_s > 0 and (t_ns - self._last_hand_t) / 1e9 >= c.auto_off_after_s:
                return "auto_off"
            return None
        if self.state == "on":
            self.deactivated()
        if not c.wake_enabled:
            return None
        present = hand is not None
        palm = present and is_wake_pose(hand, c, side)
        if self.state == "off":
            if present and (not self._prev_present or palm):
                self.state = "probe"
                self._probe_until = t_ns + int(c.wake_probe_s * 1e9)
                self._palm_since = None
            self._prev_present = present
        if self.state == "probe":
            if palm:
                if self._palm_since is None:
                    self._palm_since = t_ns
                held = (t_ns - self._palm_since) / 1e9
                self.wake_progress = min(1.0, held / c.wake_hold_s) if c.wake_hold_s > 0 else 1.0
                if held >= c.wake_hold_s:
                    return "wake"
            else:
                self._palm_since = None
                self.wake_progress = 0.0
            if t_ns >= self._probe_until:
                self.state = "off"
                self._prev_present = present
                self.wake_progress = 0.0
        return None
