from __future__ import annotations

import base64
import logging
import uuid
from typing import Any, Dict

import aiohttp

logger = logging.getLogger(__name__)

class YookassaService:
    BASE_URL = "https://api.yookassa.ru/v3"

    def __init__(self, shop_id: str, secret_key: str):
        self._shop_id = shop_id
        self._secret_key = secret_key
        self._auth = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()

    async def create_payment(
        self,
        amount: int,
        description: str,
        return_url: str,
        metadata: Dict[str, Any],
    ) -> tuple[str | None, str | None]:
        url = f"{self.BASE_URL}/payments"
        headers = {
            "Authorization": f"Basic {self._auth}",
            "Idempotence-Key": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        data = {
            "amount": {"value": f"{amount}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": return_url},
            "capture": True,
            "description": description,
            "metadata": metadata
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, headers=headers) as response:
                if response.status == 200:
                    res_json = await response.json()
                    payment_id = res_json.get("id")
                    confirmation = res_json.get("confirmation", {})
                    return confirmation.get("confirmation_url"), payment_id
                else:
                    text = await response.text()
                    logger.error("yookassa_create_failed status=%s body=%s", response.status, text)
                    return None, None

    async def get_payment_status(self, payment_id: str) -> tuple[str | None, bool]:
        url = f"{self.BASE_URL}/payments/{payment_id}"
        headers = {"Authorization": f"Basic {self._auth}"}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    res_json = await response.json()
                    return res_json.get("status"), res_json.get("paid")
                return None, False
