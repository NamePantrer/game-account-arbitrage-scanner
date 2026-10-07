from __future__ import annotations

import json
import logging
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from src.config import env
from src.models import Listing, Platform
from src.parsers.base import BaseParser

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://playerok.com/graphql"

PERSISTED = {
  "games": "5de9b3240c148579c82e2310a30b4aad5462884fd1abf93dd3c43d1f5ef14d85",
  "items": "bacca5d020eef37b4ff7a2253ad33ecd8b7e144b9ef854c20051f42ebcd04d82",
}

# Полный query viewer (PlayerOK ломается на урезанных запросах)
VIEWER_QUERY = """query viewer {
  viewer {
    ...Viewer
    __typename
  }
}

fragment Viewer on User {
  id
  username
  email
  role
  hasFrozenBalance
  supportChatId
  systemChatId
  unreadChatsCounter
  isBlocked
  isBlockedFor
  isFundsProtectionActive
  createdAt
  lastItemCreatedAt
  hasConfirmedPhoneNumber
  canPublishItems
  chosenVerifiedCard {
    ...MinimalUserBankCard
    __typename
  }
  balance {
    value
    __typename
  }
  profile {
    id
    avatarURL
    testimonialCounter
    __typename
  }
  __typename
}

fragment MinimalUserBankCard on UserBankCard {
  id
  cardFirstSix
  cardLastFour
  cardType
  isChosen
  __typename
}"""


def _parse_cookie_header(raw: str) -> dict[str, str]:
  out: dict[str, str] = {}
  for part in raw.split(";"):
    part = part.strip()
    if not part or "=" not in part:
      continue
    k, v = part.split("=", 1)
    out[k.strip()] = v.strip()
  return out


