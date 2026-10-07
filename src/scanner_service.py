"""Единый сервис сканирования: сбор лотов, поиск арбитража, сохранение."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from src.config import ROOT, env, yaml_config
from src.engine.listing_filter import filter_goods_listings
from src.engine.scanner import scan_opportunities
from src.models import ArbitrageOpportunity, Platform
from src.parsers.funpay import FunPayParser
from src.parsers.lolz import LolzParser
from src.parsers.playerok import PlayerOKParser

logger = logging.getLogger("arb-scanner.service")
DATA_DIR = ROOT / "data"


def collect_listings() -> dict[Platform, list]:
    funpay = FunPayParser()
    lolz = LolzParser()
    playerok = PlayerOKParser()

    all_listings: dict[Platform, list] = {
        Platform.FUNPAY: [],
        Platform.LOLZ: [],
        Platform.PLAYEROK: [],
    }

    for item in yaml_config.get("watch_categories", []):
        game = item.get("game", "")
        category = item.get("category", "accounts")

        if item.get("funpay_lot_path"):
            rows = funpay.fetch_listings(game, category, lot_path=item["funpay_lot_path"])
            all_listings[Platform.FUNPAY].extend(
                filter_goods_listings(
                    rows,
                    category_hint=category,
                    forced_segment=item.get("funpay_segment"),
                )
            )
        if item.get("lolz_category") or item.get("lolz_category_id"):
            kwargs = {}
            if item.get("lolz_category"):
                kwargs["category_name"] = item["lolz_category"]
            if item.get("lolz_category_id") is not None:
                kwargs["category_id"] = int(item["lolz_category_id"])
            rows = lolz.fetch_listings(game, category, **kwargs)
            all_listings[Platform.LOLZ].extend(
                filter_goods_listings(
                    rows,
                    category_hint=category,
                    forced_segment=item.get("lolz_segment"),
                )
            )
        if item.get("playerok_category_url"):
            rows = playerok.fetch_listings(game, category, category_url=item["playerok_category_url"])
            all_listings[Platform.PLAYEROK].extend(
                filter_goods_listings(
                    rows,
                    category_hint=category,
                    forced_segment=item.get("playerok_segment"),
                )
            )

    # повторный фильтр уже не нужен — сегменты проставлены выше
    before = {p: len(v) for p, v in all_listings.items()}
    logger.info(
        "Listings after goods/segment filter: funpay=%d lolz=%d playerok=%d",
        before.get(Platform.FUNPAY, 0),
        before.get(Platform.LOLZ, 0),
        before.get(Platform.PLAYEROK, 0),
    )

    return all_listings


def save_results(opportunities: list[ArbitrageOpportunity]) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    payload = [o.model_dump(mode="json") for o in opportunities]
    out.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    _prune_old_scans(keep=30)
    return out


def _prune_old_scans(keep: int = 30) -> None:
    scans = sorted(DATA_DIR.glob("scan_*.json"), reverse=True)
    for old in scans[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def run_scan() -> tuple[list[ArbitrageOpportunity], dict[Platform, int], Path | None]:
    """Выполняет полный цикл сканирования. Возвращает сделки, счётчики лотов, путь к файлу."""
    listings = collect_listings()
    counts = {p: len(items) for p, items in listings.items()}
    total_listings = sum(counts.values())

    logger.info(
        "Scan: funpay=%d lolz=%d playerok=%d",
        counts.get(Platform.FUNPAY, 0),
        counts.get(Platform.LOLZ, 0),
        counts.get(Platform.PLAYEROK, 0),
    )

    opportunities = scan_opportunities(listings)
    path = None
    if opportunities:
        path = save_results(opportunities)
        logger.info("Saved %d opportunities to %s", len(opportunities), path.name)
    else:
        logger.warning("No opportunities found (listings total: %d)", total_listings)

    return opportunities, counts, path
