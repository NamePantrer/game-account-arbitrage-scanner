"""Клиенты и персональные предложения со скидкой.

Клиент — это человек, который у нас уже хоть раз что-то купил. Чем больше
покупок, тем клиент ценнее (segment: new → returning → loyal → vip).

ВАЖНО про источник данных:
  • Никаких выдуманных клиентов. База наполняется ТОЛЬКО реальными продажами
    (record_sale) или ручным импортом (import_clients). Если продаж не было —
    база пуста, и интерфейс так и пишет: «клиентов пока нет».
  • Продажи можно фиксировать вручную (кнопка/эндпоинт), импортом CSV, либо
    подключив реальный источник (кабинет Lolz/FunPay/PlayerOK) — точка
    расширения `record_sale`.

Про рассылку офферов (этика/ToS):
  • Движок готовит ЧЕРНОВИКИ. Авто-отправка выключена (auto_send=false) и
    ограничена дневным лимитом. Реальная доставка в чат площадки — `_deliver`.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import threading
from datetime import datetime, timezone

from src.config import LEARNING_DIR, get_learning_config

logger = logging.getLogger("arb-scanner.clients")

CLIENTS_PATH = LEARNING_DIR / "clients.json"
OFFERS_PATH = LEARNING_DIR / "client_offers.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clients_cfg() -> dict:
    return get_learning_config().get("clients", {}) or {}


def _days_since(iso: str | None) -> int:
    if not iso:
        return 9999
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except ValueError:
        return 9999


class ClientStore:
    """Постоянная база реальных покупателей (только по фактам продаж)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.clients: dict[str, dict] = {}
        LEARNING_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if CLIENTS_PATH.exists():
            try:
                self.clients = json.loads(CLIENTS_PATH.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                self.clients = {}

    def _persist(self) -> None:
        CLIENTS_PATH.write_text(
            json.dumps(self.clients, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _key(nick: str, platform: str = "") -> str:
        return f"{platform}:{nick}".strip(":").lower()

    def record_sale(self, nick: str, *, game: str = "", price: float = 0.0,
                    platform: str = "", at: str | None = None) -> dict:
        """Фиксирует факт продажи клиенту. Наполняет/обновляет базу."""
        nick = (nick or "").strip()
        if not nick:
            return {"ok": False, "error": "nick обязателен"}
        key = self._key(nick, platform)
        ts = at or _now_iso()
        with self._lock:
            c = self.clients.get(key)
            if c is None:
                c = {
                    "id": key, "nick": nick, "platform": platform,
                    "purchases": 0, "total_spent": 0.0, "games": {},
                    "first_seen": ts, "last_seen": ts,
                }
                self.clients[key] = c
            c["purchases"] += 1
            c["total_spent"] = round(c["total_spent"] + float(price or 0), 2)
            c["last_seen"] = ts
            if game:
                c["games"][game] = c["games"].get(game, 0) + 1
            self._persist()
            return {"ok": True, "client": self._enrich(c)}

    def import_clients(self, rows: list[dict]) -> dict:
        """Массовый импорт (например, из CSV кабинета). rows: nick, game, price, purchases?"""
        added = 0
        for r in rows:
            nick = str(r.get("nick") or r.get("buyer") or r.get("name") or "").strip()
            if not nick:
                continue
            times = int(r.get("purchases") or 1)
            price = float(r.get("price") or r.get("avg_price") or 0)
            game = str(r.get("game") or "")
            platform = str(r.get("platform") or "")
            for _ in range(max(1, times)):
                self.record_sale(nick, game=game, price=price, platform=platform)
            added += 1
        return {"ok": True, "imported": added, "total": len(self.clients)}

    @staticmethod
    def _segment(purchases: int) -> str:
        if purchases >= 10:
            return "vip"
        if purchases >= 4:
            return "loyal"
        if purchases >= 2:
            return "returning"
        return "new"

    @staticmethod
    def _value_score(c: dict) -> float:
        """Ценность клиента: в основном число покупок + сумма + свежесть."""
        purchases = c.get("purchases", 0)
        spent = c.get("total_spent", 0.0)
        recency = max(0.0, 1.0 - _days_since(c.get("last_seen")) / 90.0)
        # логарифмический вклад повторных покупок (10 покупок ≈ 1.0)
        vol = min(1.0, (purchases ** 0.7) / (10 ** 0.7))
        spend = min(1.0, spent / 30000.0)
        return round(vol * 0.6 + spend * 0.2 + recency * 0.2, 3)

    def _enrich(self, c: dict) -> dict:
        games = c.get("games", {})
        fav = sorted(games, key=games.get, reverse=True)[:2]
        purchases = c.get("purchases", 0)
        last_days = _days_since(c.get("last_seen"))
        active_min = int(_clients_cfg().get("active_min_deals", 2))
        return {
            "id": c["id"],
            "nick": c["nick"],
            "platform": c.get("platform", ""),
            "purchases": purchases,
            "total_spent": round(c.get("total_spent", 0.0), 2),
            "avg_price": round(c.get("total_spent", 0.0) / purchases, 0) if purchases else 0,
            "fav_games": fav,
            "last_seen_days": last_days,
            "segment": self._segment(purchases),
            "value": self._value_score(c),
            "is_active": purchases >= active_min and last_days <= 60,
        }

    def list_clients(self) -> list[dict]:
        with self._lock:
            enriched = [self._enrich(c) for c in self.clients.values()]
        enriched.sort(key=lambda x: x["value"], reverse=True)
        return enriched

    def clear(self) -> dict:
        with self._lock:
            n = len(self.clients)
            self.clients = {}
            self._persist()
        return {"ok": True, "removed": n}


client_store = ClientStore()


def analyze_clients(opportunities: list[dict] | None = None, source: str = "offline") -> list[dict]:
    """Возвращает реальную базу клиентов (пусто, если продаж ещё не было)."""
    return client_store.list_clients()


def _safe_discount(opp: dict, cap_percent: float, segment: str) -> float:
    """Скидка, при которой сделка остаётся прибыльной (отдаём макс. половину маржи).

    Ценным клиентам (loyal/vip) даём ближе к потолку, новым — скромнее.
    """
    sell = float(opp.get("sell_price_rub", 0) or 0)
    net = float(opp.get("net_profit_rub", 0) or 0)
    if sell <= 0 or net <= 0:
        return 0.0
    margin_room = (net / sell) * 100 * 0.5   # не больше половины маржи
    seg_cap = {"vip": 1.0, "loyal": 0.85, "returning": 0.65, "new": 0.4}.get(segment, 0.5)
    return round(min(cap_percent * seg_cap, margin_room), 1)


def _draft_message(client: dict, opp: dict, discount: float, new_price: float) -> str:
    title = opp.get("buy", {}).get("title", "аккаунт")
    game = opp.get("buy", {}).get("game", "")
    seg = client.get("segment")
    greet = {
        "vip": "как нашему постоянному клиенту",
        "loyal": "для постоянных покупателей",
        "returning": "для тебя как вернувшегося покупателя",
        "new": "специально для тебя",
    }.get(seg, "для тебя")
    return (
        f"Привет, {client['nick']}! "
        f"{'Вижу, тебе интересны ' + game + '. ' if game else ''}"
        f"Есть отличный вариант: «{title}». "
        f"Персональная скидка {discount:.0f}% {greet} — "
        f"цена {new_price:.0f}₽ вместо обычной. Забронировать?"
    )


def build_offers(opportunities: list[dict], source: str = "offline") -> dict:
    cfg = _clients_cfg()
    clients = analyze_clients()
    active = [c for c in clients if c["is_active"]]

    base = {
        "enabled": bool(cfg.get("enabled", True)),
        "auto_send": bool(cfg.get("auto_send", False)),
        "daily_limit": int(cfg.get("daily_offer_limit", 20)),
        "total_clients": len(clients),
        "active_clients": len(active),
        "offers": [],
        "clients": clients[:50],
    }

    if not cfg.get("enabled", True):
        base["enabled"] = False
        return base

    # Честное пустое состояние: клиентов нет — так и говорим.
    if not clients:
        base["empty_reason"] = (
            "Клиентов пока нет. Клиент появляется, когда кто-то у вас покупает. "
            "Зафиксируйте продажу вручную, импортируйте историю или подключите "
            "кабинет площадки — и база начнёт наполняться."
        )
        return base
    if not active:
        base["empty_reason"] = (
            "Активных клиентов нет. Как только появятся повторные покупатели, "
            "движок подберёт им персональные предложения."
        )
        return base

    # индексируем лоты по игре для подбора под интересы клиента
    by_game: dict[str, list[dict]] = {}
    for o in opportunities or []:
        if float(o.get("net_profit_rub", 0) or 0) <= 0:
            continue
        by_game.setdefault(o.get("buy", {}).get("game", ""), []).append(o)
    for g in by_game:
        by_game[g].sort(key=lambda x: x.get("net_profit_rub", 0), reverse=True)

    all_by_profit = sorted(
        (o for o in (opportunities or []) if float(o.get("net_profit_rub", 0) or 0) > 0),
        key=lambda x: x.get("net_profit_rub", 0), reverse=True,
    )

    offers = []
    for c in active:
        match = None
        for g in c["fav_games"]:
            if by_game.get(g):
                match = by_game[g][0]
                break
        if not match and all_by_profit:
            match = all_by_profit[0]  # интересов не знаем — берём топ по прибыли
        if not match:
            continue
        discount = _safe_discount(match, float(cfg.get("max_discount_percent", 12)), c["segment"])
        if discount < 1.0:
            continue
        sell = float(match.get("sell_price_rub", 0) or 0)
        new_price = round(sell * (1 - discount / 100), 0)
        net_after = round(float(match.get("net_profit_rub", 0)) - (sell - new_price), 0)
        offers.append({
            "id": f"offer-{c['id']}",
            "client": c["nick"],
            "client_id": c["id"],
            "segment": c["segment"],
            "value": c["value"],
            "purchases": c["purchases"],
            "game": match.get("buy", {}).get("game", ""),
            "lot": match.get("buy", {}).get("title", ""),
            "base_price": sell,
            "discount_percent": discount,
            "offer_price": new_price,
            "profit_after": net_after,
            "sell_url": match.get("sell_url"),
            "message": _draft_message(c, match, discount, new_price),
            "status": "draft",
        })
    offers.sort(key=lambda x: x["value"], reverse=True)
    base["offers"] = offers
    return base


class ClientHub:
    """Хранит журнал отправленных/черновых предложений и лимит в день."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.log: dict = {"sent": [], "day": None, "count": 0}
        LEARNING_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if OFFERS_PATH.exists():
            try:
                self.log = json.loads(OFFERS_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

    def _persist(self) -> None:
        OFFERS_PATH.write_text(json.dumps(self.log, ensure_ascii=False, indent=2), encoding="utf-8")

    def _deliver(self, offer: dict) -> bool:
        """Реальная доставка в чат площадки — точка интеграции.

        По умолчанию не подключена: только фиксируем в журнале. Подключение
        боевого чата должно соблюдать правила площадки и антиспам-лимиты.
        """
        logger.info("Offer prepared for %s: %s", offer.get("client"), offer.get("message"))
        return False  # не отправлено реально — только черновик/журнал

    def send_offer(self, offer: dict) -> dict:
        cfg = _clients_cfg()
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            if self.log.get("day") != today:
                self.log["day"] = today
                self.log["count"] = 0
            limit = int(cfg.get("daily_offer_limit", 20))
            if self.log["count"] >= limit:
                return {"status": "rate_limited", "limit": limit}

            auto = bool(cfg.get("auto_send", False))
            delivered = self._deliver(offer) if auto else False
            status = "sent" if delivered else ("queued" if auto else "draft_saved")
            record = {**offer, "status": status, "ts": _now_iso(),
                      "note": None if auto else "auto_send выключен — сохранён черновик"}
            self.log["sent"].append(record)
            self.log["count"] += 1
            self.log["sent"] = self.log["sent"][-200:]
            self._persist()
            return {"status": status, "count_today": self.log["count"], "limit": limit}


client_hub = ClientHub()


def import_clients_csv(text: str) -> dict:
    """Парсит CSV (заголовки: nick[,game][,price][,purchases][,platform])."""
    try:
        reader = csv.DictReader(io.StringIO(text))
        rows = [{k.strip().lower(): v for k, v in row.items()} for row in reader]
    except csv.Error as exc:
        return {"ok": False, "error": f"CSV parse: {exc}"}
    return client_store.import_clients(rows)
