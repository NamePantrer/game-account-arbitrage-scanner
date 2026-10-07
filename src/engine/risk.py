"""Риск-аналитика сделки.

Считает:
- risk_score (0-100, выше = опаснее) из источника аккаунта, репутации продавца,
  совпадения лотов, ценовой аномалии и ликвидности;
- success_probability — вероятность, что сделка пройдёт без отката/бана/скама;
- expected_value — матожидание прибыли с учётом риска потери капитала.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.config import get_risk_config


@dataclass
class RiskResult:
    risk_score: float          # 0-100
    risk_label: str            # low / medium / high / critical
    success_probability: float  # 0-1
    expected_value_rub: float
    factors: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


def _label_for(score: float) -> str:
    if score < 25:
        return "low"
    if score < 50:
        return "medium"
    if score < 75:
        return "high"
    return "critical"


def assess_risk(
    *,
    origin: Optional[str],
    seller_rating: Optional[float],
    seller_reviews: Optional[int],
    match_score: float,
    spread_percent: float,
    liquidity_label: str,
    net_profit: float,
    buy_total: float,
) -> RiskResult:
    cfg = get_risk_config()
    origin_fail = cfg.get("origin_fail_prob", {})
    loss_given_failure = float(cfg.get("loss_given_failure", 0.6))
    anomaly_threshold = float(cfg.get("anomaly_spread_percent", 60))

    factors: dict[str, float] = {}
    flags: list[str] = []

    # 1) Источник аккаунта — главный фактор скама/отката
    o = (origin or "unknown").lower()
    base_fail = float(origin_fail.get(o, origin_fail.get("unknown", 0.15)))
    origin_risk = base_fail * 100
    factors["origin"] = round(origin_risk, 1)
    if o in {"stealer", "phishing", "brute"}:
        flags.append("🚫 Источник аккаунта опасен (возможен откат/бан)")

    # 2) Репутация продавца
    rep_risk = 0.0
    if seller_reviews is not None:
        if seller_reviews < 5:
            rep_risk += 22
            flags.append("⚠️ У продавца мало отзывов")
        elif seller_reviews < 20:
            rep_risk += 10
    if seller_rating is not None and seller_rating < 4.5:
        rep_risk += 15
        flags.append("⚠️ Низкий рейтинг продавца")
    factors["reputation"] = round(rep_risk, 1)

    # 3) Совпадение лотов — риск купить не то
    match_risk = max(0.0, (100 - match_score)) * 0.35
    factors["match"] = round(match_risk, 1)
    if match_score < 70:
        flags.append("⚠️ Лоты слабо совпадают — проверь вручную")

    # 4) Ценовая аномалия: слишком хороший спред = подозрительно
    anomaly_risk = 0.0
    if spread_percent > anomaly_threshold:
        anomaly_risk = min(40.0, (spread_percent - anomaly_threshold) * 0.8 + 15)
        flags.append("🎣 Аномально большой спред — вероятен скам или ошибка в лоте")
    factors["anomaly"] = round(anomaly_risk, 1)

    # 5) Ликвидность — риск заморозки капитала
    liq_risk = {"fast": 0, "medium": 6, "slow": 18, "dead": 32, "unknown": 12}.get(
        liquidity_label, 12
    )
    factors["liquidity"] = float(liq_risk)

    # Итоговый risk_score — взвешенная сумма, обрезаем 0..100
    raw = (
        origin_risk * 0.9
        + rep_risk * 0.8
        + match_risk * 0.7
        + anomaly_risk * 1.0
        + liq_risk * 0.8
    )
    risk_score = round(max(0.0, min(100.0, raw)), 1)

    # Вероятность успеха: базово из fail-prob источника, ухудшаем за флаги
    success = 1.0 - base_fail
    success -= rep_risk / 100 * 0.4
    success -= match_risk / 100 * 0.5
    if spread_percent > anomaly_threshold:
        success -= 0.15
    success = round(max(0.05, min(0.98, success)), 3)

    # Expected Value: с вероятностью success получаем прибыль,
    # иначе теряем часть вложений (loss_given_failure)
    ev = success * net_profit - (1 - success) * buy_total * loss_given_failure
    expected_value = round(ev, 2)

    return RiskResult(
        risk_score=risk_score,
        risk_label=_label_for(risk_score),
        success_probability=success,
        expected_value_rub=expected_value,
        factors=factors,
        flags=flags,
    )
