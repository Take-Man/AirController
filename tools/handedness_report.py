"""手の左右ラベル（MediaPipe の handedness）の、姿勢ごとの反転率を記録から集計する（TASKS-20261025 2.5）。

- 姿勢（モード）は、今の設定のジェスチャー層に記録を流して決める（有効化に関係なく、ジェスチャー層の判定を使う）。
- 反転率: その記録で多い方のラベルと違うラベルが付いたフレームの割合。反転の回数（ラベルが変わった回数）も出す。
- 形からの左右: 指を曲げた手（グー・いいね・ズームの手招き）では、指先が手のひら側にある。手首→人差し指付け根と
  手首→小指付け根の外積（world）の向きと、付け根→指先の向きの内積の符号で、ラベルに頼らずに実際の左右が分かる
  （画像は鏡像にしていないので、+ が右手）。これでラベルの意味（"Right" が作者の右手か）を確かめる。

使い方: .venv/bin/python tools/handedness_report.py [recordings/*.jsonl ...]
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aircontroller.__main__ import load_config  # noqa: E402
from aircontroller.gesture import GestureConfig, GestureEngine, finger_straightness, palm_facing  # noqa: E402
from aircontroller.recording import iter_frames  # noqa: E402
from aircontroller.types import INDEX_MCP, MIDDLE_MCP, PINKY_MCP, RING_MCP, WRIST  # noqa: E402

MCPS = (INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP)


def chirality(hand) -> float | None:
    """形から見た左右: +1 に近いと右手、-1 に近いと左手。指が十分曲がっていなければ None。"""
    w = hand.world
    if np.mean([finger_straightness(w, m) for m in MCPS]) > 0.75:
        return None
    n = np.cross(w[INDEX_MCP] - w[WRIST], w[PINKY_MCP] - w[WRIST])
    tips = np.mean([w[m + 3] for m in MCPS], axis=0) - np.mean([w[m] for m in MCPS], axis=0)
    d = float(np.dot(n / (np.linalg.norm(n) + 1e-12), tips / (np.linalg.norm(tips) + 1e-12)))
    return d


def main(paths: list[str]) -> None:
    gcfg = GestureConfig.from_toml(load_config(None))
    tot = defaultdict(Counter)  # 姿勢 → ラベルの数
    flips = Counter()
    low = defaultdict(list)  # 姿勢 → ラベルのスコア
    chir = defaultdict(list)  # ラベル → 形からの左右
    per_file = []
    for p in paths:
        eng = GestureEngine(gcfg)
        labels, modes, scores = [], [], []
        for f in iter_frames(p):
            f = f.__class__(**{**f.__dict__, "active": True})  # 待機中の区間も、姿勢の判定は同じ
            eng.update(f)
            h = eng.primary_hand(f)
            if h is None:
                continue
            m = eng.state.mode
            if m == "idle" and palm_facing(h) > gcfg.zoom_stay_back_min:
                m = "idle(甲)"
            labels.append(h.handedness)
            modes.append(m)
            scores.append(h.score)
            c = chirality(h)
            if c is not None:
                chir[h.handedness].append(c)
        if not labels:
            continue
        major = Counter(labels).most_common(1)[0][0]
        n_minor = sum(l != major for l in labels)
        per_file.append((Path(p).name, len(labels), major, n_minor))
        for i, (l, m, s) in enumerate(zip(labels, modes, scores)):
            tot[m]["major" if l == major else "minor"] += 1
            low[m].append(s)
            if i and l != labels[i - 1]:
                flips[m] += 1
    print("[記録ごと] フレーム数、多い方のラベル、少ない方のラベルのフレーム")
    for name, n, major, minor in per_file:
        print(f"  {name:42s} {n:6d}  {major:5s}  {minor:5d}（{minor / n * 100:5.2f}%）")
    print("\n[姿勢ごと] 反転率（多い方と違うラベルの割合）、反転の回数、ラベルのスコア（p5）")
    for m in sorted(tot, key=lambda k: -sum(tot[k].values())):
        n = sum(tot[m].values())
        print(f"  {m:10s} フレーム {n:6d}  反転率 {tot[m]['minor'] / n * 100:5.2f}%  反転 {flips[m]:4d} 回  "
              f"スコア p5 {np.percentile(low[m], 5):.3f}")
    print("\n[形から見た左右]（指を曲げたフレーム。+ = 右手、- = 左手）")
    for l, v in chir.items():
        v = np.array(v)
        print(f"  ラベル {l:5s}: フレーム {len(v):6d}  + の割合 {np.mean(v > 0) * 100:5.1f}%  中央値 {np.median(v):+.2f}")


if __name__ == "__main__":
    main(sys.argv[1:] or [str(p) for p in sorted((ROOT / "recordings").glob("*.jsonl"))])
