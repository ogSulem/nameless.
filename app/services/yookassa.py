from __future__ import annotations

import base64
import logging
import uuid
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

API_URL = "https://api.yookassa.ru/v3"
TIMEOUT = aiohttp.ClientTimeout(total=15)


class YookassaError(Exception):
    pass


class YookassaService:
    """Thin ЮKassa client.

    Differences from the previous version, all required for real money in RU:

    * request timeouts (a hung gateway used to freeze the handler forever);
    * ``Idempotence-Key`` reused across retries of the same logical payment;
    * a 54-ФЗ ``receipt`` is always attached (fiscalisation is mandatory, the
      previous code sent none, which is a legal violation, not a bug);
    * refunds.
    """

    def __init__(self, shop_id: str, secret_key: str) -> None:
        self._shop_id = shop_id
        self._secret_key = secret_key
        self._auth = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()

    @property
    def configured(self) -> bool:
        return bool(self._shop_id and self._secret_key)

    def _headers(self, idempotence_key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Basic {self._auth}", "Content-Type": "application/json"}
        if idempotence_key:
            headers["Idempotence-Key"] = idempotence_key
        return headers

    async def create_payment(
        self,
        *,
        amount_rub: int,
        description: str,
        return_url: str,
        customer_email: str,
        metadata: dict[str, Any],
        idempotence_key: str | None = None,
    ) -> tuple[str, str]:
        """Returns (confirmation_url, payment_id). Raises YookassaError."""
        payload = {
            "amount": {"value": f"{int(amount_rub)}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": return_url},
            "capture": True,
            "description": description[:128],
            "metadata": metadata,
            "receipt": {
                "customer": {"email": customer_email},
                "items": [
                    {
                        "description": description[:128],
                        "quantity": "1.00",
                        "amount": {"value": f"{int(amount_rub)}.00", "currency": "RUB"},
                        "vat_code": 1,
                        "payment_mode": "full_payment",
                        "payment_subject": "service",
                    }
                ],
            },
        }

        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                async with session.post(
                    f"{API_URL}/payments",
                    json=payload,
                    headers=self._headers(idempotence_key or str(uuid.uuid4())),
                ) as resp:
                    body = await resp.text()
                    if resp.status not in (200, 201):
                        logger.error("yookassa_create_failed status=%s body=%s", resp.status, body[:500])
                        raise YookassaError(f"create_failed:{resp.status}")
                    data = await resp.json()
        except aiohttp.ClientError as exc:
            logger.exception("yookassa_network_error")
            raise YookassaError("network") from exc

        confirmation = data.get("confirmation") or {}
        url = confirmation.get("confirmation_url")
        payment_id = data.get("id")
        if not url or not payment_id:
            raise YookassaError("no_confirmation_url")
        return str(url), str(payment_id)

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                async with session.get(f"{API_URL}/payments/{payment_id}", headers=self._headers()) as resp:
                    if resp.status != 200:
                        logger.warning("yookassa_get_failed status=%s payment_id=%s", resp.status, payment_id)
                        raise YookassaError(f"get_failed:{resp.status}")
                    return await resp.json()
        except aiohttp.ClientError as exc:
            raise YookassaError("network") from exc

    async def refund(self, payment_id: str, amount_rub: int) -> dict[str, Any]:
        payload = {
            "payment_id": payment_id,
            "amount": {"value": f"{int(amount_rub)}.00", "currency": "RUB"},
        }
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                async with session.post(
                    f"{API_URL}/refunds", json=payload, headers=self._headers(str(uuid.uuid4()))
                ) as resp:
                    body = await resp.text()
                    if resp.status not in (200, 201):
                        logger.error("yookassa_refund_failed status=%s body=%s", resp.status, body[:500])
                        raise YookassaError(f"refund_failed:{resp.status}")
                    return await resp.json()
        except aiohttp.ClientError as exc:
            raise YookassaError("network") from exc
