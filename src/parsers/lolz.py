from __future__ import annotations

import logging
from typing import Optional

import requests

from src.config import env, yaml_config
from src.models import Listing, Platform
from src.parsers.base import BaseParser

logger = logging.getLogger(__name__)

API_BASE = "https://prod-api.lzt.market"

# Числовой ID → slug пути API (GET /{categoryName})
CATEGORY_ID_TO_NAME: dict[int, str] = {
  1: "steam",
  3: "ea",
  4: "warface",
  5: "uplay",
  6: "llm",
  7: "socialclub",
  8: "hytale",
  9: "fortnite",
  10: "instagram",
  11: "battlenet",
  12: "epicgames",
  13: "riot",
  14: "world-of-tanks",
  15: "supercell",
  16: "wot-blitz",
  17: "mihoyo",
  18: "escape-from-tarkov",
  19: "vpn",
  20: "tiktok",
  22: "discord",
  24: "telegram",
  28: "minecraft",
  30: "gifts",
  31: "roblox",
}


class LolzParser(BaseParser):
  """Клиент Lolz Market API — https://lzt-market.readme.io"""

  platform_name = "lolz"

  def __init__(self, api_token: Optional[str] = None) -> None:
    self.api_token = api_token or env.lzt_api_token
    self.session = requests.Session()
    self.session.headers.update(
      {
        "Authorization": f"Bearer {self.api_token}",
        "Accept": "application/json",
      }
    )
    self.allowed_origins = set(yaml_config.get("lolz_allowed_origins", []))
    self.blocked_origins = set(yaml_config.get("lolz_blocked_origins", []))
    self._id_to_name: dict[int, str] = dict(CATEGORY_ID_TO_NAME)

  def _resolve_category_name(
    self,
    category_name: Optional[str] = None,
    category_id: Optional[int] = None,
  ) -> Optional[str]:
    if category_name:
      return str(category_name).strip().lstrip("/")
    if not category_id:
      return None
    if category_id in self._id_to_name:
      return self._id_to_name[category_id]
    # Подтянуть карту с API один раз
    try:
      resp = self.session.get(f"{API_BASE}/category", timeout=20)
      resp.raise_for_status()
      for cat in resp.json().get("categories", []):
        cid = cat.get("category_id")
        cname = cat.get("category_name")
        if cid is not None and cname:
          self._id_to_name[int(cid)] = str(cname)
    except requests.RequestException as exc:
      logger.error("Lolz: не удалось загрузить /category: %s", exc)
    return self._id_to_name.get(int(category_id))

  def fetch_listings(
    self,
    game: str,
    category: str,
    category_id: Optional[int] = None,
    category_name: Optional[str] = None,
    limit: int = 50,
  ) -> list[Listing]:
    if not self.api_token:
      logger.warning("Lolz: задай LZT_API_TOKEN в .env")
      return []

    slug = self._resolve_category_name(category_name=category_name, category_id=category_id)
    if not slug:
      logger.warning("Lolz: укажи lolz_category или lolz_category_id для %s", game)
      return []

    params = {
      "page": 1,
      "order_by": "price_to_up",
      "pmin": 50,
    }

    try:
      resp = self.session.get(
        f"{API_BASE}/{slug}",
        params=params,
        timeout=20,
      )
      resp.raise_for_status()
      data = resp.json()
    except requests.RequestException as exc:
      logger.error("Lolz API error: %s", exc)
      return []

    items = data.get("items", data.get("list", []))
    listings: list[Listing] = []

    for item in items[:limit]:
      origin = item.get("item_origin") or item.get("origin")
      if origin in self.blocked_origins:
        continue
      if self.allowed_origins and origin and origin not in self.allowed_origins:
        continue

      item_id = str(item.get("item_id", item.get("id", "")))
      title = item.get("title") or item.get("description") or f"Account #{item_id}"
      price = float(item.get("price", 0))
      if price <= 0:
        continue

      seller = item.get("seller", {}) or {}
      seller_name = seller.get("username") or seller.get("display_username")
      seller_id = seller.get("user_id") or seller.get("id")
      seller_url = f"https://lzt.market/user/{seller_id}/" if seller_id else None
      listings.append(
        Listing(
          platform=Platform.LOLZ,
          external_id=item_id,
          title=title[:200],
          description=(item.get("description") or "")[:500],
          price_rub=price,
          url=f"https://lzt.market/{item_id}/",
          game=game,
          category=category,
          seller_name=seller_name,
          seller_url=seller_url,
          seller_rating=seller.get("rating"),
          seller_reviews=seller.get("reviews_count"),
          origin=origin,
        )
      )

    logger.info("Lolz: fetched %d listings for category %s", len(listings), slug)
    return listings
