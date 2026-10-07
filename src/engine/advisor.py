"""Советник по раскрутке капитала.

Берёт бюджет и весь пул сделок, подбирает лучшие ДОСТУПНЫЕ позиции,
строит стратегию с реинвестом (компаундинг) и выдаёт конкретные шаги
со ссылками: где купить, у кого, где продать.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.config import get_risk_config
from src.engine.strategies import STRATEGY_TITLES
from src.models import ArbitrageOpportunity


@dataclass
class Pick:
    title: str
    game: str
    strategies: list[str]
    # где купить
    buy_platform: str
    buy_price: float
    buy_total: float
    buy_url: str | None
    seller_name: str | None
    seller_url: str | None
    seller_rating: float | None
    seller_reviews: int | None
    origin: str | None
    # где продать
    sell_platform: str
    sell_price: float
    sell_url: str | None
    # экономика
    net_profit: float
    net_revenue: float
    expected_value: float
    sell_fee: float
    payout_fee: float
    yookassa_charge: float
    yookassa_payout: float
    total_fees: float
    risk_score: float
    risk_label: str
    success_probability: float
    est_days_to_sell: float
    roi_percent: float
    action: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class GrowthRound:
    round_no: int
    capital_start: float
    pick_title: str
    pick_game: str
    qty: int
    invested: float
    expected_gain: float
    capital_end: float
    days: float
    cumulative_days: float


@dataclass
class AdvisorPlan:
    budget: float
    affordable_count: int
    strategy_name: str
    strategy_desc: str
    best_pick: Pick | None
    picks: list[Pick]
    steps: list[str]
    growth: list[GrowthRound]
    projected_capital: float
    projected_days: float
    projected_roi_percent: float
    warnings: list[str]


def _efficiency(o: ArbitrageOpportunity) -> float:
    """Скорость роста капитала: EV на рубль вложений в день."""
    days = max(o.est_days_to_sell, 1.0)
    cost = max(o.buy_total_rub, 1.0)
    return o.expected_value_rub / cost / days


def _make_pick(o: ArbitrageOpportunity) -> Pick:
    days = max(o.est_days_to_sell, 1.0)
    roi = round(o.net_profit_rub / max(o.buy_total_rub, 1) * 100, 1)
    seller = o.buy.seller_name or "продавец"

    action = (
        f"Купить на {o.buy.platform.value.upper()} за {o.buy.price_rub:.0f}₽ "
        f"у «{seller}» → продать на {o.sell_platform.value.upper()} за {o.sell_price_rub:.0f}₽"
    )

    warnings: list[str] = []
    if o.buy.origin in {"stealer", "phishing", "brute"}:
        warnings.append("Опасный источник аккаунта — высокий риск отката")
    if o.buy.seller_reviews is not None and o.buy.seller_reviews < 5:
        warnings.append("У продавца мало отзывов — проверь репутацию")
    if o.liquidity_label in {"slow", "dead"}:
        warnings.append(f"Продажа может занять ~{o.est_days_to_sell:.0f} дн.")
    if o.cross_platform and o.payout_included and o.payout_fee_rub > 0:
        warnings.append(
            f"После продажи выведи деньги с {o.sell_platform.value.upper()} "
            f"(−{o.payout_fee_rub:.0f}₽ комиссия вывода уже в прибыли)"
        )
    elif o.cross_platform:
        warnings.append("Кросс-площадка: деньги не перекидываются напрямую — только вывод и пополнение")

    return Pick(
        title=o.buy.title,
        game=o.buy.game,
        strategies=[STRATEGY_TITLES.get(s, s) for s in o.strategies],
        buy_platform=o.buy.platform.value,
        buy_price=o.buy.price_rub,
        buy_total=o.buy_total_rub,
        buy_url=o.buy.url,
        seller_name=o.buy.seller_name,
        seller_url=o.buy.seller_url,
        seller_rating=o.buy.seller_rating,
        seller_reviews=o.buy.seller_reviews,
        origin=o.buy.origin,
        sell_platform=o.sell_platform.value,
        sell_price=o.sell_price_rub,
        sell_url=o.sell_url,
        net_profit=o.net_profit_rub,
        net_revenue=o.net_revenue_rub,
        expected_value=o.expected_value_rub,
        sell_fee=o.sell_fee_rub,
        payout_fee=o.payout_fee_rub,
        yookassa_charge=o.yookassa_charge_rub,
        yookassa_payout=o.yookassa_payout_rub,
        total_fees=o.total_fees_rub,
        risk_score=o.risk_score,
        risk_label=o.risk_label,
        success_probability=o.success_probability,
        est_days_to_sell=o.est_days_to_sell,
        roi_percent=roi,
        action=action,
        warnings=warnings,
    )


def _pick_strategy_name(budget: float) -> tuple[str, str]:
    if budget < 3000:
        return (
            "🚀 Агрессивная раскрутка (компаундинг)",
            "Маленький бюджет: берём дешёвые, быстрые и надёжные флипы по одному, "
            "каждый раз вкладывая всю выручку обратно. Цель — быстро наращивать капитал за счёт оборота.",
        )
    if budget < 15000:
        return (
            "⚖️ Сбалансированный рост",
            "Средний бюджет: 2-4 сделки параллельно, баланс скорости оборота и размера прибыли.",
        )
    return (
        "🏦 Диверсифицированный портфель",
        "Крупный бюджет: распределяем по нескольким играм и стратегиям, снижая риск.",
    )


def build_advisor_plan(
    opportunities: list[ArbitrageOpportunity],
    budget: float,
    max_cycles: int = 8,
) -> AdvisorPlan:
    cfg = get_risk_config()
    # Для раскрутки маленького бюджета — жёстче по риску
    max_risk = 40 if budget < 3000 else 55

    affordable = [
        o for o in opportunities
        if o.buy_total_rub <= budget
        and o.expected_value_rub > 0
        and o.risk_score <= max_risk
    ]
    affordable.sort(key=_efficiency, reverse=True)

    strategy_name, strategy_desc = _pick_strategy_name(budget)
    warnings: list[str] = []

    if not affordable:
        warnings.append(
            f"Нет подходящих сделок в бюджет {budget:.0f}₽ с приемлемым риском. "
            "Попробуй увеличить бюджет или снизить требования."
        )
        return AdvisorPlan(
            budget=budget, affordable_count=0,
            strategy_name=strategy_name, strategy_desc=strategy_desc,
            best_pick=None, picks=[], steps=[], growth=[],
            projected_capital=budget, projected_days=0, projected_roi_percent=0,
            warnings=warnings,
        )

    picks = [_make_pick(o) for o in affordable[:10]]
    best = picks[0]

    # --- Компаундинг: реинвестируем выручку ---
    growth: list[GrowthRound] = []
    capital = budget
    cum_days = 0.0
    for rnd in range(1, max_cycles + 1):
        pool = [o for o in affordable if o.buy_total_rub <= capital]
        if not pool:
            break
        top = max(pool, key=_efficiency)
        qty = max(1, int(capital // top.buy_total_rub))
        invested = round(qty * top.buy_total_rub, 2)
        expected_gain = round(qty * top.expected_value_rub, 2)
        capital_end = round(capital + expected_gain, 2)
        days = max(top.est_days_to_sell, 1.0)
        cum_days = round(cum_days + days, 1)
        growth.append(GrowthRound(
            round_no=rnd,
            capital_start=round(capital, 2),
            pick_title=top.buy.title,
            pick_game=top.buy.game,
            qty=qty,
            invested=invested,
            expected_gain=expected_gain,
            capital_end=capital_end,
            days=days,
            cumulative_days=cum_days,
        ))
        capital = capital_end
        if capital >= budget * 3 or cum_days > 120:
            break

    projected_capital = round(capital, 2)
    projected_days = cum_days
    projected_roi = round((projected_capital - budget) / budget * 100, 1) if budget else 0.0

    # --- Человеко-понятные шаги ---
    steps = [
        f"1. Стартовый капитал: {budget:.0f}₽. Стратегия: {strategy_name}.",
        f"2. Первая сделка — «{best.title}» ({best.game}): {best.action}.",
        f"   Прибыль ~{best.net_profit:.0f}₽ на руки (EV {best.expected_value:.0f}₽), "
        f"вывод с {best.sell_platform.upper()} −{best.payout_fee:.0f}₽ уже учтён, "
        f"срок ~{best.est_days_to_sell:.0f} дн., риск {best.risk_label}.",
        "3. После продажи выведи деньги с площадки продажи и пополни площадку закупа для следующей сделки.",
        "4. Вкладывай всю выручку на руки в следующую лучшую сделку (см. таблицу роста).",
        f"5. Через ~{projected_days:.0f} дн. капитал вырастет примерно до {projected_capital:.0f}₽ "
        f"(+{projected_roi:.0f}%).",
        "6. Каждую сделку проверяй вручную по ссылке перед покупкой (риск скама/отката).",
    ]

    if best.risk_label in {"high", "critical"}:
        warnings.append("Лучшая по доходности сделка рискованная — оцени, готов ли потерять вложение.")
    if budget < 1000:
        warnings.append("Очень маленький бюджет: выбор сделок ограничен, рост будет медленным.")

    return AdvisorPlan(
        budget=budget,
        affordable_count=len(affordable),
        strategy_name=strategy_name,
        strategy_desc=strategy_desc,
        best_pick=best,
        picks=picks,
        steps=steps,
        growth=growth,
        projected_capital=projected_capital,
        projected_days=projected_days,
        projected_roi_percent=projected_roi,
        warnings=warnings,
    )
