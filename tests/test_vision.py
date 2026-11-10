"""The face gate must be cheap, bounded and never able to break a dialog."""

from __future__ import annotations

import asyncio

import pytest

from app.services.vision import FaceDetector

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"0" * 2000


class FakeBot:
    def __init__(self, payload: bytes = JPEG_BYTES, *, fail: bool = False, delay: float = 0.0) -> None:
        self.payload = payload
        self.fail = fail
        self.delay = delay
        self.downloads = 0

    async def download(self, file_id: str, destination=None):
        self.downloads += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("telegram said no")
        destination.write(self.payload)
        return destination


def _detector(settings, **overrides) -> FaceDetector:
    # The shared fixture keeps vision off (tests must not depend on OpenCV);
    # these tests opt in explicitly.
    tuned = settings.model_copy(update={"vision_enabled": True, **overrides})
    return FaceDetector(tuned)


@pytest.mark.asyncio
async def test_empty_payload_is_skipped_without_touching_opencv(settings):
    verdict = await _detector(settings).has_face(b"")
    assert verdict.skipped is True
    assert verdict.has_face is False


@pytest.mark.asyncio
async def test_detection_can_be_switched_off(settings):
    verdict = await _detector(settings, vision_enabled=False).has_face(JPEG_BYTES)
    assert verdict.skipped is True
    assert verdict.backend == "disabled"


@pytest.mark.asyncio
async def test_garbage_image_is_a_miss_not_an_exception(settings):
    detector = _detector(settings)
    if not detector.available:  # no OpenCV in this environment
        pytest.skip("opencv not installed")
    verdict = await detector.has_face(b"not an image at all")
    assert verdict.has_face is False
    assert verdict.backend == "decode_failed"


@pytest.mark.asyncio
async def test_oversized_payload_is_rejected_before_decoding(settings):
    detector = _detector(settings)
    if not detector.available:
        pytest.skip("opencv not installed")
    verdict = await detector.has_face(b"\xff" * (FaceDetector.MAX_BYTES + 1))
    assert verdict.skipped is True
    assert verdict.backend == "too_big"


@pytest.mark.asyncio
async def test_failed_download_is_skipped(settings):
    bot = FakeBot(fail=True)
    verdict = await _detector(settings).check_telegram_photo(bot, "file-1")
    assert verdict.skipped is True
    assert verdict.backend == "download_failed"


@pytest.mark.asyncio
async def test_download_inside_a_slot_keeps_memory_bounded(settings):
    """Downloads must not pile up: only `vision_concurrency` photos in flight."""
    detector = _detector(settings, vision_concurrency=2, vision_timeout_s=30.0)
    if not detector.available:
        pytest.skip("opencv not installed")

    running = 0
    peak = 0

    async def slow_detect(data: bytes):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        from app.services.vision import FaceVerdict

        return FaceVerdict(True, 1, "fake", 1)

    detector._detect = slow_detect  # type: ignore[assignment]

    bots = [FakeBot(delay=0.01) for _ in range(6)]
    await asyncio.gather(*[detector.check_telegram_photo(b, f"f{i}") for i, b in enumerate(bots)])

    assert peak <= 2, f"more than vision_concurrency photos were held in memory at once (peak={peak})"
    assert all(b.downloads == 1 for b in bots)


@pytest.mark.asyncio
async def test_background_photo_check_never_raises(bot, monkeypatch, settings):
    """A broken detector must leave the dialog untouched."""
    from app.handlers import dialog as dialog_handler

    class Broken:
        available = True

        async def check_telegram_photo(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(dialog_handler.FaceDetector, "shared", classmethod(lambda cls, s: Broken()))

    class Info:
        dialog_id = 1
        partner_telegram_id = 2

    class RedisStub:
        async def get(self, *_a, **_k):
            return None

        async def set(self, *_a, **_k):
            return True

    await dialog_handler._photo_pipeline(bot, RedisStub(), settings, Info(), "file", 3)
