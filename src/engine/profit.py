from __future__ import annotations

from typing import Optional

from src.engine.fees import compute_fees
from src.engine.liquidity import estimate_liquidity
from src.engine.market import price_zscore
from src.engine.risk import assess_risk
from src.engine.strategies import tag_strategies
from src.config import get_platform_fees
from src.engine.pricing import PriceAnalysis, classify_listing_price
from src.models import ArbitrageOpportunity, Listing, Platform


def sell_cfg_pct(platform: Platform) -> float:
    return get_platform_fees(platform.value)["payout_fee_percent"]


def estimate_arbitrage(
  buy_listing: Listing,
  sell_platform: Platform,
  sell_price_rub: float,
  match_score: float,
  extra_costs_rub: float = 0.0,
  competing_prices: Optional[list[float]] = None,
  demand_per_day: Optional[float] = None,
  market_prices: Optional[list[float]] = None,
  competition_index: float = 0.0,
  market_health: str = "unknown",
  sell_listing: Optional[Listing] = None,
  sell_listing_raw_price: Optional[float] = None,
  sell_price_analysis: Optional[PriceAnalysis] = None,
  buy_price_analysis: Optional[PriceAnalysis] = None,
) -> ArbitrageOpportunity:
  """Полный расчёт сделки: комиссии, ликвидность, риск, EV, стратегии."""
  # --- Комиссии (единый источник правды) ---
  fees = compute_fees(
    buy_price=buy_listing.price_rub,
    buy_platform=buy_listing.platform,
    sell_price=sell_price_rub,
    sell_platform=sell_platform,
    extra_costs_rub=extra_costs_rub,
  )
  gross = sell_price_rub - buy_listing.price_rub
  spread_percent = (gross / buy_listing.price_rub * 100) if buy_listing.price_rub else 0.0

  notes: list[str] = []
  if fees.cross_platform and fees.payout_included:
    payout_total = fees.payout_fee + fees.payout_fixed
    notes.append(
      f"💸 Учтён вывод с {sell_platform.value}: −{payout_total:.0f}₽ "
      f"({sell_cfg_pct(sell_platform):.0f}% от суммы после продажи)"
    )
  elif fees.cross_platform and not fees.payout_included:
    notes.append("⚠️ Вывод не учтён — деньги остаются на площадке продажи")

  if sell_price_analysis and sell_price_analysis.outliers_low + sell_price_analysis.outliers_high > 0:
    notes.append(
      f"📊 Отфильтровано {sell_price_analysis.outliers_low + sell_price_analysis.outliers_high} "
      f"ценовых выбросов · сегмент {sell_price_analysis.p25:.0f}–{sell_price_analysis.p75:.0f}₽"
    )
  if sell_listing_raw_price and sell_price_analysis and sell_price_analysis.median > 0:
    raw_cls = classify_listing_price(sell_listing_raw_price, sell_price_analysis)
    if raw_cls == "above_segment":
      notes.append(
        f"📈 Референс-лот завышен ({sell_listing_raw_price:.0f}₽) — продажа по медиане {sell_price_rub:.0f}₽"
      )
    elif raw_cls == "below_segment":
      notes.append(
        f"📉 Референс-лот занижен ({sell_listing_raw_price:.0f}₽) — ориентир {sell_price_rub:.0f}₽"
      )
  if buy_price_analysis and buy_price_analysis.fair_count >= 3:
    buy_cls = classify_listing_price(buy_listing.price_rub, buy_price_analysis)
    if buy_cls == "below_segment":
      notes.append("✅ Закуп ниже рынка — хорошая точка входа")
    elif buy_cls == "above_segment":
      notes.append("⚠️ Закуп выше рынка — переплата относительно сегмента")
  if fees.yookassa_included and (fees.yookassa_charge > 0 or fees.yookassa_payout > 0):
    notes.append(
      f"💳 YooKassa: списание −{fees.yookassa_charge:.0f}₽, выплата −{fees.yookassa_payout:.0f}₽"
    )

  # --- Ликвидность ---
  liq = None
  if competing_prices is not None and demand_per_day is not None:
    liq = estimate_liquidity(sell_price_rub, competing_prices, demand_per_day)
    if liq.note:
      notes.append(liq.note)
  liq_label = liq.label if liq else "unknown"

  # --- Риск + Expected Value ---
  risk = assess_risk(
    origin=buy_listing.origin,
    seller_rating=buy_listing.seller_rating,
    seller_reviews=buy_listing.seller_reviews,
    match_score=match_score,
    spread_percent=spread_percent,
    liquidity_label=liq_label,
    net_profit=fees.net_profit,
    buy_total=fees.buy_total,
  )
  notes.extend(risk.flags)

  # --- Рыночная аномалия цены закупа ---
  zscore = price_zscore(buy_listing.price_rub, market_prices) if market_prices else 0.0

  # --- Уверенность: совпадение + риск + ликвидность ---
  serious_flags = [n for n in notes if n.startswith(("⚠️", "🚫", "🎣"))]
  if match_score >= 85 and not serious_flags:
    confidence = "high"
  elif match_score >= 70:
    confidence = "medium"
  else:
    confidence = "low"
  if liq and liq.label == "dead" and confidence == "high":
    confidence = "medium"
  if risk.risk_label == "critical":
    confidence = "low"

  opp = ArbitrageOpportunity(
    buy=buy_listing,
    sell_platform=sell_platform,
    sell_price_rub=sell_price_rub,
    sell_url=sell_listing.url if sell_listing else None,
    sell_reference_title=sell_listing.title if sell_listing else None,
    buy_total_rub=fees.buy_total,
    sell_total_rub=fees.net_revenue,
    gross_profit_rub=round(gross, 2),
    net_profit_rub=fees.net_profit,
    margin_percent=fees.margin_percent,
    match_score=match_score,
    confidence=confidence,
    notes=notes,
    liquidity_score=liq.score if liq else 0.0,
    liquidity_label=liq_label,
    est_days_to_sell=liq.est_days_to_sell if liq else 0.0,
    competing_listings=liq.competing_listings if liq else 0,
    price_percentile=liq.price_percentile if liq else 0.0,
    total_fees_rub=fees.total_fees,
    fee_share_percent=fees.fee_share_percent,
    fee_breakdown=fees.parts,
    sell_fee_rub=fees.sell_fee,
    payout_fee_rub=round(fees.payout_fee + fees.payout_fixed, 2),
    yookassa_charge_rub=fees.yookassa_charge,
    yookassa_payout_rub=fees.yookassa_payout,
    cross_platform=fees.cross_platform,
    payout_included=fees.payout_included,
    yookassa_included=fees.yookassa_included,
    net_revenue_rub=fees.net_revenue,
    risk_score=risk.risk_score,
    risk_label=risk.risk_label,
    success_probability=risk.success_probability,
    expected_value_rub=risk.expected_value_rub,
    risk_factors=risk.factors,
    price_zscore=zscore,
    sell_listing_raw_price=sell_listing_raw_price,
    optimal_sell_rub=sell_price_analysis.optimal_sell if sell_price_analysis else sell_price_rub,
    market_median_rub=sell_price_analysis.median if sell_price_analysis else 0.0,
    price_segment_low=sell_price_analysis.p25 if sell_price_analysis else 0.0,
    price_segment_high=sell_price_analysis.p75 if sell_price_analysis else 0.0,
    price_outliers_filtered=(
      (sell_price_analysis.outliers_low + sell_price_analysis.outliers_high)
      if sell_price_analysis else 0
    ),
    competition_index=competition_index,
    market_health=market_health,
  )
  opp.strategies = tag_strategies(opp)
  return opp
