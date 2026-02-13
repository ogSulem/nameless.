from __future__ import annotations

import logging
import asyncio
import threading
import time
import numpy as np
import cv2
from typing import Any

logger = logging.getLogger(__name__)

class AIService:
    _insight_model: Any | None = None
    _insight_init_error: str | None = None
    _insight_init_lock: asyncio.Lock | None = None
    _insight_detect_lock = threading.Lock()
    _insight_init_started: bool = False

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

        self._mp_face_detector: Any | None = None
        self._mp_face_detector_error: str | None = None
        self._mp_lock = threading.Lock()

        self._vision_max_side = 640
        self._vision_min_conf = 0.7

    def configure_from_settings(self, settings: Any) -> None:
        try:
            v = float(getattr(settings, "vision_min_conf", 0.7) or 0.7)
            if 0.0 < v < 1.0:
                self._vision_min_conf = v
        except Exception:
            pass

        try:
            m = int(getattr(settings, "vision_max_side", 640) or 640)
            if m >= 128:
                self._vision_max_side = m
        except Exception:
            pass

    def _resize_max_side(self, bgr: np.ndarray) -> np.ndarray:
        max_side = int(self._vision_max_side or 0)
        if max_side <= 0:
            return bgr
        h, w = bgr.shape[:2]
        m = max(h, w)
        if m <= max_side:
            return bgr
        scale = float(max_side) / float(m)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        return cv2.resize(bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)

    async def _get_insight_model(self) -> Any | None:
        """Lazy-init InsightFace model once per process. Returns None if unavailable."""
        if AIService._insight_model is not None:
            return AIService._insight_model
        if AIService._insight_init_error is not None:
            return None

        # Never block the caller: start init in background and return None until ready.
        await self._ensure_insight_init_started()
        return AIService._insight_model

    async def _ensure_insight_init_started(self) -> None:
        if AIService._insight_init_started or AIService._insight_model is not None or AIService._insight_init_error is not None:
            return

        AIService._insight_init_started = True
        try:
            asyncio.create_task(self._init_insight_model_background())
        except Exception:
            # If we can't schedule a task, allow fallback permanently.
            AIService._insight_init_error = "schedule_failed"

    async def _init_insight_model_background(self) -> None:
        if AIService._insight_model is not None or AIService._insight_init_error is not None:
            return

        if AIService._insight_init_lock is None:
            AIService._insight_init_lock = asyncio.Lock()

        async with AIService._insight_init_lock:
            if AIService._insight_model is not None:
                return AIService._insight_model
            if AIService._insight_init_error is not None:
                return

            try:
                def sync_init() -> Any:
                    import insightface  # type: ignore

                    model = insightface.app.FaceAnalysis(name="buffalo_l")
                    # ctx_id=-1 forces CPU.
                    model.prepare(ctx_id=-1, providers=["CPUExecutionProvider"])
                    return model

                loop = asyncio.get_event_loop()
                model = await loop.run_in_executor(None, sync_init)
                AIService._insight_model = model
                logger.info("insightface_ready")
            except Exception as e:
                AIService._insight_init_error = str(e)
                logger.exception("insightface_init_failed")
                return

    async def detect_human_with_meta(self, photo_bytes: bytes) -> tuple[bool, dict[str, Any]]:
        """Like detect_human(), but also returns debugging metadata for admin channel."""
        try:
            loop = asyncio.get_event_loop()

            def sync_detect() -> tuple[bool, dict[str, Any]]:
                t0 = time.perf_counter()
                nparr = np.frombuffer(photo_bytes, np.uint8)
                image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                if image is None:
                    return (False, {"backend": "decode", "error": "decode_failed", "duration_ms": int((time.perf_counter() - t0) * 1000)})

                h, w = image.shape[:2]

                # Resize for speed/consistency with bench (keeps enough detail for "face vs no face")
                image_det = self._resize_max_side(image)

                insight_err: str | None = None

                # 1) MediaPipe backend (fast & accurate; preferred if available)
                try:
                    import mediapipe as mp  # type: ignore

                    if hasattr(mp, "solutions") and hasattr(mp.solutions, "face_detection"):
                        mp_face_detection = mp.solutions.face_detection
                        rgb_image = cv2.cvtColor(image_det, cv2.COLOR_BGR2RGB)

                        with self._mp_lock:
                            if self._mp_face_detector is None and self._mp_face_detector_error is None:
                                try:
                                    self._mp_face_detector = mp_face_detection.FaceDetection(
                                        model_selection=0,
                                        min_detection_confidence=float(self._vision_min_conf),
                                    )
                                except Exception as e:
                                    self._mp_face_detector_error = str(e)

                            detector = self._mp_face_detector

                        if detector is not None:
                            # MediaPipe graphs are not guaranteed to be thread-safe under concurrent calls.
                            with self._mp_lock:
                                results = detector.process(rgb_image)
                        else:
                            results = None

                        detections = getattr(results, "detections", None) if results is not None else None
                        score_ok = False
                        cnt = 0
                        if detections:
                            for det in detections:
                                cnt += 1
                                try:
                                    if det.score and det.score[0] >= float(self._vision_min_conf):
                                        score_ok = True
                                except Exception:
                                    pass

                        # IMPORTANT: If MediaPipe is available and ran, we trust its verdict.
                        # Do NOT fall back to Haar on "no detections"; that would change behavior and increase FP/FN.
                        return (
                            bool(score_ok),
                            {
                                "backend": "mediapipe_face_detection",
                                "faces": int(cnt),
                                "min_conf": float(self._vision_min_conf),
                                "model_selection": 0,
                                "w": int(w),
                                "h": int(h),
                                "error": None,
                                "duration_ms": int((time.perf_counter() - t0) * 1000),
                            },
                        )
                except Exception as e:
                    # Ignore: environment may not have mediapipe or mp.solutions
                    insight_err = str(e)

                # 2) InsightFace fallback (accurate, but may be unavailable and may download models on first use)
                # NOTE: Do not start InsightFace initialization here.
                # This function runs in a worker thread and must not touch the event loop.
                # Also, triggering downloads during message handling is undesirable for latency.
                model = AIService._insight_model

                if model is not None:
                    try:
                        with AIService._insight_detect_lock:
                            faces = model.get(image_det)
                        return (
                            len(faces) > 0,
                            {
                                "backend": "insightface_buffalo_l",
                                "faces": int(len(faces)),
                                "w": int(w),
                                "h": int(h),
                                "error": None,
                                "duration_ms": int((time.perf_counter() - t0) * 1000),
                            },
                        )
                    except Exception as e:
                        insight_err = str(e)

                # 3) Strict OpenCV fallback
                gray = cv2.cvtColor(image_det, cv2.COLOR_BGR2GRAY)
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
                    "duration_ms": int((time.perf_counter() - t0) * 1000),
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
