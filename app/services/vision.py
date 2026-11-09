from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config import Settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aiogram import Bot

logger = logging.getLogger(__name__)


def _shared_semaphore(limit: int) -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(limit)
    return _SEM


_SEM: asyncio.Semaphore | None = None


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

    # Telegram photos are compressed and capped, but a stray huge file must not
    # turn into a huge in-memory buffer.
    MAX_BYTES = 12 * 1024 * 1024
    # Below this the long side cannot hold a detectable face anyway.
    MIN_SIDE = 48

    _shared: FaceDetector | None = None

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cv2 = None
        self._cascade = None
        self._profile = None
        self._loaded = False
        self._sem = _shared_semaphore(max(1, int(settings.vision_concurrency)))

    @classmethod
    def shared(cls, settings: Settings) -> FaceDetector:
        """One detector per process.

        Building a new instance per photo re-read the Haar cascade from disk and
        reset the concurrency semaphore on every message, so the limit never
        actually limited anything.
        """
        if cls._shared is None:
            cls._shared = cls(settings)
        return cls._shared

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
            # Sideways photos are still "a photo of a person"; the profile cascade
            # only matches one direction, so it is also run on a mirrored copy.
            profile = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_profileface.xml"  # type: ignore[attr-defined]
            )
            self._cv2 = cv2
            self._cascade = cascade
            self._profile = None if profile.empty() else profile
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
            if max(h, w) < self.MIN_SIDE:
                return 0, "too_small"
            if max(h, w) > max_side:
                scale = max_side / float(max(h, w))
                img = cv2.resize(img, (int(w * scale), int(h * scale)))
            prepared = cv2.equalizeHist(img)
            faces = cascade.detectMultiScale(
                prepared, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
            )
            if len(faces):
                return len(faces), "haar"

            # Frontal pass came back empty: a profile is still a person. Costs
            # ~170 ms of a background thread, and only for photos we would
            # otherwise have to call "no face at all".
            profile = self._profile
            if profile is not None:
                for candidate in (prepared, cv2.flip(prepared, 1)):
                    found = profile.detectMultiScale(
                        candidate, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
                    )
                    if len(found):
                        return len(found), "haar_profile"
            return 0, "haar"
        except Exception:
            logger.warning("vision_detect_failed", exc_info=True)
            return 0, "error"

    async def _detect(self, data: bytes) -> FaceVerdict:
        """Detect without taking the slot — every caller below holds one already.

        Detection is pure CPU: it runs in a worker thread, so the event loop keeps
        serving other users while one photo is being examined.
        """
        started = time.perf_counter()
        if not data or len(data) > self.MAX_BYTES:
            return FaceVerdict(False, 0, "too_big", 0, skipped=True)
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

    async def has_face(self, data: bytes) -> FaceVerdict:
        if not self._load():
            return FaceVerdict(False, 0, "disabled", 0, skipped=True)
        async with self._sem:
            return await self._detect(data)

    async def check_telegram_photo(self, bot: Bot, file_id: str) -> FaceVerdict:
        """Download and analyse one photo under a single concurrency slot.

        The download happens *inside* the slot on purpose: with `vision_concurrency`
        slots only that many photos are ever held in RAM, no matter how many people
        send pictures at once.
        """
        if not self._load():
            return FaceVerdict(False, 0, "disabled", 0, skipped=True)

        async with self._sem:
            buffer = io.BytesIO()
            try:
                await asyncio.wait_for(
                    bot.download(file_id, destination=buffer),
                    timeout=float(self._settings.vision_timeout_s),
                )
            except asyncio.TimeoutError:
                logger.info("photo_download_timeout")
                return FaceVerdict(False, 0, "download_timeout", 0, skipped=True)
            except Exception:
                logger.info("photo_download_failed", exc_info=True)
                return FaceVerdict(False, 0, "download_failed", 0, skipped=True)
            return await self._detect(buffer.getvalue())
