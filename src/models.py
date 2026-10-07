from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Platform(str, Enum):
    FUNPAY = "funpay"
    PLAYEROK = "playerok"
    LOLZ = "lolz"


class Listing(BaseModel):
    """Нормализованный лот с любой площадки."""

    platform: Platform
    external_id: str
    title: str
    description: str = ""
    price_rub: float
    url: str
    game: str
    category: str
    seller_name: Optional[str] = None
    seller_url: Optional[str] = None
    seller_rating: Optional[float] = None
    seller_reviews: Optional[int] = None
    origin: Optional[str] = None  # lolz: personal, autoreg, stealer...
    # сегмент товара для честного ценообразования: account | currency | key | other
    segment: str = "account"
    fetched_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def search_key(self) -> str:
        """Ключ для fuzzy-matching между площадками."""
        return f"{self.game}|{self.category}|{self.title}".lower()


class ArbitrageOpportunity(BaseModel):
    """Найденная возможность арбитража."""

    buy: Listing
    sell_platform: Platform
    sell_price_rub: float
    sell_url: Optional[str] = None            # ссылка на сопоставимый лот на площадке продажи
    sell_reference_title: Optional[str] = None
    buy_total_rub: float
    sell_total_rub: float
    gross_profit_rub: float
    net_profit_rub: float
    margin_percent: float
    match_score: float  # 0-100, насколько лоты похожи
    confidence: str  # low / medium / high
    notes: list[str] = Field(default_factory=list)

    # Ликвидность: как быстро реально продать купленный лот
    liquidity_score: float = 0.0        # 0-100, выше = ликвиднее
    liquidity_label: str = "unknown"    # fast / medium / slow / dead
    est_days_to_sell: float = 0.0
    competing_listings: int = 0
    price_percentile: float = 0.0

    # Комиссии (полная разбивка)
    total_fees_rub: float = 0.0
    fee_share_percent: float = 0.0
    fee_breakdown: dict = Field(default_factory=dict)
    sell_fee_rub: float = 0.0
    payout_fee_rub: float = 0.0
    yookassa_charge_rub: float = 0.0
    yookassa_payout_rub: float = 0.0
    net_revenue_rub: float = 0.0
    cross_platform: bool = True
    payout_included: bool = True
    yookassa_included: bool = True

    # Риск-аналитика
    risk_score: float = 0.0             # 0-100, выше = опаснее
    risk_label: str = "unknown"         # low / medium / high / critical
    success_probability: float = 0.0    # 0-1
    expected_value_rub: float = 0.0     # матожидание с учётом риска
    risk_factors: dict = Field(default_factory=dict)

    # Рыночный контекст
    price_zscore: float = 0.0           # отклонение цены закупа от рынка (сигмы)
    sell_listing_raw_price: Optional[float] = None
    optimal_sell_rub: float = 0.0
    market_median_rub: float = 0.0
    price_segment_low: float = 0.0
    price_segment_high: float = 0.0
    price_outliers_filtered: int = 0
    competition_index: float = 0.0      # предложение / спрос
    market_health: str = "unknown"      # buyers_market / balanced / sellers_market

    # Стратегии
    strategies: list[str] = Field(default_factory=list)

    @property
    def is_profitable(self) -> bool:
        return self.net_profit_rub > 0 and self.margin_percent > 0

    @property
    def daily_profit_rub(self) -> float:
        """Прибыль с поправкой на срок продажи — реальная эффективность капитала."""
        days = max(self.est_days_to_sell, 1.0)
        return round(self.net_profit_rub / days, 2)

    @property
    def annualized_roi_percent(self) -> float:
        """Годовая доходность капитала с учётом срока продажи."""
        days = max(self.est_days_to_sell, 1.0)
        if self.buy_total_rub <= 0:
            return 0.0
        return round(self.net_profit_rub / self.buy_total_rub * (365 / days) * 100, 1)
