from __future__ import annotations

import logging
import io
import asyncio
import numpy as np
import cv2
from typing import Any

logger = logging.getLogger(__name__)

class AIService:
    def __init__(self, api_key: str | None = None):
        # Local, fast, and reliable face detection.
        # Primary: OpenCV Haar cascade (always available offline).
        # Optional: MediaPipe solutions API if present in the environment.
        self._face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )

    async def detect_human(self, photo_bytes: bytes) -> bool:
        """
        Fast local human face detection.
        Returns True if a face is detected.
        """
        try:
            def sync_detect():
                # Convert bytes to numpy array
                nparr = np.frombuffer(photo_bytes, np.uint8)
                image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                
                if image is None:
                    logger.error("Failed to decode image")
                    return False

                # 1) OpenCV Haar cascade (very fast)
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                faces = self._face_cascade.detectMultiScale(
                    gray,
                    scaleFactor=1.1,
                    minNeighbors=5,
                    minSize=(30, 30),
                )
                if len(faces) > 0:
                    return True

                # 2) Optional MediaPipe fallback if solutions API exists
                try:
                    import mediapipe as mp

                    if not hasattr(mp, "solutions"):
                        return False

                    mp_face_detection = mp.solutions.face_detection
                    rgb_image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                    with mp_face_detection.FaceDetection(
                        model_selection=1,
                        min_detection_confidence=0.6,
                    ) as detector:
                        results = detector.process(rgb_image)
                        return bool(results.detections)
                except Exception:
                    return False

            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, sync_detect)
            
        except Exception:
            logger.exception("local_cv_detection_failed")
            return False

    async def contains_human(self, photo_bytes: bytes) -> bool:
        """Alias for detect_human"""
        return await self.detect_human(photo_bytes)
