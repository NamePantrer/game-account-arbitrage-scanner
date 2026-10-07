"""Автопокупка выгодных лотов на Lolz Market после скана.

Цикл:
1. После live-скана отбираются лучшие сделки (профит / EV / риск).
2. Если баланса Lolz хватает — fast-buy через API.
3. Если не хватает — лот в очередь; докупится после ручного пополнения баланса.

FunPay / PlayerOK — без API покупки, только вручную.
Вывод средств — вручную; YooKassa не используется.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Optional

from src.config import ROOT, env, get_autobuy_config
from src.models import ArbitrageOpportunity
from src.payments.lolz_buyer import LolzBuyer

logger = logging.getLogger("arb-scanner.autobuy")

DATA_DIR = ROOT / "data"
CONFIG_PATH = DATA_DIR / "autobuy_runtime.json"
LOG_PATH = DATA_DIR / "autobuy_log.json"
QUEUE_PATH = DATA_DIR / "autobuy_queue.json"

WATCH_INTERVAL_SEC = 30


@dataclass
class AutoBuySettings:
    enabled: bool = False
    dry_run: bool = True
    max_per_deal_rub: float = 5000.0
    max_daily_rub: float = 20000.0
    min_profit_rub: float = 100.0
    min_ev_rub: float = 50.0
    max_risk_score: float = 40.0
    max_deals_per_scan: int = 1
    topup_buffer_rub: float = 50.0
    allowed_buy_platforms: list[str] = field(default_factory=lambda: ["lolz"])


@dataclass
class AutoBuyState:
    enabled: bool = False
    dry_run: bool = True
    spent_today_rub: float = 0.0
    purchases_today: int = 0
    lolz_balance_rub: float = 0.0
    lolz_configured: bool = False
    queue_size: int = 0
    last_run_at: Optional[str] = None
    last_error: Optional[str] = None


class AutoBuyService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._settings = self._load_settings()
        self._state = AutoBuyState()
        self._history: list[dict] = self._load_json(LOG_PATH, [])
        self._queue: list[dict] = self._load_json(QUEUE_PATH, [])
        self._buyer = LolzBuyer()
        self._spent_date = date.today().isoformat()
        self._watch_stop = threading.Event()
        self._watch_thread: Optional[threading.Thread] = None

    @staticmethod
    def _load_json(path, default):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return default

    @staticmethod
    def _write_json(path, data) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_settings(self) -> AutoBuySettings:
        cfg = get_autobuy_config()
        runtime = self._load_json(CONFIG_PATH, {})
        merged = {**cfg, **runtime}
        return AutoBuySettings(
            enabled=bool(merged.get("enabled", env.autobuy_enabled)),
            dry_run=bool(merged.get("dry_run", env.autobuy_dry_run)),
            max_per_deal_rub=float(merged.get("max_per_deal_rub", 5000)),
            max_daily_rub=float(merged.get("max_daily_rub", 20000)),
            min_profit_rub=float(merged.get("min_profit_rub", 100)),
            min_ev_rub=float(merged.get("min_ev_rub", 50)),
            max_risk_score=float(merged.get("max_risk_score", 40)),
            max_deals_per_scan=int(merged.get("max_deals_per_scan", 1)),
            topup_buffer_rub=float(merged.get("topup_buffer_rub", 50)),
            allowed_buy_platforms=list(merged.get("allowed_buy_platforms", ["lolz"])),
        )

    def _save_settings(self) -> None:
        self._write_json(CONFIG_PATH, asdict(self._settings))

    def _save_queue(self) -> None:
        self._write_json(QUEUE_PATH, self._queue)

    def _append_log(self, entry: dict) -> None:
        entry["at"] = datetime.now().isoformat(timespec="seconds")
        self._history.insert(0, entry)
        self._history = self._history[:200]
        self._write_json(LOG_PATH, self._history)

    def _reset_daily_if_needed(self) -> None:
        today = date.today().isoformat()
        if today != self._spent_date:
            self._spent_date = today
            self._state.spent_today_rub = 0.0
            self._state.purchases_today = 0

    def get_settings(self) -> AutoBuySettings:
        with self._lock:
            return self._settings

    def update_settings(self, patch: dict) -> AutoBuySettings:
        with self._lock:
            for key, val in patch.items():
                if hasattr(self._settings, key):
                    setattr(self._settings, key, val)
            self._save_settings()
            return self._settings

    def get_status(self) -> dict:
        with self._lock:
            self._reset_daily_if_needed()
            s = self._settings
            self._state.enabled = s.enabled
            self._state.dry_run = s.dry_run
            self._state.lolz_configured = self._buyer.configured
            self._state.queue_size = len(self._queue)
            if self._buyer.configured:
                self._state.lolz_balance_rub = self._buyer.get_balance_rub()
            return {
                "settings": asdict(s),
                "state": asdict(self._state),
                "history_count": len(self._history),
            }

    def get_history(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return self._history[:limit]

    def get_queue(self) -> list[dict]:
        with self._lock:
            return list(self._queue)

    def _ensure_balance(self, need_rub: float, settings: AutoBuySettings) -> dict:
        balance = self._buyer.get_balance_rub()
        self._state.lolz_balance_rub = balance
        if balance >= need_rub:
            return {"ok": True, "balance": balance}
        return {
            "ok": False,
            "balance": balance,
            "error": (
                f"Недостаточно баланса Lolz ({balance:.0f}₽), нужно {need_rub:.0f}₽. "
                "Пополни баланс вручную — лот встанет в очередь."
            ),
        }

    def _enqueue(self, opp: ArbitrageOpportunity, reason: str) -> None:
        item_id = opp.buy.external_id
        if any(q.get("item_id") == item_id for q in self._queue):
            return
        self._queue.append({
            "item_id": item_id,
            "title": opp.buy.title,
            "game": opp.buy.game,
            "price": opp.buy.price_rub,
            "buy_total": opp.buy_total_rub,
            "net_profit": opp.net_profit_rub,
            "url": opp.buy.url,
            "reason": reason,
            "queued_at": datetime.now().isoformat(timespec="seconds"),
        })
        self._queue = self._queue[-50:]
        self._save_queue()

    def _dequeue(self, item_id: str) -> None:
        self._queue = [q for q in self._queue if q.get("item_id") != item_id]
        self._save_queue()

    def cancel_queued(self, item_id: str) -> dict:
        with self._lock:
            self._dequeue(item_id)
            return {"ok": True, "queue_size": len(self._queue)}

    def _execute_buy(self, item_id: str, price: float, meta: dict, *, dry_run: bool = False) -> dict:
        if dry_run:
            self._append_log({
                "status": "dry_run",
                "item_id": item_id,
                "title": meta.get("title"),
                "price": price,
                "net_profit": meta.get("net_profit"),
                "url": meta.get("url"),
            })
            return {"ok": True, "dry_run": True}

        result = self._buyer.fast_buy(item_id, price)
        if result.get("ok"):
            self._state.spent_today_rub += price
            self._state.purchases_today += 1
            self._append_log({
                "status": "purchased",
                "item_id": item_id,
                "title": meta.get("title"),
                "price": price,
                "net_profit": meta.get("net_profit"),
                "url": meta.get("url"),
            })
        else:
            self._append_log({
                "status": "failed",
                "item_id": item_id,
                "title": meta.get("title"),
                "price": price,
                "error": result.get("error"),
            })
            self._state.last_error = str(result.get("error"))
        return result

    def process_queue(self) -> int:
        with self._lock:
            if not (self._settings.enabled and not self._settings.dry_run):
                return 0
            if not self._buyer.configured or not self._queue:
                return 0

            self._reset_daily_if_needed()
            balance = self._buyer.get_balance_rub()
            self._state.lolz_balance_rub = balance
            bought = 0

            for q in list(self._queue):
                need = float(q.get("buy_total", q.get("price", 0)))
                if balance < need:
                    continue
                if self._state.spent_today_rub + need > self._settings.max_daily_rub:
                    break
                meta = {
                    "title": q.get("title"),
                    "game": q.get("game"),
                    "net_profit": q.get("net_profit"),
                    "url": q.get("url"),
                }
                res = self._execute_buy(q["item_id"], q["price"], meta)
                if res.get("ok"):
                    balance -= need
                    self._dequeue(q["item_id"])
                    bought += 1
            self._state.queue_size = len(self._queue)
            return bought

    def _watch_loop(self) -> None:
        while not self._watch_stop.wait(WATCH_INTERVAL_SEC):
            try:
                self.process_queue()
            except Exception:
                logger.exception("AutoBuy queue watcher error")

    def start(self) -> None:
        if self._watch_thread and self._watch_thread.is_alive():
            return
        self._watch_stop.clear()
        self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True, name="autobuy-watch")
        self._watch_thread.start()
        logger.info("AutoBuy queue watcher started")

    def stop(self) -> None:
        self._watch_stop.set()
        if self._watch_thread:
            self._watch_thread.join(timeout=3)

    def _opp_from_dict(self, raw: dict) -> Optional[ArbitrageOpportunity]:
        try:
            return ArbitrageOpportunity.model_validate(raw)
        except Exception:
            return None

    def _is_candidate(self, opp: ArbitrageOpportunity, settings: AutoBuySettings) -> bool:
        if opp.buy.platform.value not in settings.allowed_buy_platforms:
            return False
        if opp.net_profit_rub < settings.min_profit_rub:
            return False
        if opp.expected_value_rub < settings.min_ev_rub:
            return False
        if opp.risk_score > settings.max_risk_score:
            return False
        if opp.buy_total_rub > settings.max_per_deal_rub:
            return False
        if opp.confidence == "low":
            return False
        return opp.buy.external_id.isdigit()

    def evaluate_and_execute(self, opportunities: list[dict], source: str = "live") -> None:
        with self._lock:
            settings = self._settings
            if not settings.enabled:
                return
            if source != "live":
                self._state.last_error = "Автопокупка работает только на live-данных"
                return
            if not self._buyer.configured:
                self._state.last_error = "Задай LZT_API_TOKEN для автопокупок"
                return

            self._reset_daily_if_needed()
            self._state.last_run_at = datetime.now().isoformat(timespec="seconds")
            self._state.last_error = None
            self._state.lolz_balance_rub = self._buyer.get_balance_rub()

            models = [
                opp for raw in opportunities
                if (opp := self._opp_from_dict(raw)) and self._is_candidate(opp, settings)
            ]
            models.sort(key=lambda o: o.expected_value_rub, reverse=True)
            bought = 0

            for opp in models:
                if bought >= settings.max_deals_per_scan:
                    break
                if self._state.spent_today_rub + opp.buy_total_rub > settings.max_daily_rub:
                    break

                item_id = opp.buy.external_id
                price = opp.buy.price_rub
                meta = {
                    "item_id": item_id,
                    "game": opp.buy.game,
                    "title": opp.buy.title[:80],
                    "net_profit": opp.net_profit_rub,
                    "url": opp.buy.url,
                }

                if settings.dry_run:
                    self._execute_buy(item_id, price, meta, dry_run=True)
                    bought += 1
                    continue

                bal = self._ensure_balance(opp.buy_total_rub, settings)
                if not bal.get("ok"):
                    self._enqueue(opp, reason=bal.get("error", "нет средств"))
                    self._append_log({
                        "status": "queued",
                        "item_id": item_id,
                        "title": opp.buy.title,
                        "price": price,
                        "error": bal.get("error"),
                    })
                    self._state.last_error = bal.get("error")
                    continue

                res = self._execute_buy(item_id, price, meta)
                if res.get("ok"):
                    bought += 1

            self._state.queue_size = len(self._queue)
            logger.info("AutoBuy: candidates=%d executed=%d queued=%d", len(models), bought, len(self._queue))


autobuy_service = AutoBuyService()
