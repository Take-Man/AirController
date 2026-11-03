"""mediapipe を、OpenCV（cv2）が無くても読み込めるようにする（SPEC 13.68）。

mediapipe 1.1.1 は `import mediapipe` の中で `tasks/python/vision/drawing_utils.py` を読み、それが `import cv2` をする。
AirController は drawing_utils を使わない（推論だけ）。配布する .app には OpenCV を入れないので、cv2 が無いときは、
使おうとすると分かる中身のない cv2 を先に置いてから読む。cv2 が入っていれば（開発用の .venv）、そのまま本物を使う。
"""
from __future__ import annotations

import sys
import types


class _NoCv2(types.ModuleType):
    def __getattr__(self, name: str):
        raise ImportError(f"OpenCV（cv2）は入っていません（cv2.{name}。配布版は OpenCV を同梱しない）")


def _ensure_cv2() -> bool:
    """本物の cv2 が読めれば True。読めなければ中身のない cv2 を置いて False。"""
    if "cv2" in sys.modules:
        return not isinstance(sys.modules["cv2"], _NoCv2)
    try:
        import cv2  # noqa: F401
    except ImportError:
        sys.modules["cv2"] = _NoCv2("cv2")
        return False
    return True


HAVE_CV2 = _ensure_cv2()

import mediapipe as mp  # noqa: E402

__all__ = ["HAVE_CV2", "mp"]
