"""Сигналы спроса из интернета и статистики площадок.

Задача — корректировать оценку спроса (demand_per_day) актуальными данными:
рост популярности игры, сезонность, всплески.

По умолчанию работает в offline-режиме на детерминированной эвристике,
чтобы не зависеть от внешних API. Для боевого режима сюда подключаются
реальные коннекторы (Google Trends, статистика площадок) — точка расширения
`_fetch_live_trend`.
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import dataclass

from src.config import get_learning_config

logger = logging.getLogger("arb-scanner.web_intel")

_CACHE: dict[str, tuple[float, float]] = {}  # game -> (trend_index, ts)
_TTL_SEC = 60 * 30


@dataclass
class TrendSignal:
    game: str
    trend_index: float   # 1.0 = норма, >1 рост спроса, <1 спад
    source: str
    note: str


def _deterministic_trend(game: str) -> float:
    """Стабильный псевдо-тренд из хэша игры + суточной фазы.

    Даёт правдоподобную вариативность без внешних запросов: одни игры
    «на подъёме», другие «остывают», плюс мягкая суточная волна.
    """
    h = int(hashlib.sha1(game.encode("utf-8")).hexdigest(), 16)
    base = 0.75 + (h % 60) / 100.0          # 0.75..1.35 — «структурный» интерес
    day = (time.time() // 86400)
    phase = math.sin((h % 7 + day / 7.0) * 2 * math.pi / 7.0)  # недельная волна
    return round(max(0.5, base + phase * 0.15), 3)


def _fetch_live_trend(game: str) -> float | None:
    """Точка расширения для реальных источников (Trends/статистика площадок).

    Верни индекс спроса (float) или None, если данных нет. По умолчанию
    коннектор не настроен — используется offline-эвристика.
    """
    return None


def get_trend(game: str) -> TrendSignal:
    cfg = get_learning_config().get("web_intel", {})
    source = str(cfg.get("source", "offline"))

    now = time.time()
    cached = _CACHE.get(game)
    if cached and now - cached[1] < _TTL_SEC:
        return TrendSignal(game, cached[0], source, "cache")

    idx = None
    note = "offline-эвристика"
    if source == "live" and cfg.get("enabled", True):
        try:
            idx = _fetch_live_trend(game)
            if idx is not None:
                note = "live-источник"
        except Exception:
            logger.exception("web_intel live fetch failed for %s", game)
            idx = None

    if idx is None:
        idx = _deterministic_trend(game)

    _CACHE[game] = (idx, now)
    return TrendSignal(game, idx, source, note)


def adjust_demand(game: str, base_demand: float) -> tuple[float, TrendSignal]:
    """Корректирует базовый спрос с учётом тренда и веса из конфига."""
    cfg = get_learning_config().get("web_intel", {})
    if not cfg.get("enabled", True):
        return base_demand, TrendSignal(game, 1.0, "disabled", "web_intel выкл")
    weight = float(cfg.get("trend_weight", 0.35))
    sig = get_trend(game)
    factor = 1.0 + (sig.trend_index - 1.0) * weight
    return round(max(0.01, base_demand * factor), 4), sig
