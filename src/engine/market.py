"""Рыночная аналитика по категориям (с фильтрацией выбросов)."""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from src.config import get_demand_per_day
from src.engine.pricing import PriceAnalysis, analyze_prices
from src.models import Listing


@dataclass
class MarketStats:
    game: str
    category: str
    supply: int
    fair_supply: int
    outliers_low: int
    outliers_high: int
    demand_per_day: float
    competition_index: float
    min_price: float
    p25: float
    median: float
    p75: float
    max_price: float
    mean: float
    volatility_percent: float
    optimal_buy: float
    optimal_sell: float
    optimal_sell_fast: float
    optimal_sell_premium: float
    health: str


def compute_market_stats(game: str, category: str, prices: list[float]) -> MarketStats:
    demand = get_demand_per_day(game)
    pa = analyze_prices(prices)
    supply = pa.raw_count
    fair = pa.fair_count
    comp_index = round(supply / (demand * 30), 2) if demand > 0 else 0.0

    if not pa.fair_prices:
        return MarketStats(
            game, category, supply, 0, pa.outliers_low, pa.outliers_high,
            demand, comp_index, 0, 0, 0, 0, 0, 0, 0.0,
            0, 0, 0, 0, "balanced",
        )

    s = sorted(pa.fair_prices)
    mean = pa.trimmed_mean
    stdev = statistics.pstdev(s) if len(s) > 1 else 0.0
    vol = round(stdev / mean * 100, 1) if mean else 0.0

    if comp_index > 1.5:
        health = "buyers_market"
    elif comp_index < 0.6:
        health = "sellers_market"
    else:
        health = "balanced"

    return MarketStats(
        game=game,
        category=category,
        supply=supply,
        fair_supply=fair,
        outliers_low=pa.outliers_low,
        outliers_high=pa.outliers_high,
        demand_per_day=demand,
        competition_index=comp_index,
        min_price=pa.min_fair,
        p25=pa.p25,
        median=pa.median,
        p75=pa.p75,
        max_price=pa.max_fair,
        mean=mean,
        volatility_percent=vol,
        optimal_buy=pa.optimal_buy,
        optimal_sell=pa.optimal_sell,
        optimal_sell_fast=pa.optimal_sell_fast,
        optimal_sell_premium=pa.optimal_sell_premium,
        health=health,
    )


def price_zscore(price: float, prices: list[float]) -> float:
    from src.engine.pricing import robust_zscore
    return robust_zscore(price, prices)


def build_market_map(listings: list[Listing]) -> dict[str, MarketStats]:
    groups: dict[tuple[str, str], list[float]] = {}
    for lst in listings:
        groups.setdefault((lst.game, lst.category), []).append(lst.price_rub)
    return {
        f"{g}|{c}": compute_market_stats(g, c, prices)
        for (g, c), prices in groups.items()
    }
