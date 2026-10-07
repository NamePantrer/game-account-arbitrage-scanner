"""Робастный анализ цен с фильтрацией выбросов.

Продавцы часто занижают или завышают цены — это ломает среднее и z-score.
Здесь: IQR-фильтр, MAD-zscore и оптимальный ценовой сегмент (p25–p75).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from src.config import get_pricing_config


@dataclass
class PriceAnalysis:
    raw_count: int
    fair_count: int
    outliers_low: int
    outliers_high: int
    min_fair: float
    p25: float
    median: float
    p75: float
    max_fair: float
    trimmed_mean: float
    iqr: float
    mad: float
    optimal_buy: float       # целевая цена закупа (нижняя часть сегмента)
    optimal_sell: float      # реалистичная цена продажи (медиана сегмента)
    optimal_sell_fast: float  # быстрая продажа (p25 сегмента)
    optimal_sell_premium: float  # терпеливая продажа (p75 сегмента)
    fair_prices: list[float]


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def filter_outliers(prices: list[float]) -> tuple[list[float], int, int]:
    """Tukey IQR: возвращает (fair_prices, outliers_low, outliers_high)."""
    cfg = get_pricing_config()
    mult = float(cfg.get("outlier_iqr_multiplier", 1.5))
    min_samples = int(cfg.get("min_samples_for_filter", 5))

    if len(prices) < min_samples:
        return list(prices), 0, 0

    s = sorted(prices)
    q1 = _percentile(s, 0.25)
    q3 = _percentile(s, 0.75)
    iqr = q3 - q1
    if iqr <= 0:
        return list(prices), 0, 0

    lo_fence = q1 - mult * iqr
    hi_fence = q3 + mult * iqr
    fair = [p for p in prices if lo_fence <= p <= hi_fence]
    low = sum(1 for p in prices if p < lo_fence)
    high = sum(1 for p in prices if p > hi_fence)

    # Если отфильтровали слишком много — ослабляем (мало данных)
    if len(fair) < max(3, len(prices) * 0.4):
        mult2 = mult * 1.5
        lo_fence = q1 - mult2 * iqr
        hi_fence = q3 + mult2 * iqr
        fair = [p for p in prices if lo_fence <= p <= hi_fence]
        low = sum(1 for p in prices if p < lo_fence)
        high = sum(1 for p in prices if p > hi_fence)

    return fair if fair else list(prices), low, high


def analyze_prices(prices: list[float]) -> PriceAnalysis:
    """Полный анализ: фильтрация выбросов + оптимальный сегмент."""
    cfg = get_pricing_config()
    buy_pct = float(cfg.get("optimal_buy_percentile", 0.25))
    sell_pct = float(cfg.get("optimal_sell_percentile", 0.50))

    raw = [p for p in prices if p > 0]
    if not raw:
        return PriceAnalysis(
            raw_count=0, fair_count=0, outliers_low=0, outliers_high=0,
            min_fair=0, p25=0, median=0, p75=0, max_fair=0,
            trimmed_mean=0, iqr=0, mad=0,
            optimal_buy=0, optimal_sell=0, optimal_sell_fast=0,
            optimal_sell_premium=0, fair_prices=[],
        )

    fair, low, high = filter_outliers(raw)
    s = sorted(fair)
    p25 = _percentile(s, 0.25)
    med = _percentile(s, 0.50)
    p75 = _percentile(s, 0.75)
    iqr = p75 - p25
    trimmed_mean = statistics.fmean(s)

    mad = statistics.median([abs(x - med) for x in s]) if len(s) > 1 else 0.0

    return PriceAnalysis(
        raw_count=len(raw),
        fair_count=len(fair),
        outliers_low=low,
        outliers_high=high,
        min_fair=round(s[0], 2),
        p25=round(p25, 2),
        median=round(med, 2),
        p75=round(p75, 2),
        max_fair=round(s[-1], 2),
        trimmed_mean=round(trimmed_mean, 2),
        iqr=round(iqr, 2),
        mad=round(mad, 2),
        optimal_buy=round(_percentile(s, buy_pct), 2),
        optimal_sell=round(_percentile(s, sell_pct), 2),
        optimal_sell_fast=round(p25, 2),
        optimal_sell_premium=round(p75, 2),
        fair_prices=fair,
    )


def robust_zscore(price: float, prices: list[float]) -> float:
    """MAD-zscore: устойчив к выбросам."""
    analysis = analyze_prices(prices)
    fair = analysis.fair_prices
    if len(fair) < 2 or analysis.mad == 0:
        return 0.0
    med = analysis.median
    # 1.4826 — константа для нормального распределения
    mad_scaled = analysis.mad * 1.4826
    if mad_scaled == 0:
        return 0.0
    return round((price - med) / mad_scaled, 2)


def classify_listing_price(price: float, analysis: PriceAnalysis) -> str:
    """Где лот относительно честного сегмента."""
    if analysis.fair_count == 0:
        return "unknown"
    if price < analysis.p25:
        return "below_segment"   # занижен — возможно скам или срочная продажа
    if price > analysis.p75:
        return "above_segment"   # завышен — долго продаваться
    return "fair"
