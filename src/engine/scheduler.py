"""Фоновый планировщик: автоматическое сканирование каждые N минут."""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from src.config import env
from src.sample_data import generate_sample_opportunities
from src.scanner_service import run_scan
from src.engine.autobuy import autobuy_service
from src.engine.brain import brain

logger = logging.getLogger("arb-scanner.scheduler")


@dataclass
class ScanState:
    running: bool = False
    last_scan_at: Optional[str] = None
    next_scan_at: Optional[str] = None
    last_count: int = 0
    listings: dict = field(default_factory=dict)
    source: str = "offline"       # live | offline
    scan_version: int = 0
    last_error: Optional[str] = None
    interval_minutes: int = 15
    scan_in_progress: bool = False


class ScanScheduler:
    def __init__(self, interval_minutes: int | None = None) -> None:
        self.interval = interval_minutes or env.scan_interval_minutes or 15
        self.state = ScanState(interval_minutes=self.interval)
        self._cache: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._on_update: list[Callable[[], None]] = []

    def get_cache(self) -> list[dict]:
        with self._lock:
            return list(self._cache)

    def subscribe(self, callback: Callable[[], None]) -> None:
        self._on_update.append(callback)

    def _notify(self) -> None:
        for cb in self._on_update:
            try:
                cb()
            except Exception:
                pass

    def execute_scan(self, use_offline_fallback: bool = True) -> None:
        """Один цикл сканирования (синхронный, вызывается из потока)."""
        with self._lock:
            self.state.scan_in_progress = True

        try:
            opportunities, counts, _path = run_scan()
            payload = [o.model_dump(mode="json") for o in opportunities]
            has_live_listings = sum(counts.values()) > 0
            source = "live"

            # Демо только если API вообще ничего не отдал (нет токена / парсеры пустые)
            if not payload and use_offline_fallback and not has_live_listings:
                payload = [o.model_dump(mode="json") for o in generate_sample_opportunities()]
                source = "offline"
                logger.info("Live scan empty — using offline sample data for UI")
            elif not payload:
                logger.info(
                    "Live listings present (%s) but no arb matches — keeping LIVE empty deals",
                    {p.value: c for p, c in counts.items()},
                )

            with self._lock:
                self._cache = payload
                self.state.last_count = len(payload)
                self.state.listings = {p.value: c for p, c in counts.items()}
                self.state.source = source
                self.state.last_scan_at = datetime.now().isoformat(timespec="seconds")
                self.state.next_scan_at = (
                    datetime.now() + timedelta(minutes=self.interval)
                ).isoformat(timespec="seconds")
                self.state.scan_version += 1
                self.state.last_error = None
                self.state.running = True

            logger.info("Scan complete: %d deals, source=%s", len(payload), source)
            self._notify()
            threading.Thread(
                target=self._run_learning,
                args=(payload, source),
                daemon=True,
                name="brain-learn",
            ).start()
            try:
                autobuy_service.evaluate_and_execute(payload, source=source)
            except Exception:
                logger.exception("AutoBuy failed")

        except Exception as exc:
            logger.exception("Scan failed")
            with self._lock:
                self.state.last_error = str(exc)
                if not self._cache and use_offline_fallback:
                    self._cache = [o.model_dump(mode="json") for o in generate_sample_opportunities()]
                    self.state.source = "offline"
                    self.state.last_count = len(self._cache)
                    self.state.scan_version += 1
        finally:
            with self._lock:
                self.state.scan_in_progress = False

    def _run_learning(self, payload: list[dict], source: str) -> None:
        try:
            brain.record_scan(payload, source=source)
        except Exception:
            logger.exception("Brain learning failed")

    def _loop(self) -> None:
        logger.info("Scheduler started: every %d min", self.interval)
        self.execute_scan()
        while not self._stop.wait(timeout=self.interval * 60):
            self.execute_scan()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scan-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            self.state.running = False

    def trigger_now(self) -> None:
        """Ручной запуск скана в фоне."""
        threading.Thread(target=self.execute_scan, daemon=True, name="scan-manual").start()


# Глобальный экземпляр для API
scheduler = ScanScheduler(interval_minutes=15)
