"""Оценка ликвидности: как быстро реально продать купленный лот.

Идея: чем больше похожих лотов уже висит на площадке продажи (конкуренция),
чем ниже спрос по игре и чем выше твоя цена относительно конкурентов —
тем дольше лот будет продаваться. Считаем ожидаемые дни до продажи и score.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class LiquidityResult:
    score: float          # 0-100, выше = ликвиднее
    label: str            # fast / medium / slow / dead
    est_days_to_sell: float
    competing_listings: int
    price_percentile: float  # 0 = самый дешёвый, 100 = самый дорогой
    note: str | None = None


# Порог: дороже скольки дней считаем «замороженным» капиталом
_LABELS = (
    (3.0, "fast"),
    (10.0, "medium"),
    (30.0, "slow"),
)


def _label_for(days: float) -> str:
    for threshold, name in _LABELS:
        if days <= threshold:
            return name
    return "dead"


def estimate_liquidity(
    sell_price: float,
    competing_prices: list[float],
    demand_per_day: float,
) -> LiquidityResult:
    """
    sell_price       — по какой цене планируешь продавать
    competing_prices — цены похожих лотов (желательно уже без выбросов)
    demand_per_day   — оценка продаж в день по этой игре/категории
    """
    from src.engine.pricing import analyze_prices

    analysis = analyze_prices(competing_prices)
    pool = analysis.fair_prices if analysis.fair_count >= 3 else competing_prices
    demand = max(demand_per_day, 0.02)
    supply = len(pool)

    cheaper = sum(1 for p in pool if p <= sell_price)
    price_percentile = round(cheaper / supply * 100, 1) if supply else 0.0
    queue_ahead = cheaper + 1
    est_days = queue_ahead / demand

    # Штраф за перегретый рынок (очень много предложений на слабый спрос)
    if supply > 0:
        oversupply = supply / (demand * 30)  # предложений на месячный спрос
        if oversupply > 1:
            est_days *= min(oversupply, 3.0)

    est_days = round(est_days, 1)

    # Плавный score: days=0 -> 100, ~7д -> 56, ~14д -> 31, ~30д -> 8
    score = round(max(3.0, min(100.0, 100 * math.exp(-est_days / 12))), 1)
    label = _label_for(est_days)

    note = None
    if label == "slow":
        note = f"🐌 Средняя ликвидность: ~{est_days:.0f} дн. до продажи"
    elif label == "dead":
        note = f"🧊 Низкая ликвидность: ~{est_days:.0f} дн. — капитал зависнет"

    return LiquidityResult(
        score=score,
        label=label,
        est_days_to_sell=est_days,
        competing_listings=supply,
        price_percentile=price_percentile,
        note=note,
    )
