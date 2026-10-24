"""権限の確認。付与の対象は、実行元のターミナル（またはアプリ）。"""
from __future__ import annotations

import AVFoundation
import Quartz
from ApplicationServices import AXIsProcessTrusted

_CAMERA_STATUS = {0: "未確認", 1: "制限あり", 2: "拒否", 3: "許可"}


def camera_status() -> str:
    return _CAMERA_STATUS.get(int(AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
        AVFoundation.AVMediaTypeVideo)), "不明")


def check(need_events: bool = True, request: bool = True) -> dict[str, bool]:
    """各権限の有無を返す。request=True なら、未付与の権限について OS の確認ダイアログを出し、
    システム設定の一覧に実行元を登録させる（オンにするのは利用者）。"""
    result = {"カメラ": camera_status() in ("許可", "未確認")}  # 未確認なら初回のカメラ起動時に OS が尋ねる
    if need_events:
        result["アクセシビリティ"] = bool(AXIsProcessTrusted())
        result["入力監視"] = bool(Quartz.CGPreflightListenEventAccess())
        if request:
            if not result["入力監視"]:
                Quartz.CGRequestListenEventAccess()
            if not result["アクセシビリティ"]:
                Quartz.CGRequestPostEventAccess()
    return result


GUIDE = {
    "カメラ": "システム設定 → プライバシーとセキュリティ → カメラ",
    "アクセシビリティ": "システム設定 → プライバシーとセキュリティ → アクセシビリティ",
    "入力監視": "システム設定 → プライバシーとセキュリティ → 入力監視",
}


def explain_missing(result: dict[str, bool]) -> str:
    missing = [k for k, v in result.items() if not v]
    lines = ["不足している権限: " + "、".join(missing)]
    for k in missing:
        lines.append(f"  - {k}: {GUIDE[k]} で、実行元のターミナル（Terminal など）をオンにする")
    lines.append("オンにした後、ターミナルを再起動してから、もう一度実行してください。")
    return "\n".join(lines)
