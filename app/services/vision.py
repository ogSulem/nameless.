from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from app.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FaceVerdict:
    has_face: bool
    faces: int
    backend: str
    ms: int
    skipped: bool = False


class FaceDetector:
    """Cheap "is there a human face?" gate in front of photo relay.

    Deliberately single-backend (OpenCV Haar). The old code pulled MediaPipe and
    HuggingFace models — ~800 MB of dependencies, a second conflicting OpenCV
    build, and a startup crash when `mp.solutions` disappeared in 0.10.x. A
    best-effort face check does not need any of that.

    The check never blocks a dialog: if the library is missing or the image fails
    to decode, we allow the photo and log it. Refusing to relay photos because a
    wheel broke would be far worse than a missed check.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cv2 = None
        self._cascade = None
        self._loaded = False
        self._sem = asyncio.Semaphore(max(1, int(settings.vision_concurrency)))

    # ------------------------------------------------------------------ loading
    def _load(self):
        if self._loaded:
            return self._cascade
        self._loaded = True
        if not self._settings.vision_enabled:
            return None
        try:
            import cv2  # imported lazily: a broken wheel must not kill import

            cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"  # type: ignore[attr-defined]
            )
            if cascade.empty():
                raise RuntimeError("empty cascade")
            self._cv2 = cv2
            self._cascade = cascade
        except Exception:
            logger.warning("vision_unavailable: photo face check disabled", exc_info=True)
            self._cascade = None
        return self._cascade

    @property
    def available(self) -> bool:
        return self._load() is not None

    # ------------------------------------------------------------------ detect
    def _sync_detect(self, data: bytes) -> tuple[int, str]:
        cv2 = self._cv2
        cascade = self._cascade
        if cv2 is None or cascade is None:
            return 0, "disabled"
        try:
            import numpy as np

            buf = np.frombuffer(data, dtype=np.uint8)
            img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
            if img is None:
                return 0, "decode_failed"
            max_side = int(self._settings.vision_max_side)
            h, w = img.shape[:2]
            if max(h, w) > max_side:
                scale = max_side / float(max(h, w))
                img = cv2.resize(img, (int(w * scale), int(h * scale)))
            faces = cascade.detectMultiScale(
                cv2.equalizeHist(img), scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
            )
            return len(faces), "haar"
        except Exception:
            logger.warning("vision_detect_failed", exc_info=True)
            return 0, "error"

    async def has_face(self, data: bytes) -> FaceVerdict:
        if not data or not self._load():
            return FaceVerdict(False, 0, "disabled", 0, skipped=True)
        started = time.perf_counter()
        async with self._sem:
            try:
                faces, backend = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, self._sync_detect, data),
                    timeout=float(self._settings.vision_timeout_s),
                )
            except asyncio.TimeoutError:
                logger.warning("vision_timeout")
                return FaceVerdict(False, 0, "timeout", 0, skipped=True)
        ms = int((time.perf_counter() - started) * 1000)
        return FaceVerdict(faces > 0, faces, backend, ms)
