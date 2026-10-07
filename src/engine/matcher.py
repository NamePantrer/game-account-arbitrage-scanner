from __future__ import annotations

import re
from typing import Iterable

from rapidfuzz import fuzz

from src.models import Listing


def normalize_title(text: str) -> str:
  text = text.lower().strip()
  text = re.sub(r"[^\w\s|+\-]", " ", text, flags=re.UNICODE)
  text = re.sub(r"\s+", " ", text)
  return text


def extract_features(title: str, description: str = "") -> dict[str, str | int | None]:
  """Извлекает ключевые признаки для сопоставления аккаунтов."""
  blob = normalize_title(f"{title} {description}")
  features: dict[str, str | int | None] = {}

  lvl = re.search(r"(?:lvl|level|ур|уровень|ar)\s*[:=]?\s*(\d+)", blob)
  if lvl:
    features["level"] = int(lvl.group(1))

  count = re.search(r"(\d+)\s*(?:brawler|бравлер|hero|гер|перс)", blob)
  if count:
    features["count"] = int(count.group(1))

  for region in ("eu", "us", "ru", "asia", "европа", "россия", "америка"):
    if region in blob:
      features["region"] = region
      break

  keywords = []
  for word in ("legendary", "mythic", "leg", "lega", "лег", "миф", "prime"):
    if word in blob:
      keywords.append(word)
  if keywords:
    features["keywords"] = ",".join(sorted(set(keywords)))

  return features


def match_score(a: Listing, b: Listing) -> float:
  """
  Похожесть лотов (0-100).
  Обязательно: одна игра + один сегмент товара (акк≠кристаллы).
  """
  if a.game != b.game or a.category != b.category:
    return 0.0
  seg_a = (a.segment or "other").lower()
  seg_b = (b.segment or "other").lower()
  # разные номинации (канал vs premium vs аккаунт) — не пара
  if seg_a != seg_b:
    return 0.0
  # «непонятный» сегмент не скрещиваем между площадками
  if seg_a in {"other", "tg_other", "service"}:
    return 0.0

  title_score = fuzz.token_sort_ratio(
    normalize_title(a.title),
    normalize_title(b.title),
  )

  score = 55.0 + 0.45 * float(title_score)

  fa = extract_features(a.title, a.description)
  fb = extract_features(b.title, b.description)

  for key in ("level", "count", "region"):
    if key in fa and key in fb:
      if fa[key] == fb[key]:
        score += 8
      else:
        score -= 12

  return max(0.0, min(100.0, score))


def pick_sell_reference(
  buy: Listing,
  candidates: list[tuple[Listing, float]],
  target_price: float,
) -> tuple[Listing, float] | None:
  """
  Референс для ссылки «похожий лот»: ближе к целевой цене продажи,
  а не самый дорогой (иначе UI ведёт на бафнутый лот).
  """
  if not candidates:
    return None
  # сначала достаточно похожие
  strong = [(s, sc) for s, sc in candidates if sc >= 60] or candidates
  target = target_price if target_price > 0 else buy.price_rub * 1.2

  def key(item: tuple[Listing, float]) -> tuple:
    sell, score = item
    # 1) близость цены к целевой  2) выше match_score  3) не ультра-дорогой выброс
    return (abs(sell.price_rub - target) / max(target, 1.0), -score, sell.price_rub)

  return min(strong, key=key)


def find_matches(
  buy_listings: Iterable[Listing],
  sell_listings: Iterable[Listing],
  min_score: float = 55.0,
  max_pairs_per_buy: int = 3,
) -> list[tuple[Listing, Listing, float]]:
  """
  Пары: одна игра + один сегмент, разные площадки.
  Sell-лот — только референс цены/ссылки (ближе к рынку, не max price).
  """
  pairs: list[tuple[Listing, Listing, float]] = []
  sells = list(sell_listings)

  by_key: dict[tuple[str, str], list[Listing]] = {}
  for s in sells:
    by_key.setdefault((s.game, (s.segment or "other").lower()), []).append(s)

  for buy in buy_listings:
    if buy.price_rub < 5:
      continue
    seg = (buy.segment or "other").lower()
    if seg in {"other", "tg_other", "service"}:
      continue
    candidates = by_key.get((buy.game, seg), [])
    scored: list[tuple[Listing, float]] = []
    for sell in candidates:
      if buy.platform == sell.platform:
        continue
      if sell.price_rub < 5:
        continue
      score = match_score(buy, sell)
      if score < min_score:
        continue
      scored.append((sell, score))

    if not scored:
      continue

    # целевая цена ориентира — около нижней половины сегмента продаж
    sell_prices = sorted(s.price_rub for s, _ in scored)
    mid = sell_prices[len(sell_prices) // 2] if sell_prices else buy.price_rub
    p25 = sell_prices[max(0, len(sell_prices) // 4)] if sell_prices else mid
    target = min(mid, p25 * 1.15)

    # берём лучший референс + ещё пару ближайших по цене
    scored_sorted = sorted(
      scored,
      key=lambda it: (abs(it[0].price_rub - target) / max(target, 1.0), -it[1]),
    )
    for sell, score in scored_sorted[:max_pairs_per_buy]:
      pairs.append((buy, sell, score))

  pairs.sort(key=lambda x: (x[2], -(abs(x[1].price_rub - x[0].price_rub))), reverse=True)
  return pairs
