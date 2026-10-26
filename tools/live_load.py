"""設定画面を開いている間と閉じている間で、run の CPU と遅延（p95）が変わらないかを測る（SPEC 13.47、設定画面の作業の第6節）。

使い方: run を起動して有効化し（右 Command）、手をカメラに映したまま、別のターミナルで
  .venv/bin/python tools/live_load.py [--seconds 60]
「つながない → 設定画面の代わりにつなぐ（SSE を受け続ける）→ つながない」の 3 区間を測り、区間ごとに run の CPU（1 コア比）と、
フレーム取得 → 出力の遅延の p50 / p95 を出す（run の /stats から。run 自身が数えた値）。
注意: つないでいる間は設定モードなので、run は出力を止め、オフ（1Hz）でも毎フレーム推論する。比べるときは、3 区間とも有効化したままにする。
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60.0, help="1 区間の長さ（秒）")
    ap.add_argument("--live-json", default=str(ROOT / "live.json"), help="run が書いた live.json（既定はリポジトリの直下）")
    args = ap.parse_args(argv)
    try:
        info = json.loads(Path(args.live_json).read_text())
    except (OSError, ValueError):
        print("live.json がありません。run を起動してから実行してください")
        return 2
    port, token = info["port"], info["token"]
    hd = {"Host": f"127.0.0.1:{port}", "X-Token": token}

    def stats(window: float) -> dict:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", f"/stats?window={window}", headers=hd)
        out = json.loads(c.getresponse().read())
        c.close()
        return out

    stop = threading.Event()
    got = {"n": 0}

    def client():
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", "/stream", headers=hd)
        r = c.getresponse()
        while not stop.is_set():
            line = r.fp.readline()
            if not line:
                break
            if line.startswith(b"data: "):
                got["n"] += 1
        c.close()

    rows = []
    for name, connect in (("つながない", False), ("つなぐ（設定モード）", True), ("つながない", False)):
        th = None
        if connect:
            stop.clear()
            th = threading.Thread(target=client, daemon=True)
            th.start()
        print(f"{name}: {args.seconds:.0f} 秒 測ります…", flush=True)
        s0 = stats(args.seconds)
        time.sleep(args.seconds)
        s1 = stats(args.seconds)
        if th:
            stop.set()
            th.join(timeout=3)
            time.sleep(1.0)  # 設定モードを抜けるのを待つ
        cpu = (s1["cpu_s"] - s0["cpu_s"]) / (s1["wall"] - s0["wall"]) * 100
        rows.append((name, cpu, s1["frames"], s1["lat_p50"], s1["lat_p95"], got["n"] if connect else None))
    print("\n| 区間 | CPU（1 コア比） | フレーム | 遅延 p50 | 遅延 p95 | 受けた値 |")
    print("|---|---|---|---|---|---|")
    for name, cpu, n, p50, p95, recv in rows:
        print(f"| {name} | {cpu:.1f}% | {n} | {p50} ms | {p95} ms | {recv if recv is not None else '—'} |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
