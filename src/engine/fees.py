"""Полная модель комиссий площадок (без YooKassa — вывод вручную)."""
from __future__ import annotations

from dataclasses import dataclass, field

from src.config import get_fees_config, get_platform_fees
from src.models import Platform


@dataclass
class FeeBreakdown:
    buy_price: float
    buy_fee: float
    deposit_fee: float
    buy_total: float
    sell_price: float
    sell_fee: float
    payout_fee: float
    payout_fixed: float
    transfer_friction: float
    yookassa_charge: float       # списание с карты (приём платежа)
    yookassa_payout: float       # выплата на карту (Payouts API)
    net_revenue: float           # на карту после всех сборов
    net_profit: float
    total_fees: float
    margin_percent: float
    fee_share_percent: float
    cross_platform: bool
    payout_included: bool
    yookassa_included: bool
    parts: dict = field(default_factory=dict)


def _gateway_fee(amount: float, percent: float, fixed: float, vat_percent: float) -> float:
    """Комиссия YooKassa: процент + фикс + НДС на сумму комиссии."""
    if amount <= 0:
        return 0.0
    commission = amount * percent / 100 + fixed
    vat = commission * vat_percent / 100
    return round(commission + vat, 2)


def compute_fees(
    buy_price: float,
    buy_platform: Platform,
    sell_price: float,
    sell_platform: Platform,
    extra_costs_rub: float = 0.0,
    include_payout: bool | None = None,
    include_yookassa: bool | None = None,
) -> FeeBreakdown:
    buy_cfg = get_platform_fees(buy_platform.value)
    sell_cfg = get_platform_fees(sell_platform.value)
    fees_cfg = get_fees_config()
    yk_cfg = fees_cfg.get("yookassa", {}) or {}

    cross_platform = buy_platform != sell_platform
    if include_payout is None:
        include_payout = cross_platform
    if include_yookassa is None:
        include_yookassa = bool(fees_cfg.get("include_yookassa_fees", False))

    vat = float(yk_cfg.get("vat_on_commission_percent", 22))
    yk_charge_pct = float(yk_cfg.get("charge_percent", 3.5))
    yk_payout_pct = float(yk_cfg.get("payout_percent", 2.8))
    yk_payout_fixed = float(yk_cfg.get("payout_fixed_rub", 30))

    buy_fee = buy_price * buy_cfg["buy_fee_percent"] / 100

    deposit_fee = 0.0
    if cross_platform and fees_cfg.get("include_deposit_fee", False):
        deposit_fee = buy_price * buy_cfg["deposit_fee_percent"] / 100

    transfer_friction = float(fees_cfg.get("cross_platform_transfer_rub", 0)) if cross_platform else 0.0
    buy_total = buy_price + buy_fee + deposit_fee + transfer_friction + extra_costs_rub

    sell_fee = sell_price * sell_cfg["sell_fee_percent"] / 100
    after_sell = sell_price - sell_fee

    if include_payout:
        payout_fee = after_sell * sell_cfg["payout_fee_percent"] / 100
        payout_fixed = sell_cfg["payout_fixed_rub"]
    else:
        payout_fee = 0.0
        payout_fixed = 0.0

    payout_total = payout_fee + payout_fixed
    after_platform = after_sell - payout_total

    yookassa_charge = 0.0
    yookassa_payout = 0.0
    if include_yookassa:
        # Пополнение с карты под закуп
        yookassa_charge = _gateway_fee(buy_total, yk_charge_pct, 0, vat)
        # Выплата прибыли на карту после продажи и вывода с площадки
        yookassa_payout = _gateway_fee(after_platform, yk_payout_pct, yk_payout_fixed, vat)

    net_revenue = after_platform - yookassa_payout
    net_profit = net_revenue - buy_total - yookassa_charge
    total_fees = (
        buy_fee + deposit_fee + sell_fee + payout_total
        + transfer_friction + yookassa_charge + yookassa_payout
    )
    margin = (net_profit / (buy_total + yookassa_charge) * 100) if (buy_total + yookassa_charge) > 0 else 0.0
    fee_share = (total_fees / sell_price * 100) if sell_price > 0 else 0.0

    parts: dict[str, float] = {"Закуп": round(buy_price + buy_fee, 2)}
    if deposit_fee > 0:
        parts["Пополнение"] = round(deposit_fee, 2)
    if transfer_friction > 0:
        parts["Перевод между площадками"] = round(transfer_friction, 2)
    if yookassa_charge > 0:
        parts["YooKassa: списание с карты"] = yookassa_charge
    parts["Комиссия продажи"] = round(sell_fee, 2)
    if payout_total > 0:
        parts["Комиссия вывода"] = round(payout_total, 2)
    if yookassa_payout > 0:
        parts["YooKassa: выплата на карту"] = yookassa_payout
    parts["Чистая прибыль"] = round(net_profit, 2)

    return FeeBreakdown(
        buy_price=round(buy_price, 2),
        buy_fee=round(buy_fee, 2),
        deposit_fee=round(deposit_fee, 2),
        buy_total=round(buy_total, 2),
        sell_price=round(sell_price, 2),
        sell_fee=round(sell_fee, 2),
        payout_fee=round(payout_fee, 2),
        payout_fixed=round(payout_fixed, 2),
        transfer_friction=round(transfer_friction, 2),
        yookassa_charge=yookassa_charge,
        yookassa_payout=yookassa_payout,
        net_revenue=round(net_revenue, 2),
        net_profit=round(net_profit, 2),
        total_fees=round(total_fees, 2),
        margin_percent=round(margin, 2),
        fee_share_percent=round(fee_share, 2),
        cross_platform=cross_platform,
        payout_included=include_payout,
        yookassa_included=include_yookassa,
        parts=parts,
    )
