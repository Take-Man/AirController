"""位置によって変えうる閾値の入口（設定画面の作業の第7節、SPEC 13.48。13.41 の (b)「操作範囲内の位置によって閾値を変える」の準備）。

ピンチ・右クリック・親指の畳み・ピースの条件（指の真っすぐさ）の閾値は、ジェスチャー層（GestureEngine.update）がすべてここから取る。
`PositionThresholds.at(palm, roi)` は手のひら中心の位置を受け取るが、今は位置を無視して、いつも設定の値を返す（挙動は変えない）。

位置ごとの補正の形式（余地だけ。まだ使わない）: 設定の [position] 表に、項目ごとの 3×3 の倍率を書く。
    [position]
    pinch_on = [[1.0, 1.0, 1.0], [1.0, 1.0, 1.0], [1.0, 1.0, 1.1]]   # 行 = 上・中・下、列 = 利用者から見た左・中・右
マスは操作範囲（roi）を 3×3 に分けたもの（範囲の外は、いちばん近いマス）。読み込むときに形式だけ確かめる。
"""
from __future__ import annotations

from dataclasses import dataclass, fields

# 位置依存の候補（パラメータ台帳の position = True と同じ。テストで確かめる）
POSITION_KEYS = ("pinch_on", "pinch_off", "drag_off", "right_on", "right_off", "right_arm",
                 "thumb_fold_on", "thumb_fold_off", "index_up_enter", "index_up_exit", "peace_exit_straight", "ring_down_max")


@dataclass(frozen=True)
class Thresholds:
    pinch_on: float
    pinch_off: float
    drag_off: float
    right_on: float
    right_off: float
    right_arm: float
    thumb_fold_on: float
    thumb_fold_off: float
    index_up_enter: float
    index_up_exit: float
    peace_exit_straight: float
    ring_down_max: float


def validate_grid(grid) -> dict[str, tuple[tuple[float, ...], ...]]:
    """[position] 表の形式を確かめる（項目は POSITION_KEYS、値は正の数の 3×3）。"""
    if not grid:
        return {}
    if not isinstance(grid, dict):
        raise ValueError("[position] は表（項目 = 3×3 の倍率）で書きます")
    out = {}
    for k, m in grid.items():
        if k not in POSITION_KEYS:
            raise ValueError(f"[position] {k}: 位置で変えられる項目ではありません（{', '.join(POSITION_KEYS)}）")
        if not (isinstance(m, (list, tuple)) and len(m) == 3 and all(isinstance(r, (list, tuple)) and len(r) == 3 for r in m)):
            raise ValueError(f"[position] {k}: 3×3 の倍率で書きます")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for r in m for v in r):
            raise ValueError(f"[position] {k}: 倍率は正の数です")
        out[k] = tuple(tuple(float(v) for v in r) for r in m)
    return out


def cell(palm: tuple[float, float], roi: tuple[float, float, float, float]) -> tuple[int, int]:
    """手のひら中心（画像座標）が、操作範囲を 3×3 に分けたどのマスか（行 = 上 0・中 1・下 2、列 = 利用者から見た左 0・中 1・右 2）。
    画像は鏡像でないので、画像の右が利用者の左。範囲の外は、いちばん近いマス。"""
    x0, y0, w, h = roi
    u = 1.0 - (palm[0] - x0) / w if w > 0 else 0.5
    v = (palm[1] - y0) / h if h > 0 else 0.5
    return min(2, max(0, int(v * 3))), min(2, max(0, int(u * 3)))


class PositionThresholds:
    def __init__(self, cfg):
        self.cfg = cfg
        self.refresh()

    def refresh(self) -> None:
        """設定から作り直す（初期化時と、実行中に設定を差し替えたとき）。"""
        self._base = Thresholds(**{f.name: getattr(self.cfg, f.name) for f in fields(Thresholds)})
        self.grid = validate_grid(getattr(self.cfg, "position_scale", None))

    @property
    def base(self) -> Thresholds:
        return self._base

    def at(self, palm: tuple[float, float] | None, roi: tuple[float, float, float, float] | None) -> Thresholds:
        """手のひら中心の位置での閾値。今は位置を無視して、設定の値を返す（位置による補正は SPEC 13.41 の (b) で、測ってから入れる）。"""
        return self._base
