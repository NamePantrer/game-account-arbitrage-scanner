from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable

from src.config import (
    get_demand_per_day,
    get_effective_min_margin,
    get_effective_min_profit,
    refresh_overrides,
)
from src.engine.market import compute_market_stats
from src.engine.pricing import analyze_prices
from src.engine.matcher import find_matches
from src.engine.profit import estimate_arbitrage
from src.engine.web_intel import adjust_demand
from src.models import ArbitrageOpportunity, Listing, Platform

logger = logging.getLogger(__name__)


def _build_market_cache(pool: list[Listing]) -> dict[tuple[str, str, str], tuple]:
    """Кэш analyze_prices + market stats по (game, category, segment)."""
    prices_by_key: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for item in pool:
        seg = (item.segment or "account").lower()
        prices_by_key[(item.game, item.category, seg)].append(item.price_rub)

    cache: dict[tuple[str, str, str], tuple] = {}
    for key, prices in prices_by_key.items():
        analysis = analyze_prices(prices)
        market = compute_market_stats(key[0], key[1], prices)
        cache[key] = (analysis, market, prices)
    return cache


def scan_opportunities(
  listings_by_platform: dict[Platform, list[Listing]],
  min_profit_rub: float | None = None,
  min_margin_percent: float | None = None,
) -> list[ArbitrageOpportunity]:
  """Ищет арбитраж между всеми парами площадок."""
  refresh_overrides()  # подхватываем авто-настройки «мозга»
  min_profit = min_profit_rub if min_profit_rub is not None else get_effective_min_profit()
  min_margin = min_margin_percent if min_margin_percent is not None else get_effective_min_margin()

  opportunities: list[ArbitrageOpportunity] = []
  platforms = list(listings_by_platform.keys())

  for i, buy_platform in enumerate(platforms):
    for sell_platform in platforms[i + 1 :]:
      buy_listings = listings_by_platform.get(buy_platform, [])
      sell_listings = listings_by_platform.get(sell_platform, [])

      # Направление A: buy on platform i, sell on j
      opportunities.extend(
        _evaluate_direction(buy_listings, sell_listings, sell_platform, min_profit, min_margin)
      )
      # Направление B: buy on j, sell on i
      opportunities.extend(
        _evaluate_direction(sell_listings, buy_listings, buy_platform, min_profit, min_margin)
      )

  opportunities.sort(key=lambda o: o.net_profit_rub, reverse=True)
  return opportunities


def _evaluate_direction(
  buy_listings: Iterable[Listing],
  sell_listings: Iterable[Listing],
  sell_platform: Platform,
  min_profit: float,
  min_margin: float,
) -> list[ArbitrageOpportunity]:
  results: list[ArbitrageOpportunity] = []
  sell_pool = list(sell_listings)
  buy_pool = list(buy_listings)
  pairs = find_matches(buy_pool, sell_pool)

  sell_cache = _build_market_cache(sell_pool)
  buy_cache = _build_market_cache(buy_pool)
  demand_cache: dict[str, tuple[float, object]] = {}

  for buy, sell, score in pairs:
    seg = (buy.segment or sell.segment or "account").lower()
    sk = (sell.game, sell.category, seg)
    bk = (buy.game, buy.category, seg)
    # fallback на общий пул игры, если сегмент пустой
    if sk not in sell_cache:
      sk = (sell.game, sell.category, "account")
    if bk not in buy_cache:
      bk = (buy.game, buy.category, "account")
    if sk not in sell_cache or bk not in buy_cache:
      continue

    sell_analysis, market, competing_prices = sell_cache[sk]
    buy_analysis, _, buy_market = buy_cache[bk]

    if sell.game not in demand_cache:
      demand_cache[sell.game] = adjust_demand(sell.game, get_demand_per_day(sell.game))
    demand, _trend = demand_cache[sell.game]

    # Консервативная цена продажи: p25..медиана сегмента того же типа товара.
    # Не тянем к бафнутому одиночному лоту.
    if sell_analysis.fair_count >= 3:
      sell_price = min(
        sell_analysis.optimal_sell,
        sell_analysis.optimal_sell_fast * 1.08 if sell_analysis.optimal_sell_fast > 0 else sell_analysis.optimal_sell,
      )
      # референс не должен быть сильно выше целевой цены
      if sell.price_rub > sell_price * 1.35:
        # перепривяжем цену к рынку, ссылку оставим только если близка
        pass
    else:
      sell_price = sell.price_rub

    # Если референс-лот заметно дороже сегмента — не используем его цену вообще
    # (ссылку в UI покажем, но с пометкой; цена сделки = сегмент)
    opp = estimate_arbitrage(
      buy_listing=buy,
      sell_platform=sell_platform,
      sell_price_rub=sell_price,
      match_score=score,
      competing_prices=sell_analysis.fair_prices or competing_prices,
      demand_per_day=demand,
      market_prices=buy_analysis.fair_prices or buy_market,
      competition_index=market.competition_index,
      market_health=market.health,
      sell_listing=sell if sell.price_rub <= sell_price * 1.25 else None,
      sell_listing_raw_price=sell.price_rub,
      sell_price_analysis=sell_analysis,
      buy_price_analysis=buy_analysis,
    )
    # если референс отбросили — всё равно дадим ближайший честный URL
    if opp.sell_url is None and sell.price_rub <= sell_price * 1.5:
      opp.sell_url = sell.url
      opp.sell_reference_title = sell.title

    if opp.net_profit_rub >= min_profit and opp.margin_percent >= min_margin:
      results.append(opp)

  return results