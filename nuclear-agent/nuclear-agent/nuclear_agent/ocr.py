"""Optional OCR fallback for pages whose text is drawn on a canvas or in images.

Uses Tesseract through pytesseract if both are installed. If not, OCR is
simply unavailable and the agent relies on DOM text (it never pretends).
"""

from __future__ import annotations

import io
import logging
import shutil

log = logging.getLogger(__name__)
_STATE: dict = {}


def available() -> bool:
    if "ok" in _STATE:
        return _STATE["ok"]
    ok = False
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401

        if shutil.which("tesseract"):
            ok = True
        else:
            # Default Windows install location
            import os
            win = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
            if os.path.exists(win):
                pytesseract.pytesseract.tesseract_cmd = win
                ok = True
    except ImportError:
        ok = False
    _STATE["ok"] = ok
    return ok


def image_to_text(image_bytes: bytes, max_chars: int = 2000) -> str:
    if not available():
        return ""
    try:
        import pytesseract
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        text = pytesseract.image_to_string(img, timeout=20)
        return " ".join(text.split())[:max_chars]
    except Exception as exc:  # OCR must never break a task
        log.warning("OCR failed: %s", exc)
        return ""
