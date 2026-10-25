"""ピンチの成立を止める条件の検証（SPEC 13.42。実装はしていない。判定のコードは変えず、ピンチの成立だけを外から止めて数える）。
1: 親指先が画像の端の近く（y > edge_y、または x が端から edge_x 以内）
2: 親指先が直前 k フレームの間に、1 フレームで手の大きさ（画像上の手首→中指付け根）の jump 倍以上跳んだ

使う記録: 端への往復（labpos-20261026-043902、意図しないピンチ）、位置ごと（labpos-20261026-042216、クリックの区間は意図したピンチ）、
実際の操作の記録（tests/fixtures の use01〜05, abs02, pinch01, right01）。
使い方: .venv/bin/python tools/pinch_gate_eval.py
"""
import json, sys
from collections import Counter, deque
import numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import aircontroller.gesture as G
from aircontroller.__main__ import load_config
from aircontroller.recording import iter_frames
from aircontroller.types import THUMB_TIP, WRIST, MIDDLE_MCP

orig_metrics = G.image_pinch_metrics


class GatedEngine(G.GestureEngine):
    def __init__(self, cfg, edge_y=None, edge_x=None, jump=None, k=3):
        super().__init__(cfg)
        self.edge_y, self.edge_x, self.jump, self.k = edge_y, edge_x, jump, k
        self.hist = deque(maxlen=k + 1)
        self.gated = 0

    def update(self, frame):
        h = self._best(frame)
        block = False
        if h is not None:
            px = h.landmarks[:, :2] * np.asarray(frame.image_size)
            ref = float(np.linalg.norm(px[MIDDLE_MCP] - px[WRIST])) or 1.0
            tx, ty = h.landmarks[THUMB_TIP, 0], h.landmarks[THUMB_TIP, 1]
            if self.edge_y is not None and ty > self.edge_y:
                block = True
            if self.edge_x is not None and (tx < self.edge_x or tx > 1 - self.edge_x):
                block = True
            if self.jump is not None and self.hist:  # 直前 k フレームの、各フレーム間の跳びの最大
                pts = list(self.hist) + [px[THUMB_TIP]]
                if max(float(np.linalg.norm(b - a)) / ref for a, b in zip(pts, pts[1:])) >= self.jump:
                    block = True
            self.hist.append(px[THUMB_TIP])
        else:
            self.hist.clear()
        if block and self._pinch_kind is None:
            G.image_pinch_metrics = lambda hand, size: {"thumb_index_n": 9.0, "thumb_middle_n": 9.0}
            try:
                ev = super().update(frame)
            finally:
                G.image_pinch_metrics = orig_metrics
            return ev
        return super().update(frame)


def count(path, mk, labels_kind=None):
    segs = json.load(open(path.replace('.jsonl', '.labels.json')))['segments'] if labels_kind else None
    eng = mk()
    c = Counter()
    for f in iter_frames(path):
        if labels_kind:
            f = f.__class__(**{**f.__dict__, 'active': True})
        ev = eng.update(f)
        lab = next((s['label'] for s in segs if s['t_start_ns'] <= f.t_capture_ns < s['t_end_ns']), None) if segs else 'all'
        if lab is None:
            continue
        g = lab.split('/')[-1]
        for e in ev:
            if e.kind == 'pinch_start':
                b = e.data.get('button')
                if labels_kind == 'edge':
                    c[('誤' if g in ('move', 'hold', 'peace_move') else '意図') + b] += 1
                elif labels_kind == 'basic':
                    c[('意図' if g == 'click' else '誤') + b] += 1
                else:
                    c[b] += 1
            if e.kind in ('click', 'drag_start') and not labels_kind:
                c[e.kind] += 1
    return c


cfg = G.GestureConfig.from_toml(load_config(None), (1440.0, 900.0))
cfg.roi_adapt = False
R = str(Path(__file__).resolve().parent.parent) + '/'
edge = R + 'recordings/labpos-20261026-043902.jsonl'
basic = R + 'recordings/labpos-20261026-042216.jsonl'
fixtures = [R + f'tests/fixtures/{n}.jsonl' for n in ('use01', 'use02', 'use03', 'use04', 'use05', 'abs02', 'pinch01', 'right01')]
variants = [("なし（今）", {}),
            ("1: y>0.95", dict(edge_y=0.95)), ("1: y>0.90", dict(edge_y=0.90)), ("1: y>0.90, x端0.02", dict(edge_y=0.90, edge_x=0.02)),
            ("2: 跳び0.5/3f", dict(jump=0.5, k=3)), ("2: 跳び0.3/3f", dict(jump=0.3, k=3)), ("2: 跳び0.3/5f", dict(jump=0.3, k=5)),
            ("1+2: y>0.95 + 跳び0.3/3f", dict(edge_y=0.95, jump=0.3, k=3)), ("1+2: y>0.90 + 跳び0.3/3f", dict(edge_y=0.90, jump=0.3, k=3))]
print("条件 | 端への往復: 意図しない左/右 | 位置ごと: クリックの区間の左ピンチ / 誤 | 実際の操作の記録（use01-05, abs02, pinch01, right01）: 左ピンチ 右ピンチ クリック ドラッグ")
for name, kw in variants:
    mk = lambda: GatedEngine(cfg, **kw)
    e = count(edge, mk, 'edge')
    b = count(basic, mk, 'basic')
    tot = Counter()
    for fx in fixtures:
        tot += count(fx, mk)
    print(f"{name:26s} | {e['誤left']:3d} / {e['誤right']:2d} | {b['意図left']:3d} / {b['誤left'] + b['誤right']:2d} | "
          f"{tot['left']:4d} {tot['right']:3d} {tot['click']:4d} {tot['drag_start']:3d}")

print("\n条件 2（跳び 0.3/3f）で、実際の操作の記録から消えた・増えたピンチ")
for fx in fixtures:
    def starts(eng):
        out = []
        t0 = None
        for f in iter_frames(fx):
            t0 = t0 or f.t_capture_ns
            for e in eng.update(f):
                if e.kind in ('pinch_start', 'click', 'drag_start'):
                    out.append((round((e.t_ns - t0) / 1e9, 2), e.kind, e.data.get('button', '')))
        return out
    a, b = starts(G.GestureEngine(cfg)), starts(GatedEngine(cfg, jump=0.3, k=3))
    sa, sb = set(a), set(b)
    if sa != sb:
        print(' ', fx.split('/')[-1], '消えた', sorted(sa - sb), '増えた', sorted(sb - sa))
