"""Покупка лотов на Lolz Market через официальный API."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests

from src.config import env

logger = logging.getLogger(__name__)

API_BASE = "https://prod-api.lzt.market"
MAX_RETRIES = 5


class LolzBuyer:
    def __init__(self, api_token: Optional[str] = None, balance_id: Optional[int] = None) -> None:
        self.api_token = api_token or env.lzt_api_token
        self.balance_id = balance_id or env.lzt_balance_id
        self.session = requests.Session()
        if self.api_token:
            self.session.headers.update({
                "Authorization": f"Bearer {self.api_token}",
                "Accept": "application/json",
            })

    @property
    def configured(self) -> bool:
        return bool(self.api_token)

    def get_balance_rub(self) -> float:
        if not self.configured:
            return 0.0
        try:
            resp = self.session.get(f"{API_BASE}/me", timeout=20)
            resp.raise_for_status()
            data = resp.json()
            user = data.get("user", data)
            balance = user.get("balance", user.get("market_balance", 0))
            return float(balance)
        except requests.RequestException as exc:
            logger.error("Lolz balance error: %s", exc)
            return 0.0

    def fast_buy(self, item_id: str, price_rub: float) -> dict[str, Any]:
        """Fast-buy с повтором при retry_request."""
        if not self.configured:
            return {"ok": False, "error": "LZT_API_TOKEN не задан"}

        payload: dict[str, Any] = {"price": price_rub}
        if self.balance_id:
            payload["balance_id"] = self.balance_id

        url = f"{API_BASE}/{item_id}/fast-buy"
        last_error = "unknown"

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.post(url, json=payload, timeout=30)
                body = resp.json() if resp.content else {}
            except requests.RequestException as exc:
                last_error = str(exc)
                time.sleep(0.5)
                continue

            if resp.status_code == 200 and body.get("status") == "ok":
                return {"ok": True, "item_id": item_id, "price": price_rub, "response": body}

            err_text = str(body.get("errors", body.get("error", body)))
            last_error = err_text
            if "retry_request" in err_text.lower() or body.get("status") == "retry_request":
                time.sleep(0.4 * attempt)
                continue
            break

        return {"ok": False, "item_id": item_id, "error": last_error}
