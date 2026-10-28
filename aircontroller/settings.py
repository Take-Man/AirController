"""設定の層・保存・履歴（設定画面の土台、SPEC 13.43）。

層（後ろほど優先）: config.toml（既定値と説明）< calibration.toml（利用者と測定が書く）< auto.toml（操作範囲の自動補正）。
auto.toml は、書いたときの下の層の値（[base.<節>]）を一緒に持つ。下の層の値がその後変わったら（calibrate で測り直した、
手で直した）、auto.toml のその値は古いものとして使わない。以前 run が calibration.toml に書いた roi_* は、そのまま読む。

保存は一時ファイルに書いてから置き換える（原子的）。calibration.toml を保存するたびに、利用者の層（calibration.toml と
auto.toml）の全体を settings-history/YYYYmmdd-HHMMSS.toml に残し、変わった項目を settings-history/changes.jsonl に書く。
スナップショットは最新 KEEP 件を保つ。名前を付けた状態は settings-history/named/<名前>.toml（数の上限なし）。
auto.toml の保存（run の終了時）は changes.jsonl にだけ書き、スナップショットは作らない（毎回の run で履歴が流れないように）。
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
import tomllib
from pathlib import Path

KEEP = 50


# ---------------------------------------------------------------- TOML の書き出し（値は数値・真偽・文字列・それらのリスト）
def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def dump_toml(data: dict, header: str = "") -> str:
    """入れ子の表（{節: {キー: 値}} や {層: {節: {...}}}）を TOML にする。"""
    lines = [f"# {line}" for line in header.splitlines()] if header else []

    def table(prefix: str, d: dict) -> None:
        scalars = {k: v for k, v in d.items() if not isinstance(v, dict)}
        subs = {k: v for k, v in d.items() if isinstance(v, dict)}
        if prefix and (scalars or not subs):
            lines.append(f"\n[{prefix}]")
        lines.extend(f"{k} = {toml_value(v)}" for k, v in scalars.items())
        for k, v in subs.items():
            table(f"{prefix}.{k}" if prefix else k, v)

    table("", data)
    return "\n".join(lines).lstrip("\n") + "\n"


def write_atomic(path: Path, text: str) -> None:
    """同じディレクトリの一時ファイルに書いてから置き換える（途中で落ちても、元のファイルか新しいファイルのどちらかが残る）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(text)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as fp:
        return tomllib.load(fp)


def _sections(d: dict) -> dict[str, dict]:
    return {k: dict(v) for k, v in d.items() if isinstance(v, dict)}


def _merge(dst: dict, src: dict) -> None:
    for section, values in _sections(src).items():
        dst.setdefault(section, {}).update(values)


def _same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(float(a), float(b), rel_tol=0, abs_tol=5e-5)  # 4 桁に丸めて書くため
        except (TypeError, ValueError):
            return False
    return a == b


