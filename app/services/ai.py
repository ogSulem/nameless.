from __future__ import annotations

import logging
import io
import asyncio
import numpy as np
import cv2
import mediapipe as mp
from typing import Any

logger = logging.getLogger(__name__)

class AIService:
    def __init__(self, api_key: str):
        self._api_key = api_key
        # MediaPipe for local backup
        self._mp_face_detection = mp.solutions.face_detection
        self._face_detection = self._mp_face_detection.FaceDetection(
            model_selection=0,
            min_detection_confidence=0.5
        )

    async def detect_human(self, photo_bytes: bytes) -> bool:
        """
        Dual-mode human detection: 
        1. Try Hugging Face Router (High quality)
        2. Fallback to local MediaPipe (Always works)
        """
        # Try HF Router first (using the NEW endpoint to avoid 410 error)
        try:
            import aiohttp
            # Hugging Face says: Use router.huggingface.co instead of api-inference.huggingface.co
            url = "https://router.huggingface.co/hf-inference/models/facebook/detr-resnet-50"
            headers = {"Authorization": f"Bearer {self._api_key}"}
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=photo_bytes, timeout=5) as response:
                    if response.status == 200:
                        results = await response.json()
                        for item in results:
                            if item.get('label') == 'person' and item.get('score', 0) > 0.5:
                                logger.info("Human detected via HF Router!")
                                return True
                    else:
                        logger.warning("HF Router failed (status %s), falling back to MediaPipe", response.status)
        except Exception as e:
            logger.warning("HF Router error: %s, falling back to MediaPipe", e)

        # Fallback to Local MediaPipe
        try:
            def sync_detect():
                nparr = np.frombuffer(photo_bytes, np.uint8)
                image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                if image is None: return False
                rgb_image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                results = self._face_detection.process(rgb_image)
                return bool(results.detections)

            loop = asyncio.get_event_loop()
            is_human = await loop.run_in_executor(None, sync_detect)
            if is_human:
                logger.info("Human detected via local MediaPipe fallback!")
            return is_human
        except Exception:
            logger.exception("local_fallback_failed")
            return False

    async def contains_human(self, photo_bytes: bytes) -> bool:
        """Alias for detect_human"""
        return await self.detect_human(photo_bytes)
