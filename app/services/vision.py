from __future__ import annotations

import logging
import aiohttp
import base64
from typing import Any

logger = logging.getLogger(__name__)

class VisionService:
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"

    async def contains_human(self, photo_bytes: bytes) -> bool:
        if not self._api_key:
            return False

        try:
            encoded_image = base64.b64encode(photo_bytes).decode('utf-8')
            
            payload = {
                "contents": [{
                    "parts": [
                        {"text": "Is there a human face or person clearly visible in this image? Answer only 'YES' or 'NO'."},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": encoded_image
                            }
                        }
                    ]
                }]
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(self._url, json=payload) as response:
                    if response.status == 200:
                        res_json = await response.json()
                        text = res_json['candidates'][0]['content']['parts'][0]['text'].strip().upper()
                        return "YES" in text
                    else:
                        text = await response.text()
                        logger.error("gemini_vision_failed status=%s body=%s", response.status, text)
                        return False
        except Exception:
            logger.exception("gemini_vision_exception")
            return False
