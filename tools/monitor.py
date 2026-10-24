"""AirController（run）のプロセスと Mac の状態を、一定間隔で記録する（TASKS-20261025 第5節）。管理者権限は要らない。

記録する列: 時刻、プロセスの常駐メモリ（RSS）と累積 CPU 時間・スレッド数（ps）、熱の状態（pmset -g therm の警告）、
バッテリー（ioreg AppleSmartBattery: 残量 %、電圧、電流 → 消費電力 W、外部電源の有無）。
バッテリーの消費電力は Mac 全体の値なので、状態ごとに比べるときは、ほかのアプリ・画面の明るさを同じにして、
外部電源を外して測る。

使い方（run を起動した後、別のターミナルで）:
  .venv/bin/python tools/monitor.py [--interval 10] [--pid PID]
  → logs/monitor-YYYYmmdd-HHMMSS.csv。Ctrl+C で終了。集計は tools/load_report.py
"""
from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COLS = ["wall", "t_mono", "pid", "rss_mb", "cpu_s", "threads", "therm", "batt_pct", "volt_v", "amp_a", "power_w",
        "external"]


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def find_pid() -> int | None:
    out = sh(["pgrep", "-f", "aircontroller run"]).split()
    pids = [int(p) for p in out if p.isdigit()]
    return max(pids) if pids else None


def cpu_seconds(s: str) -> float:
    """ps の time（[[dd-]hh:]mm:ss.ss）を秒に。"""
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [float(x) for x in s.split(":")]
    sec = 0.0
    for x in parts:
        sec = sec * 60 + x
    return days * 86400 + sec


def proc(pid: int) -> tuple[float, float, int] | None:
    out = sh(["ps", "-o", "rss=,time=", "-p", str(pid)]).split()
    if len(out) < 2:
        return None
    threads = max(0, len(sh(["ps", "-M", "-p", str(pid)]).strip().splitlines()) - 1)
    return int(out[0]) / 1024, cpu_seconds(out[1]), threads


def battery() -> dict:
    out = sh(["ioreg", "-rn", "AppleSmartBattery"])

    def val(key):
        m = re.search(rf'"{key}" = (\S+)', out)
        return m.group(1) if m else None

    def num(key):
        v = val(key)
        if v is None or not v.lstrip("-").isdigit():
            return None
        n = int(v)
        return n - 2**64 if n >= 2**63 else n  # 放電中の電流は符号なしで出ることがある

    volt, amp = num("Voltage"), num("InstantAmperage") or num("Amperage")
    return {"batt_pct": num("CurrentCapacity"), "volt_v": volt / 1000 if volt else None,
            "amp_a": amp / 1000 if amp is not None else None,
            "power_w": round(-(volt / 1000) * (amp / 1000), 2) if volt and amp is not None else None,
            "external": val("ExternalConnected")}


def therm() -> str:
    out = sh(["pmset", "-g", "therm"])
    lines = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("Note:")]
    return "; ".join(lines) or "警告なし"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--pid", type=int)
    args = ap.parse_args()
    path = ROOT / "logs" / f"monitor-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    path.parent.mkdir(exist_ok=True)
    print(f"記録先: {path}（{args.interval:.0f}秒ごと、Ctrl+C で終了）")
    with path.open("w", newline="") as fp:
        w = csv.DictWriter(fp, COLS)
        w.writeheader()
        try:
            while True:
                pid = args.pid or find_pid()
                row = {"wall": time.strftime("%Y-%m-%dT%H:%M:%S"), "t_mono": round(time.monotonic(), 2),
                       "pid": pid, "therm": therm()}
                p = proc(pid) if pid else None
                if p:
                    row.update(rss_mb=round(p[0], 1), cpu_s=round(p[1], 2), threads=p[2])
                row.update(battery())
                w.writerow(row)
                fp.flush()
                print(f"{row['wall']}  pid {pid}  RSS {row.get('rss_mb')}MB  CPU累積 {row.get('cpu_s')}s  "
                      f"電力 {row.get('power_w')}W  残量 {row.get('batt_pct')}%  外部電源 {row.get('external')}  "
                      f"{row['therm']}", flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print(f"\n終了しました: {path}")


if __name__ == "__main__":
    sys.exit(main())
