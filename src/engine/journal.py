"""Журнал реальных позиций: купил → продал → PnL.

Пользователь отмечает купленные лоты и фиксирует продажи.
Мозг использует эти факты вместо (или поверх) симуляции при эволюции стратегий.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from src.config import LEARNING_DIR

logger = logging.getLogger("arb-scanner.journal")

TRADES_PATH = LEARNING_DIR / "trades.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TradeJournal:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.trades: list[dict] = []
        LEARNING_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if TRADES_PATH.exists():
            try:
                data = json.loads(TRADES_PATH.read_text(encoding="utf-8"))
                self.trades = data if isinstance(data, list) else data.get("trades", [])
            except (json.JSONDecodeError, OSError):
                self.trades = []

    def _persist(self) -> None:
        TRADES_PATH.write_text(
            json.dumps(self.trades, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_trades(self, status: str = "all", limit: int = 200) -> list[dict]:
        with self._lock:
            rows = list(self.trades)
        if status != "all":
            rows = [t for t in rows if t.get("status") == status]
        return rows[:limit]

    def get(self, trade_id: str) -> Optional[dict]:
        with self._lock:
            for t in self.trades:
                if t.get("id") == trade_id:
                    return dict(t)
        return None

    def mark_bought(self, payload: dict) -> dict:
        """Открыть позицию (купил лот)."""
        title = str(payload.get("title") or "").strip()
        buy_price = float(payload.get("buy_price") or payload.get("price") or 0)
        if buy_price <= 0:
            return {"ok": False, "error": "укажи цену покупки"}

        buy_total = float(payload.get("buy_total") or buy_price)
        trade = {
            "id": str(uuid.uuid4())[:8],
            "status": "open",
            "title": title or "Лот без названия",
            "game": str(payload.get("game") or "unknown"),
            "buy_platform": str(payload.get("buy_platform") or "lolz"),
            "sell_platform": str(payload.get("sell_platform") or "funpay"),
            "buy_price": round(buy_price, 2),
            "buy_total": round(buy_total, 2),
            "buy_url": str(payload.get("buy_url") or payload.get("url") or ""),
            "sell_url": str(payload.get("sell_url") or ""),
            "margin_at_buy": float(payload.get("margin") or payload.get("margin_percent") or 0),
            "risk_at_buy": float(payload.get("risk") or payload.get("risk_score") or 0),
            "days_est": float(payload.get("days") or payload.get("est_days_to_sell") or 0),
            "liq": str(payload.get("liq") or payload.get("liquidity_label") or "unknown"),
            "health": str(payload.get("health") or payload.get("market_health") or "unknown"),
            "ev_at_buy": float(payload.get("ev") or payload.get("expected_value_rub") or 0),
            "net_profit_est": float(payload.get("net_profit") or payload.get("net_profit_rub") or 0),
            "item_id": str(payload.get("item_id") or payload.get("external_id") or ""),
            "buyer_nick": "",
            "sell_price": None,
            "sell_fees": 0.0,
            "realized_profit": None,
            "roi_percent": None,
            "bought_at": str(payload.get("bought_at") or _now_iso()),
            "sold_at": None,
            "notes": str(payload.get("notes") or ""),
            "source": str(payload.get("source") or "manual"),
        }
        with self._lock:
            self.trades.insert(0, trade)
            self.trades = self.trades[:500]
            self._persist()
        return {"ok": True, "trade": trade}

    def record_sold(self, trade_id: str, payload: dict) -> dict:
        """Закрыть позицию: продал → PnL."""
        sell_price = float(payload.get("sell_price") or payload.get("price") or 0)
        if sell_price <= 0:
            return {"ok": False, "error": "укажи цену продажи"}
        sell_fees = float(payload.get("sell_fees") or payload.get("fees") or 0)
        nick = str(payload.get("buyer_nick") or payload.get("nick") or "").strip()

        with self._lock:
            trade = None
            for t in self.trades:
                if t.get("id") == trade_id:
                    trade = t
                    break
            if not trade:
                return {"ok": False, "error": "сделка не найдена"}
            if trade.get("status") == "sold":
                return {"ok": False, "error": "уже продана", "trade": trade}

            buy_total = float(trade.get("buy_total") or trade.get("buy_price") or 0)
            net = round(sell_price - sell_fees - buy_total, 2)
            roi = round(net / buy_total * 100, 2) if buy_total else 0.0
            trade["status"] = "sold"
            trade["sell_price"] = round(sell_price, 2)
            trade["sell_fees"] = round(sell_fees, 2)
            trade["realized_profit"] = net
            trade["roi_percent"] = roi
            trade["buyer_nick"] = nick
            trade["sold_at"] = str(payload.get("sold_at") or _now_iso())
            if payload.get("notes"):
                trade["notes"] = str(payload["notes"])
            self._persist()
            result = dict(trade)

        return {"ok": True, "trade": result}

    def mark_lost(self, trade_id: str, payload: dict | None = None) -> dict:
        """Списать позицию (не продалась / откат) — отрицательный PnL."""
        payload = payload or {}
        loss = float(payload.get("loss") or 0)
        with self._lock:
            trade = None
            for t in self.trades:
                if t.get("id") == trade_id:
                    trade = t
                    break
            if not trade:
                return {"ok": False, "error": "сделка не найдена"}
            buy_total = float(trade.get("buy_total") or 0)
            # по умолчанию −12% от закупа (как в симуляции мозга) или явная сумма
            realized = -abs(loss) if loss else -round(buy_total * 0.12, 2)
            trade["status"] = "lost"
            trade["sell_price"] = 0.0
            trade["sell_fees"] = 0.0
            trade["realized_profit"] = realized
            trade["roi_percent"] = round(realized / buy_total * 100, 2) if buy_total else 0.0
            trade["sold_at"] = _now_iso()
            trade["notes"] = str(payload.get("notes") or trade.get("notes") or "списано")
            self._persist()
            return {"ok": True, "trade": dict(trade)}

    def delete(self, trade_id: str) -> dict:
        with self._lock:
            before = len(self.trades)
            self.trades = [t for t in self.trades if t.get("id") != trade_id]
            if len(self.trades) == before:
                return {"ok": False, "error": "не найдена"}
            self._persist()
            return {"ok": True}

    def summary(self) -> dict:
        with self._lock:
            rows = list(self.trades)
        open_t = [t for t in rows if t.get("status") == "open"]
        closed = [t for t in rows if t.get("status") in {"sold", "lost"}]
        profits = [float(t.get("realized_profit") or 0) for t in closed]
        invested_open = sum(float(t.get("buy_total") or 0) for t in open_t)
        invested_closed = sum(float(t.get("buy_total") or 0) for t in closed) or 1.0
        total_pnl = sum(profits)
        wins = sum(1 for p in profits if p > 0)
        return {
            "total": len(rows),
            "open": len(open_t),
            "sold": sum(1 for t in rows if t.get("status") == "sold"),
            "lost": sum(1 for t in rows if t.get("status") == "lost"),
            "capital_open": round(invested_open, 2),
            "total_realized_pnl": round(total_pnl, 2),
            "avg_realized_pnl": round(sum(profits) / len(profits), 1) if profits else 0.0,
            "winrate": round(wins / len(profits), 3) if profits else 0.0,
            "roi_closed_percent": round(total_pnl / invested_closed * 100, 2) if closed else 0.0,
            "forecast_from_open": round(sum(float(t.get("net_profit_est") or 0) for t in open_t), 2),
        }

    def closed_as_samples(self) -> list[dict]:
        """Закрытые сделки → формат samples для мозга."""
        with self._lock:
            closed = [t for t in self.trades if t.get("status") in {"sold", "lost"}]
        out = []
        for t in closed:
            out.append({
                "ts": t.get("sold_at") or t.get("bought_at"),
                "scan": 0,
                "source": "real",
                "game": t.get("game", "unknown"),
                "buy_total": float(t.get("buy_total") or 0),
                "margin": float(t.get("margin_at_buy") or 0),
                "risk": float(t.get("risk_at_buy") or 0),
                "days": float(t.get("days_est") or 0),
                "liq": t.get("liq", "unknown"),
                "health": t.get("health", "unknown"),
                "sold": t.get("status") == "sold" and float(t.get("realized_profit") or 0) > 0,
                "p_sold": 1.0 if t.get("status") == "sold" else 0.0,
                "realized_profit": float(t.get("realized_profit") or 0),
                "realized_days": 0,
                "real": True,
                "trade_id": t.get("id"),
            })
        return out


journal = TradeJournal()
