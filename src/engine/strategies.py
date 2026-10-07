"""Торговые стратегии и распределение капитала.

- tag_strategies: помечает сделку подходящими стратегиями;
- build_portfolio: жадно распределяет бюджет по лучшим сделкам с учётом
  диверсификации, лимита на сделку и максимального риска.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.config import get_portfolio_config, get_strategies_config
from src.models import ArbitrageOpportunity

STRATEGY_TITLES = {
    "fast_flip": "⚡ Быстрый флип",
    "high_margin": "📈 Высокая маржа",
    "volume": "🔁 Объём (дешёвые)",
    "premium": "💎 Премиум",
    "safe": "🛡 Безопасная",
    "value": "🎯 По EV",
}


@dataclass
class PortfolioItem:
    opportunity: ArbitrageOpportunity
    allocation_rub: float
    expected_return_rub: float


@dataclass
class Portfolio:
    budget: float
    invested: float
    expected_profit: float
    expected_value: float
    avg_risk: float
    avg_days: float
    roi_percent: float
    items: list[PortfolioItem] = field(default_factory=list)
    leftover: float = 0.0


def tag_strategies(opp: ArbitrageOpportunity) -> list[str]:
    cfg = get_strategies_config()
    tags: list[str] = []

    if opp.est_days_to_sell and opp.est_days_to_sell <= cfg.get("fast_flip_max_days", 5):
        tags.append("fast_flip")
    if opp.margin_percent >= cfg.get("high_margin_min_percent", 30):
        tags.append("high_margin")
    if opp.buy.price_rub <= cfg.get("volume_max_price", 800) and opp.margin_percent >= 12:
        tags.append("volume")
    if opp.net_profit_rub >= cfg.get("premium_min_profit", 800):
        tags.append("premium")
    if opp.risk_score <= cfg.get("safe_max_risk", 25) and opp.liquidity_label in {"fast", "medium"}:
        tags.append("safe")
    if opp.expected_value_rub > 0 and opp.expected_value_rub >= opp.net_profit_rub * 0.6:
        tags.append("value")

    return tags


def build_portfolio(
    opportunities: list[ArbitrageOpportunity],
    budget: float | None = None,
) -> Portfolio:
    cfg = get_portfolio_config()
    budget = budget if budget is not None else float(cfg.get("default_budget_rub", 20000))
    max_share = float(cfg.get("max_share_per_deal", 0.25))
    max_risk = float(cfg.get("max_risk_score", 55))
    per_deal_cap = budget * max_share

    # Кандидаты: положительный EV и приемлемый риск
    candidates = [
        o for o in opportunities
        if o.expected_value_rub > 0 and o.risk_score <= max_risk
    ]
    # Ранжируем по эффективности капитала: EV на рубль вложений в день
    def efficiency(o: ArbitrageOpportunity) -> float:
        days = max(o.est_days_to_sell, 1.0)
        cost = max(o.buy_total_rub, 1.0)
        return o.expected_value_rub / cost / days

    candidates.sort(key=efficiency, reverse=True)

    items: list[PortfolioItem] = []
    remaining = budget
    games_used: dict[str, int] = {}

    for o in candidates:
        cost = o.buy_total_rub
        if cost > remaining or cost > per_deal_cap:
            continue
        # Диверсификация: не больше 3 сделок на одну игру
        if games_used.get(o.buy.game, 0) >= 3:
            continue
        items.append(PortfolioItem(
            opportunity=o,
            allocation_rub=round(cost, 2),
            expected_return_rub=round(o.expected_value_rub, 2),
        ))
        remaining -= cost
        games_used[o.buy.game] = games_used.get(o.buy.game, 0) + 1

    invested = round(budget - remaining, 2)
    exp_profit = round(sum(i.opportunity.net_profit_rub for i in items), 2)
    exp_value = round(sum(i.expected_return_rub for i in items), 2)
    avg_risk = round(sum(i.opportunity.risk_score for i in items) / len(items), 1) if items else 0.0
    avg_days = round(sum(i.opportunity.est_days_to_sell for i in items) / len(items), 1) if items else 0.0
    roi = round(exp_value / invested * 100, 2) if invested else 0.0

    return Portfolio(
        budget=round(budget, 2),
        invested=invested,
        expected_profit=exp_profit,
        expected_value=exp_value,
        avg_risk=avg_risk,
        avg_days=avg_days,
        roi_percent=roi,
        items=items,
        leftover=round(remaining, 2),
    )
