"""Оффлайн-выборка сделок для интерфейса без API-токенов.

Использует тот же движок (estimate_arbitrage), что и боевой сканер.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from src.config import get_demand_per_day
from src.engine.market import compute_market_stats
from src.engine.pricing import analyze_prices
from src.engine.profit import estimate_arbitrage
from src.models import ArbitrageOpportunity, Listing, Platform

_GAMES = [
    ("brawl-stars", ["45 бравлеров, 12 лег", "ранг 30к, все скины", "старт аккаунт, 8 бравлеров"]),
    ("genshin-impact", ["AR58, 5 лег оружий", "AR45, Райден C0", "стартовый, Мона"]),
    ("steam", ["120 игр, CS2 prime", "аккаунт с Dota 2", "GTA V + RDR2"]),
    ("albion-online", ["T8 фарм, 20кк сереб", "мастер оружия, 5кк", "стартовый персонаж"]),
    ("pubg-mobile", ["Conqueror, 300 UC", "Ace ранг, скины", "новый аккаунт"]),
    ("clash-royale", ["9 арена, легендарки", "макс колода, 6к кубков", "стартовый"]),
    ("valorant", ["Immortal, 40 скинов", "Gold, ножи", "новый, без ранга"]),
]

_ORIGINS = (
    ["personal"] * 5 + ["autoreg"] * 4 + ["resale"] * 4
    + ["self_registration"] * 2 + ["unknown"] * 2 + ["stealer", "brute"]
)

_SELLERS = [
    "GameTrader", "AccShop", "ProSeller", "DigitalDealer", "TopMarket",
    "FastAccs", "EliteStore", "TrustSeller", "MarketKing", "AccGuru",
]


def _make_opportunity(seed_id: int) -> ArbitrageOpportunity:
    game_slug, titles = random.choice(_GAMES)
    title = random.choice(titles)

    buy_platform = Platform.LOLZ
    sell_platform = random.choice([Platform.FUNPAY, Platform.PLAYEROK])

    buy_price = round(random.uniform(150, 4000), 0)
    if random.random() < 0.12:
        margin_target = random.uniform(0.6, 1.3)
    else:
        margin_target = random.uniform(0.05, 0.45)
    sell_price = round(buy_price * (1 + margin_target), 0)

    match_score = round(random.uniform(55, 99), 1)
    origin = random.choice(_ORIGINS)

    demand = get_demand_per_day(game_slug)
    supply = random.randint(2, 60)
    competing_prices = [round(sell_price * random.uniform(0.65, 1.4), 0) for _ in range(supply)]
    # Несколько выбросов — как на реальном рынке
    if supply >= 8:
        competing_prices.append(round(sell_price * random.uniform(0.15, 0.45), 0))
        competing_prices.append(round(sell_price * random.uniform(1.8, 3.5), 0))

    buy_market = [round(buy_price * random.uniform(0.7, 1.35), 0) for _ in range(random.randint(3, 40))]
    buy_market.append(buy_price)
    if random.random() < 0.2:
        buy_market.append(round(buy_price * random.uniform(0.2, 0.5), 0))

    market = compute_market_stats(game_slug, "accounts", competing_prices)
    sell_analysis = analyze_prices(competing_prices)
    buy_analysis = analyze_prices(buy_market)
    sell_target = sell_analysis.optimal_sell if sell_analysis.fair_count >= 3 else sell_price

    buy_seller = random.choice(_SELLERS) + str(random.randint(1, 99))
    buy_item_id = random.randint(100000, 999999)
    buy = Listing(
        platform=buy_platform,
        external_id=f"sample-{seed_id}",
        title=title,
        description=title,
        price_rub=buy_price,
        url=f"https://lzt.market/{buy_item_id}/",
        game=game_slug,
        category="accounts",
        seller_name=buy_seller,
        seller_url=f"https://lzt.market/user/{buy_seller.lower()}/",
        seller_rating=round(random.uniform(4.0, 5.0), 1),
        seller_reviews=random.randint(1, 900),
        origin=origin,
        fetched_at=datetime.utcnow() - timedelta(minutes=random.randint(0, 240)),
    )

    sell_host = "funpay.com" if sell_platform == Platform.FUNPAY else "playerok.com"
    sell_seller = random.choice(_SELLERS) + str(random.randint(1, 99))
    sell_ref = Listing(
        platform=sell_platform,
        external_id=f"sellref-{seed_id}",
        title=title,
        description=title,
        price_rub=sell_price,
        url=f"https://{sell_host}/lots/offer?id={random.randint(100000, 999999)}",
        game=game_slug,
        category="accounts",
        seller_name=sell_seller,
        seller_url=f"https://{sell_host}/users/{random.randint(1000, 99999)}/",
    )

    return estimate_arbitrage(
        buy_listing=buy,
        sell_platform=sell_platform,
        sell_price_rub=sell_target,
        match_score=match_score,
        competing_prices=sell_analysis.fair_prices or competing_prices,
        demand_per_day=demand,
        market_prices=buy_analysis.fair_prices or buy_market,
        competition_index=market.competition_index,
        market_health=market.health,
        sell_listing=sell_ref,
        sell_listing_raw_price=sell_price,
        sell_price_analysis=sell_analysis,
        buy_price_analysis=buy_analysis,
    )


def generate_sample_opportunities(count: int = 80) -> list[ArbitrageOpportunity]:
    random.seed()
    items = [_make_opportunity(i) for i in range(count)]
    items = [o for o in items if o.net_profit_rub > 0]
    items.sort(key=lambda o: o.net_profit_rub, reverse=True)
    return items
