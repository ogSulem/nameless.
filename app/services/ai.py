from __future__ import annotations

import logging
import asyncio
import threading
import numpy as np
import cv2
from typing import Any

logger = logging.getLogger(__name__)

class AIService:
    _insight_model: Any | None = None
    _insight_init_error: str | None = None
    _insight_init_lock: asyncio.Lock | None = None
    _insight_detect_lock = threading.Lock()

    def __init__(self, api_key: str | None = None):
        # Local, fast, and reliable face detection.
        # Primary: OpenCV Haar cascade (always available offline).
        # Optional: MediaPipe solutions API if present in the environment.
        self._face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        self._eye_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_eye.xml"
        )

    async def _get_insight_model(self) -> Any | None:
        """Lazy-init InsightFace model once per process. Returns None if unavailable."""
        if AIService._insight_model is not None:
            return AIService._insight_model
        if AIService._insight_init_error is not None:
            return None

        if AIService._insight_init_lock is None:
            AIService._insight_init_lock = asyncio.Lock()

        async with AIService._insight_init_lock:
            if AIService._insight_model is not None:
                return AIService._insight_model
            if AIService._insight_init_error is not None:
                return None

            try:
                import insightface  # type: ignore

                model = insightface.app.FaceAnalysis(name="buffalo_l")
                # ctx_id=-1 forces CPU.
                model.prepare(ctx_id=-1, providers=["CPUExecutionProvider"])
                AIService._insight_model = model
                logger.info("insightface_ready")
                return model
            except Exception as e:
                AIService._insight_init_error = str(e)
                logger.exception("insightface_init_failed")
                return None

    async def detect_human_with_meta(self, photo_bytes: bytes) -> tuple[bool, dict[str, Any]]:
        """Like detect_human(), but also returns debugging metadata for admin channel."""
        try:
            model = await self._get_insight_model()
            loop = asyncio.get_event_loop()

            def sync_detect() -> tuple[bool, dict[str, Any]]:
                nparr = np.frombuffer(photo_bytes, np.uint8)
                image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                if image is None:
                    return (False, {"backend": "decode", "error": "decode_failed"})

                h, w = image.shape[:2]

                # 1) InsightFace first (preferred)
                if model is not None:
                    try:
                        with AIService._insight_detect_lock:
                            faces = model.get(image)
                        return (
                            len(faces) > 0,
                            {
                                "backend": "insightface_buffalo_l",
                                "faces": int(len(faces)),
                                "w": int(w),
                                "h": int(h),
                                "error": None,
                            },
                        )
                    except Exception as e:
                        # fall back to OpenCV
                        insight_err = str(e)
                else:
                    insight_err = AIService._insight_init_error

                # 2) Strict OpenCV fallback
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                min_dim = min(w, h)
                min_size = max(30, int(min_dim * 0.12))

                faces = self._face_cascade.detectMultiScale(
                    gray,
                    scaleFactor=1.2,
                    minNeighbors=8,
                    minSize=(min_size, min_size),
                )

                eyes_total = 0
                accepted = False
                for (x, y, fw, fh) in faces:
                    y2 = y + max(1, int(fh * 0.6))
                    roi = gray[y:y2, x : x + fw]
                    if roi.size == 0:
                        continue

                    eyes = self._eye_cascade.detectMultiScale(
                        roi,
                        scaleFactor=1.1,
                        minNeighbors=6,
                        minSize=(max(15, int(fw * 0.15)), max(15, int(fh * 0.15))),
                    )
                    eyes_total += int(len(eyes))

                    if len(eyes) >= 1 or (fw >= int(min_dim * 0.22) and fh >= int(min_dim * 0.22)):
                        accepted = True

                meta: dict[str, Any] = {
                    "backend": "opencv_haar",
                    "faces": int(len(faces)),
                    "eyes": int(eyes_total),
                    "w": int(w),
                    "h": int(h),
                    "error": None,
                }
                if insight_err:
                    meta["insight_error"] = insight_err
                return (bool(accepted), meta)

            return await loop.run_in_executor(None, sync_detect)
        except Exception as e:
            logger.exception("local_cv_detection_failed")
            return (False, {"backend": "opencv_haar", "error": str(e)})

    async def detect_human(self, photo_bytes: bytes) -> bool:
        """
        Fast local human face detection.
        Returns True if a face is detected.
        """
        ok, _meta = await self.detect_human_with_meta(photo_bytes)
        return ok

    async def contains_human(self, photo_bytes: bytes) -> bool:
        """Alias for detect_human"""
        return await self.detect_human(photo_bytes)