class SettingsStore:
    def __init__(self, root: Path, config: Path | None = None):
        self.root = Path(root)
        self.config = Path(config) if config else self.root / "config.toml"
        self.calibration = self.root / "calibration.toml"
        self.auto = self.root / "auto.toml"
        self.history = self.root / "settings-history"
        self.named = self.history / "named"
        self.changes = self.history / "changes.jsonl"

    # ------------------------------------------------------------ 読み込み
    def layer(self, name: str) -> dict:
        """利用者の層の中身。calibration は {節: {キー: 値}}、auto は {節: {...}, "base": {節: {...}}}。"""
        return _read(self.calibration if name == "calibration" else self.auto)

    def load(self, calibration: bool = True) -> dict:
        """3 つの層を重ねた設定（calibration=False なら config.toml だけ）。"""
        cfg = _read(self.config)
        if not calibration:
            return cfg
        return self.load_state(self.state())

    def load_state(self, state: dict) -> dict:
        """config.toml に、利用者の層の状態（{"calibration": ..., "auto": ...}。スナップショットの中身）を重ねた設定。"""
        cfg = _read(self.config)
        _merge(cfg, state.get("calibration", {}))
        _merge(cfg, self.valid_auto(cfg, state.get("auto", {})))
        return cfg

    def valid_auto(self, below: dict, auto: dict | None = None) -> dict[str, dict]:
        """auto.toml のうち、書いたときの下の層の値（base）が今の下の層の値と同じ項目だけ。"""
        auto = self.layer("auto") if auto is None else auto
        base = _sections(auto.get("base", {}))
        out: dict[str, dict] = {}
        for section, values in _sections({k: v for k, v in auto.items() if k != "base"}).items():
            for k, v in values.items():
                b = base.get(section, {})
                if k in b and k in below.get(section, {}) and not _same(b[k], below[section][k]):
                    continue  # 下の層が変わった（測り直した・手で直した）: 自動補正の値は古い
                out.setdefault(section, {})[k] = v
        return out

    def state(self) -> dict:
        """利用者の層の全体（スナップショットの中身）。"""
        return {"calibration": self.layer("calibration"), "auto": self.layer("auto")}

    # ------------------------------------------------------------ 保存
    def save(self, updates: dict[str, dict], route: str, note: str = "") -> list[dict]:
        """calibration.toml の値を更新し（他の値は残す）、スナップショットと変更の記録を残す。変わった項目を返す。"""
        if not self._snapshots():
            self._snapshot("初期状態（最初の保存の前）")
        data = _sections(self.layer("calibration"))
        changes = self._apply(data, updates)
        header = (f"{note or route}（{time.strftime('%Y-%m-%d %H:%M')}）。config.toml の値を上書きする。消せば config.toml の値に戻る\n"
                  "書いた経路ごとの履歴は settings-history/changes.jsonl")
        write_atomic(self.calibration, dump_toml(data, header))
        self._log(changes, "calibration", route)
        self._snapshot(route, note=note)
        return changes

    def save_auto(self, updates: dict[str, dict], route: str = "run") -> list[dict]:
        """auto.toml の値を更新する。書いたときの下の層の値を base に残す。スナップショットは作らない。"""
        below = self.load(calibration=False)
        _merge(below, self.layer("calibration"))
        valid = self.valid_auto(below)  # base が合わない古い値は使われていないので捨てる
        old_base = _sections(self.layer("auto").get("base", {}))
        data = {s: dict(v) for s, v in valid.items()}
        base = {s: {k: old_base[s][k] for k in v if k in old_base.get(s, {})} for s, v in valid.items()}
        changes = []
        for section, values in updates.items():
            for k, v in values.items():
                prev = valid.get(section, {}).get(k, below.get(section, {}).get(k))
                if not _same(prev, v):
                    changes.append({"key": f"{section}.{k}", "before": prev, "after": v})
                data.setdefault(section, {})[k] = v
                b = base.setdefault(section, {})
                if k in below.get(section, {}):
                    b[k] = below[section][k]
                else:
                    b.pop(k, None)
        out: dict = {s: v for s, v in data.items() if v}
        base = {s: v for s, v in base.items() if v}
        if base:
            out["base"] = base
        header = (f"{route} が書いた値（{time.strftime('%Y-%m-%d %H:%M')}）。calibration.toml の値を上書きする。消せば calibration.toml の値に戻る\n"
                  "[base.*] は書いたときの下の層の値。下の層の値が変わったら（calibrate で測り直した、手で直した）、その項目は使わない")
        write_atomic(self.auto, dump_toml(out, header))
        self._log(changes, "auto", route)
        return changes

    @staticmethod
    def _apply(data: dict[str, dict], updates: dict[str, dict]) -> list[dict]:
        changes = []
        for section, values in updates.items():
            for k, v in values.items():
                prev = data.get(section, {}).get(k)
                if not _same(prev, v):
                    changes.append({"key": f"{section}.{k}", "before": prev, "after": v})
                data.setdefault(section, {})[k] = v
        return changes

    def _log(self, changes: list[dict], layer: str, route: str) -> None:
        if not changes:
            return
        self.history.mkdir(parents=True, exist_ok=True)
        t = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self.changes.open("a", encoding="utf-8") as fp:
            for c in changes:
                fp.write(json.dumps({"t": t, "key": c["key"], "before": c["before"], "after": c["after"],
                                     "layer": layer, "route": route}, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------ スナップショット
    def _snapshots(self) -> list[Path]:
        if not self.history.exists():
            return []
        snaps = [(m, p) for p in self.history.glob("*.toml") if (m := re.fullmatch(r"(\d{8}-\d{6})(?:-(\d+))?", p.stem))]
        # 同じ秒に複数あるときは YYYYmmdd-HHMMSS, -2, -3, ... の順
        return [p for m, p in sorted(snaps, key=lambda x: (x[0][1], int(x[0][2] or 1)))]

    def snapshots(self) -> list[dict]:
        """スナップショットの一覧（古い順）。key は中身（利用者の層）の識別子で、同じ設定なら同じ値。"""
        out = []
        for p in self._snapshots():
            d = _read(p)
            meta = d.get("meta", {})
            out.append({"path": p, "id": p.stem, "route": meta.get("route", ""), "t": meta.get("t", ""), "note": meta.get("note", ""),
                        "key": state_key(d)})
        return out

    def names(self) -> dict[str, str]:
        """設定の中身（key）→ 表示する名前。名前を付けた状態があればその名前、無ければ、その中身が最初に現れたスナップショットの名前
        （同じ設定には、いつも同じ名前を出す）。"""
        out: dict[str, str] = {}
        for s in self.snapshots():
            out.setdefault(s["key"], s["id"])
        for n in self.named_states():
            out[state_key(_read(self.named / f"{n}.toml"))] = n
        return out

    def current_key(self) -> str:
        return state_key(self.state())

    def _snapshot(self, route: str, path: Path | None = None, note: str = "") -> Path:
        """note: 保存したときのコメント（設定画面の「メモ」など。履歴に出す）。"""
        if path is None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path, n = self.history / f"{stamp}.toml", 2
            while path.exists():
                path, n = self.history / f"{stamp}-{n}.toml", n + 1
        meta = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": route, **({"note": note} if note else {})}
        data = {"meta": meta, **self.state()}
        write_atomic(path, dump_toml(data, "設定のスナップショット（利用者の層: calibration.toml と auto.toml の全体）"))
        if path.parent == self.history:
            for old in self._snapshots()[:-KEEP]:
                old.unlink()
        return path

    def name(self, name: str) -> Path:
        """今の状態に名前を付けて残す（同じ名前は上書き）。"""
        if not re.fullmatch(r"[^/\\.][^/\\]{0,63}", name):
            raise ValueError(f"使えない名前です: {name!r}")
        return self._snapshot(f"名前: {name}", self.named / f"{name}.toml")

    def named_states(self) -> list[str]:
        return sorted(p.stem for p in self.named.glob("*.toml")) if self.named.exists() else []

    def restore(self, path: Path, route: str) -> list[dict]:
        """スナップショットの状態に戻す（戻したことも保存の 1 回として残す）。"""
        snap = _read(Path(path))
        changes = []
        for layer, target in (("calibration", self.calibration), ("auto", self.auto)):
            old, new = self.layer(layer), snap.get(layer, {})
            flat_old, flat_new = _flatten(old), _flatten(new)
            ch = [{"key": k, "before": flat_old.get(k), "after": flat_new.get(k)}
                  for k in sorted(set(flat_old) | set(flat_new)) if not _same(flat_old.get(k), flat_new.get(k))]
            if new:
                write_atomic(target, dump_toml(new, f"{route}（{time.strftime('%Y-%m-%d %H:%M')}）で戻した値"))
            elif target.exists():
                target.unlink()  # スナップショットの時点ではこの層は無かった
            self._log(ch, layer, route)
            changes += ch
        self._snapshot(route)
        return changes

    def undo(self) -> list[dict]:
        """前の保存に戻す: 最新のスナップショットの 1 つ前の状態にする。"""
        snaps = self._snapshots()
        if len(snaps) < 2:
            raise ValueError("戻せる前の保存がありません")
        return self.restore(snaps[-2], f"前の保存に戻す（{snaps[-2].stem}）")

    def restore_named(self, name: str) -> list[dict]:
        p = self.named / f"{name}.toml"
        if not p.exists():
            raise ValueError(f"名前を付けた状態がありません: {name}")
        return self.restore(p, f"名前を付けた状態に戻す（{name}）")


def state_key(d: dict) -> str:
    """利用者の層（calibration と auto）の中身の識別子。スナップショットの見出しや時刻は含めない。"""
    import hashlib

    body = {"calibration": d.get("calibration", {}), "auto": d.get("auto", {})}
    return hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out