class PlayerOKParser(BaseParser):
  """
  PlayerOK через cookie `token` + GraphQL (persisted queries).
  Официального API нет. Транспорт: curl_cffi (chrome impersonate).
  """

  platform_name = "playerok"
  BASE_URL = "https://playerok.com"

  def __init__(
    self,
    token: Optional[str] = None,
    cookies: Optional[str] = None,
    ddg5: Optional[str] = None,
    user_agent: Optional[str] = None,
  ) -> None:
    self.token = (token if token is not None else env.playerok_token or "").strip()
    self.ddg5 = (ddg5 if ddg5 is not None else env.playerok_ddg5 or "").strip()
    raw_cookies = (cookies if cookies is not None else env.playerok_cookies or "").strip()
    self.user_agent = (user_agent or env.playerok_user_agent or "").strip() or (
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
    self.cookies: dict[str, str] = {}
    if raw_cookies:
      self.cookies.update(_parse_cookie_header(raw_cookies))
    if self.token:
      self.cookies["token"] = self.token
    if self.ddg5:
      if "=" in self.ddg5 and self.ddg5.startswith("__ddg"):
        k, v = self.ddg5.split("=", 1)
        self.cookies[k.strip()] = v.strip()
      else:
        self.cookies["__ddg5_"] = self.ddg5

    self._http = None
    self._mode = "none"
    self._init_http()

  def _init_http(self) -> None:
    try:
      from curl_cffi import requests as cr

      self._http = cr.Session(impersonate="chrome", timeout=30)
      self._mode = "curl_cffi"
    except Exception:
      import requests

      self._http = requests.Session()
      self._mode = "requests"
    self._http.headers.update({"User-Agent": self.user_agent})

  @property
  def is_authenticated(self) -> bool:
    return bool(self.cookies.get("token") or self.token)

  def warm_up(self) -> None:
    assert self._http is not None
    try:
      self._http.get(self.BASE_URL, timeout=20)
      jar = getattr(self._http.cookies, "get_dict", None)
      if callable(jar):
        self.cookies.update({k: v for k, v in jar().items() if v})
      else:
        for c in self._http.cookies:
          name = getattr(c, "name", None)
          value = getattr(c, "value", None)
          if name and value:
            self.cookies[str(name)] = str(value)
      if self.token:
        self.cookies["token"] = self.token
    except Exception as exc:
      logger.warning("PlayerOK warm-up failed: %s", exc)

  def _headers(self, operation: str) -> dict[str, str]:
    return {
      "accept": "*/*",
      "accept-language": "ru-RU,ru;q=0.9",
      "apollo-require-preflight": "true",
      "apollographql-client-name": "web",
      "content-type": "application/json",
      "cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
      "origin": self.BASE_URL,
      "referer": f"{self.BASE_URL}/",
      "user-agent": self.user_agent,
      "x-gql-op": operation,
      "x-gql-path": "/",
      "x-apollo-operation-name": operation,
    }

  def _gql_post(self, operation: str, query: str, variables: dict[str, Any] | None = None) -> dict:
    assert self._http is not None
    payload = {
      "operationName": operation,
      "variables": variables or {},
      "query": query,
    }
    resp = self._http.post(
      GRAPHQL_URL, json=payload, headers=self._headers(operation), timeout=30
    )
    data = resp.json()
    if data.get("errors"):
      msg = "; ".join(str(e.get("message") or e) for e in data["errors"])
      raise RuntimeError(f"PlayerOK GraphQL: {msg}")
    return data.get("data") or {}

  def _gql_persisted(self, operation: str, variables: dict[str, Any]) -> dict:
    assert self._http is not None
    if operation not in PERSISTED:
      raise RuntimeError(f"No persisted hash for {operation}")
    params = {
      "operationName": operation,
      "variables": json.dumps(variables, ensure_ascii=False),
      "extensions": json.dumps(
        {"persistedQuery": {"version": 1, "sha256Hash": PERSISTED[operation]}},
        ensure_ascii=False,
      ),
    }
    resp = self._http.get(
      GRAPHQL_URL, params=params, headers=self._headers(operation), timeout=30
    )
    data = resp.json()
    if data.get("errors"):
      msg = "; ".join(str(e.get("message") or e) for e in data["errors"])
      raise RuntimeError(f"PlayerOK GraphQL: {msg}")
    return data.get("data") or {}

  def check_auth(self) -> dict:
    if not self.is_authenticated:
      return {
        "ok": False,
        "auth": False,
        "error": "PLAYEROK_TOKEN (cookie token) не задан",
        "transport": self._mode,
      }
    self.warm_up()
    try:
      data = self._gql_post("viewer", VIEWER_QUERY, {})
      viewer = data.get("viewer") or {}
      bal = (viewer.get("balance") or {}).get("value")
      return {
        "ok": True,
        "auth": bool(viewer.get("id")),
        "username": viewer.get("username"),
        "user_id": viewer.get("id"),
        "balance": bal,
        "transport": self._mode,
      }
    except Exception as exc:
      return {
        "ok": False,
        "auth": False,
        "error": str(exc),
        "transport": self._mode,
        "hint": "Обнови cookie token (и при BotCheck — __ddg5_)",
      }

  def resolve_game_id(self, game_slug_or_name: str) -> Optional[str]:
    self.warm_up()
    raw = (game_slug_or_name or "").strip()
    if not raw:
      return None
    slug_l = raw.lower().replace(" ", "-")
    queries = [raw, raw.replace("-", " "), slug_l]
    # уникальные, сохраняя порядок
    seen: set[str] = set()
    skip_slugs = {"drugie-igry", "drugie-prilozheniya"}

    for q in queries:
      if not q or q in seen:
        continue
      seen.add(q)
      data = self._gql_persisted(
        "games",
        {
          "pagination": {"first": 10, "after": None},
          "filter": {"name": q, "type": None},
        },
      )
      edges = ((data.get("games") or {}).get("edges")) or []
      nodes = [e.get("node") or {} for e in edges]
      for node in nodes:
        if str(node.get("slug") or "").lower() == slug_l:
          return node.get("id")
      for node in nodes:
        s = str(node.get("slug") or "").lower()
        if s and s not in skip_slugs:
          return node.get("id")
    return None

  def fetch_listings(
    self,
    game: str,
    category: str,
    category_url: Optional[str] = None,
    game_id: Optional[str] = None,
    limit: int = 50,
  ) -> list[Listing]:
    if not self.is_authenticated:
      logger.warning("PlayerOK: задай PLAYEROK_TOKEN в .env")
      return []

    try:
      gid = game_id
      slug = game
      if category_url:
        parts = [p for p in urlparse(category_url).path.split("/") if p]
        if "games" in parts:
          i = parts.index("games")
          if i + 1 < len(parts):
            slug = parts[i + 1]
      if not gid:
        gid = self.resolve_game_id(slug)
      if not gid:
        logger.error("PlayerOK: не найден game_id для %s", game)
        return []

      # API отдаёт до 24 за страницу
      collected: list[dict] = []
      after: Optional[str] = None
      while len(collected) < limit:
        page = min(24, limit - len(collected))
        data = self._gql_persisted(
          "items",
          {
            "pagination": {"first": page, "after": after},
            "filter": {"gameId": gid, "status": ["APPROVED"]},
          },
        )
        conn = data.get("items") or {}
        edges = conn.get("edges") or []
        if not edges:
          break
        collected.extend(edges)
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
          break
        after = page_info.get("endCursor")
        if not after:
          break
    except Exception as exc:
      logger.error("PlayerOK fetch failed: %s", exc)
      return []

    listings: list[Listing] = []
    for edge in collected[:limit]:
      node = edge.get("node") or {}
      item_id = str(node.get("id") or "")
      title = node.get("name") or f"Item {item_id}"
      try:
        price = float(node.get("price") or 0)
      except (TypeError, ValueError):
        continue
      if price <= 0 or not item_id:
        continue
      item_slug = node.get("slug") or item_id
      user = node.get("user") or node.get("seller") or {}
      seller_name = user.get("username")
      listings.append(
        Listing(
          platform=Platform.PLAYEROK,
          external_id=item_id,
          title=str(title)[:200],
          price_rub=price,
          url=urljoin(self.BASE_URL, f"/products/{item_slug}"),
          game=game,
          category=category,
          seller_name=seller_name,
          seller_url=(
            urljoin(self.BASE_URL, f"/profile/{seller_name}") if seller_name else None
          ),
        )
      )

    logger.info("PlayerOK: fetched %d listings for game=%s", len(listings), game)
    return listings
