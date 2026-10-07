from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from src.config import env
from src.models import Listing, Platform
from src.parsers.base import BaseParser

logger = logging.getLogger(__name__)

HEADERS = {
  "User-Agent": (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
  ),
  "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class FunPayParser(BaseParser):
  """
  FunPay: официального API нет.
  Скан лотов — публичный HTML (/lots/ID/).
  Cookie golden_key нужен для кабинета (баланс, заказы, свои лоты).
  """

  platform_name = "funpay"
  BASE_URL = "https://funpay.com"

  def __init__(
    self,
    session: Optional[requests.Session] = None,
    golden_key: Optional[str] = None,
  ) -> None:
    self.golden_key = (golden_key if golden_key is not None else env.funpay_golden_key or "").strip()
    self.session = session or requests.Session()
    self.session.headers.update(HEADERS)
    if self.golden_key:
      self.session.cookies.set("golden_key", self.golden_key, domain="funpay.com")

  @property
  def is_authenticated(self) -> bool:
    return bool(self.golden_key)

  def check_auth(self) -> dict:
    """Проверка golden_key: залогинен ли аккаунт."""
    if not self.golden_key:
      return {"ok": False, "auth": False, "error": "FUNPAY_GOLDEN_KEY не задан"}
    try:
      resp = self.session.get(f"{self.BASE_URL}/account/", timeout=20, allow_redirects=True)
    except requests.RequestException as exc:
      return {"ok": False, "auth": False, "error": str(exc)}

    url = str(resp.url)
    if "/login" in url:
      return {"ok": False, "auth": False, "error": "golden_key невалиден или протух"}

    soup = BeautifulSoup(resp.text, "lxml")
    name_el = soup.select_one(".user-link-name, .media-user-name, .user-link")
    username = name_el.get_text(strip=True) if name_el else None
    balance = None
    bal_el = soup.select_one(".badge-balance, .user-link-balance, [class*='balance']")
    if bal_el:
      balance = bal_el.get_text(" ", strip=True)
    return {
      "ok": True,
      "auth": True,
      "username": username,
      "balance": balance,
      "url": url,
    }

  def fetch_listings(
    self,
    game: str,
    category: str,
    lot_path: Optional[str] = None,
    limit: int = 50,
  ) -> list[Listing]:
    """
    game/category — для метаданных.
    lot_path — относительный URL раздела, напр. /lots/436/ или полный URL.
    """
    if not lot_path:
      logger.warning("FunPay: укажи funpay_lot_path для %s/%s в config.yaml", game, category)
      return []

    url = lot_path if lot_path.startswith("http") else urljoin(self.BASE_URL, lot_path)
    try:
      resp = self.session.get(url, timeout=20)
      resp.raise_for_status()
    except requests.RequestException as exc:
      logger.error("FunPay fetch failed: %s", exc)
      return []

    soup = BeautifulSoup(resp.text, "lxml")
    listings: list[Listing] = []

    for item in soup.select("a.tc-item")[:limit]:
      offer_id = item.get("href", "").rstrip("/").split("/")[-1]
      title_el = item.select_one(".tc-desc-text")
      price_el = item.select_one(".tc-price")
      seller_el = item.select_one(".media-user-name")

      if not offer_id or not price_el:
        continue

      title = title_el.get_text(strip=True) if title_el else "Без названия"
      if title.lower() in {"цена", "price"}:
        continue
      price = self._parse_price(price_el.get_text(" ", strip=True))
      if price is None or price < 5:
        continue

      rating = None
      reviews = None
      rating_el = item.select_one(".rating-value, .rating")
      if rating_el:
        try:
          rating = float(rating_el.get_text(strip=True).replace(",", "."))
        except ValueError:
          pass

      seller_name = seller_el.get_text(strip=True) if seller_el else None
      seller_url = None
      seller_link = item.select_one("a.avatar-photo, a[href*='/users/']")
      if seller_link and seller_link.get("href"):
        seller_url = urljoin(self.BASE_URL, seller_link.get("href"))

      listings.append(
        Listing(
          platform=Platform.FUNPAY,
          external_id=offer_id,
          title=title,
          price_rub=price,
          url=urljoin(self.BASE_URL, item.get("href", "")),
          game=game,
          category=category,
          seller_name=seller_name,
          seller_url=seller_url,
          seller_rating=rating,
          seller_reviews=reviews,
        )
      )

    logger.info("FunPay: fetched %d listings from %s", len(listings), url)
    return listings

  @staticmethod
  def _parse_price(raw: str) -> Optional[float]:
    cleaned = re.sub(r"[^\d.,]", "", raw.replace(" ", ""))
    cleaned = cleaned.replace(",", ".")
    try:
      return float(cleaned)
    except ValueError:
      return None

  def discover_lot_url(self, game_query: str) -> list[dict]:
    """Поиск ссылок /lots/ на главной по подстроке названия игры."""
    try:
      resp = self.session.get(self.BASE_URL, timeout=20)
      resp.raise_for_status()
    except requests.RequestException as exc:
      logger.error("FunPay discover failed: %s", exc)
      return []

    soup = BeautifulSoup(resp.text, "lxml")
    q = game_query.lower().strip()
    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.select("a[href*='/lots/']"):
      text = a.get_text(" ", strip=True)
      href = a.get("href", "")
      if not text or q not in text.lower():
        continue
      path = href.replace(self.BASE_URL, "")
      if path in seen:
        continue
      seen.add(path)
      out.append({"title": text, "lot_path": path, "url": urljoin(self.BASE_URL, href)})
    return out[:20]
