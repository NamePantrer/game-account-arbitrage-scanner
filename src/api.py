"""FastAPI-бэкенд для неонового дашборда арбитража."""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Optional

from fastapi import Body, FastAPI, Form, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.config import ROOT, env, get_portfolio_config
from src.sample_data import generate_sample_opportunities
from src.engine.advisor import build_advisor_plan
from src.engine.autobuy import autobuy_service
from src.engine.brain import brain
from src.engine.journal import journal
from src.engine.self_improve import self_improver
from src.engine.clients import (
    analyze_clients,
    build_offers,
    client_hub,
    client_store,
    import_clients_csv,
)
from src.engine.scheduler import scheduler
from src.engine.strategies import STRATEGY_TITLES, build_portfolio
from src.models import ArbitrageOpportunity

DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Запуск фонового сканера и watcher автопокупок при старте сервера."""
    scheduler.start()
    autobuy_service.start()
    yield
    autobuy_service.stop()
    scheduler.stop()


app = FastAPI(title="Arb Scanner Dashboard", version="2.3.0", lifespan=lifespan)

_offline_cache: list[dict] | None = None
_stats_cache: tuple[tuple[int, bool], dict] | None = None

# Пути, доступные без авторизации
_PUBLIC_PATHS = {"/login", "/api/login", "/favicon.ico", "/favicon.png"}


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Защита дашборда PIN-кодом. Включается, только если задан DASHBOARD_TOKEN.
    """
    token = env.dashboard_token
    if token:
        path = request.url.path
        is_public = path in _PUBLIC_PATHS or path.startswith("/static/")
        if not is_public:
            provided = request.cookies.get("arb_token") or request.headers.get("X-Auth-Token")
            if provided != token:
                if path.startswith("/api/"):
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return RedirectResponse("/login", status_code=302)
    return await call_next(request)


def _load_opportunities(use_offline: bool = False, refresh: bool = False, demo: bool = False) -> list[dict]:
    """Приоритет: кэш планировщика → файл на диске → оффлайн-выборка."""
    global _offline_cache
    use_offline = use_offline or demo  # demo — устаревший alias

    live = scheduler.get_cache()
    if live and not use_offline:
        return live

    if not use_offline and DATA_DIR.exists():
        scans = sorted(DATA_DIR.glob("scan_*.json"), reverse=True)
        if scans:
            try:
                raw = json.loads(scans[0].read_text(encoding="utf-8"))
                if raw:
                    return raw
            except (json.JSONDecodeError, OSError):
                pass

    if refresh or _offline_cache is None:
        _offline_cache = [o.model_dump(mode="json") for o in generate_sample_opportunities()]
    return _offline_cache


def _as_models(data: list[dict]) -> list[ArbitrageOpportunity]:
    models: list[ArbitrageOpportunity] = []
    for d in data:
        try:
            models.append(ArbitrageOpportunity.model_validate(d))
        except Exception:
            continue
    return models


def _matches_filters(
    opp: dict,
    game: Optional[str],
    confidence: Optional[str],
    sell_platform: Optional[str],
    min_profit: float,
    min_margin: float,
    liquidity: Optional[str],
    max_days: float,
    risk: Optional[str],
    max_risk: float,
    strategy: Optional[str],
) -> bool:
    if game and game != "all" and opp["buy"]["game"] != game:
        return False
    if confidence and confidence != "all" and opp["confidence"] != confidence:
        return False
    if sell_platform and sell_platform != "all" and opp["sell_platform"] != sell_platform:
        return False
    if opp["net_profit_rub"] < min_profit:
        return False
    if opp["margin_percent"] < min_margin:
        return False
    if liquidity and liquidity != "all" and opp.get("liquidity_label") != liquidity:
        return False
    if max_days > 0 and opp.get("est_days_to_sell", 0) > max_days:
        return False
    if risk and risk != "all" and opp.get("risk_label") != risk:
        return False
    if max_risk > 0 and opp.get("risk_score", 0) > max_risk:
        return False
    if strategy and strategy != "all" and strategy not in opp.get("strategies", []):
        return False
    return True


@app.get("/api/scan/status")
def scan_status() -> JSONResponse:
    s = scheduler.state
    return JSONResponse({
        "running": s.running,
        "scan_in_progress": s.scan_in_progress,
        "last_scan_at": s.last_scan_at,
        "next_scan_at": s.next_scan_at,
        "last_count": s.last_count,
        "listings": s.listings,
        "source": s.source,
        "scan_version": s.scan_version,
        "interval_minutes": s.interval_minutes,
        "last_error": s.last_error,
    })


@app.get("/api/platforms/auth")
def platforms_auth() -> JSONResponse:
    """Статус доступов: Lolz token / FunPay golden_key / PlayerOK token."""
    from src.config import env as _env
    from src.parsers.funpay import FunPayParser
    from src.parsers.playerok import PlayerOKParser

    lolz_ok = bool(_env.lzt_api_token)
    lolz_info: dict = {"configured": lolz_ok, "auth": lolz_ok}
    if lolz_ok:
        try:
            import requests as _rq
            r = _rq.get(
                "https://prod-api.lzt.market/me",
                headers={"Authorization": f"Bearer {_env.lzt_api_token}", "Accept": "application/json"},
                timeout=15,
            )
            if r.ok:
                u = (r.json() or {}).get("user") or {}
                lolz_info.update({
                    "auth": True,
                    "username": u.get("username") or u.get("display_username"),
                    "balance": u.get("balance"),
                })
            else:
                lolz_info.update({"auth": False, "error": f"HTTP {r.status_code}"})
        except Exception as exc:
            lolz_info.update({"auth": False, "error": str(exc)})

    funpay = FunPayParser()
    playerok = PlayerOKParser()
    return JSONResponse({
        "lolz": lolz_info,
        "funpay": {
            "configured": funpay.is_authenticated,
            "public_scan": True,
            **funpay.check_auth(),
        },
        "playerok": {
            "configured": playerok.is_authenticated,
            "public_scan": False,
            **playerok.check_auth(),
        },
        "how_to": {
            "funpay": "DevTools → Application → Cookies → funpay.com → golden_key → FUNPAY_GOLDEN_KEY в .env",
            "playerok": "DevTools → Application → Cookies → .playerok.com → token → PLAYEROK_TOKEN в .env",
        },
    })


@app.post("/api/scan/trigger")
def scan_trigger() -> JSONResponse:
    if scheduler.state.scan_in_progress:
        return JSONResponse({"status": "busy"})
    scheduler.trigger_now()
    return JSONResponse({"status": "started"})


@app.get("/api/refresh")
def refresh(demo: bool = Query(False), offline: bool = Query(False)) -> JSONResponse:
    if not demo and not offline:
        scheduler.trigger_now()
    else:
        _load_opportunities(offline=True, refresh=True)
    return JSONResponse({"status": "ok", "scan_version": scheduler.state.scan_version})


@app.get("/api/opportunities")
def get_opportunities(
    game: str = Query("all"),
    confidence: str = Query("all"),
    sell_platform: str = Query("all"),
    min_profit: float = Query(0),
    min_margin: float = Query(0),
    liquidity: str = Query("all"),
    max_days: float = Query(0),
    risk: str = Query("all"),
    max_risk: float = Query(0),
    strategy: str = Query("all"),
    demo: bool = Query(False),
    sort: str = Query("net_profit_rub"),
) -> JSONResponse:
    data = _load_opportunities(demo)
    filtered = [
        o for o in data
        if _matches_filters(
            o, game, confidence, sell_platform, min_profit, min_margin,
            liquidity, max_days, risk, max_risk, strategy,
        )
    ]
    simple_sort = {"net_profit_rub", "margin_percent", "match_score", "liquidity_score",
                   "risk_score", "expected_value_rub", "success_probability"}
    if sort == "buy":
        filtered.sort(key=lambda o: o["buy"]["price_rub"])
    elif sort == "daily_profit":
        filtered.sort(key=lambda o: o["net_profit_rub"] / max(o.get("est_days_to_sell", 1), 1), reverse=True)
    elif sort == "annualized_roi":
        filtered.sort(
            key=lambda o: (o["net_profit_rub"] / max(o["buy_total_rub"], 1)) * (365 / max(o.get("est_days_to_sell", 1), 1)),
            reverse=True,
        )
    elif sort == "est_days_to_sell":
        filtered.sort(key=lambda o: o.get("est_days_to_sell", 0))
    elif sort == "risk_score":
        filtered.sort(key=lambda o: o.get("risk_score", 0))
    elif sort in simple_sort:
        filtered.sort(key=lambda o: o.get(sort, 0), reverse=True)
    return JSONResponse(filtered)


@app.get("/api/stats")
def get_stats(demo: bool = Query(False)) -> JSONResponse:
    global _stats_cache
    cache_key = (scheduler.state.scan_version, demo)
    if _stats_cache and _stats_cache[0] == cache_key:
        return JSONResponse(_stats_cache[1])

    data = _load_opportunities(demo)
    total = len(data)
    if not total:
        return JSONResponse({"total_opportunities": 0})

    total_profit = round(sum(o["net_profit_rub"] for o in data), 2)
    # EV-заголовок отражает ДЕЙСТВЕННЫЕ сделки (положительный риск-скорр. EV) —
    # именно их рекомендует советник/портфель. Сумма по всем (включая
    # убыточные по риску) вводила бы в заблуждение.
    actionable = [o for o in data if o.get("expected_value_rub", 0) > 0]
    total_ev = round(sum(o["expected_value_rub"] for o in actionable), 2)
    ev_invest = round(sum(o.get("buy_total_rub", 0) for o in actionable), 2)
    total_fees = round(sum(o.get("total_fees_rub", 0) for o in data), 2)
    total_payout = round(sum(o.get("payout_fee_rub", 0) for o in data), 2)
    total_yookassa = round(
        sum(o.get("yookassa_charge_rub", 0) + o.get("yookassa_payout_rub", 0) for o in data), 2
    )
    avg_margin = round(sum(o["margin_percent"] for o in data) / total, 2)
    avg_risk = round(sum(o.get("risk_score", 0) for o in data) / total, 1)
    avg_success = round(sum(o.get("success_probability", 0) for o in data) / total * 100, 1)
    best = max(data, key=lambda o: o["net_profit_rub"])
    total_invest = round(sum(o["buy_total_rub"] for o in data), 2)
    avg_days = round(sum(o.get("est_days_to_sell", 0) for o in data) / total, 1)
    liquid_count = sum(1 for o in data if o.get("liquidity_label") in {"fast", "medium"})
    liquid_share = round(liquid_count / total * 100, 1)

    by_game: dict[str, dict] = defaultdict(lambda: {"count": 0, "profit": 0.0})
    by_conf: dict[str, int] = defaultdict(int)
    by_platform: dict[str, dict] = defaultdict(lambda: {"count": 0, "profit": 0.0})
    by_liquidity: dict[str, int] = defaultdict(int)
    by_risk: dict[str, int] = defaultdict(int)
    by_health: dict[str, int] = defaultdict(int)
    by_strategy: dict[str, int] = defaultdict(int)
    profit_buckets = {"0-100": 0, "100-300": 0, "300-700": 0, "700-1500": 0, "1500+": 0}
    fee_totals: dict[str, float] = defaultdict(float)

    for o in data:
        g = o["buy"]["game"]
        by_game[g]["count"] += 1
        by_game[g]["profit"] += o["net_profit_rub"]
        by_conf[o["confidence"]] += 1
        by_liquidity[o.get("liquidity_label", "unknown")] += 1
        by_risk[o.get("risk_label", "unknown")] += 1
        by_health[o.get("market_health", "unknown")] += 1
        for s in o.get("strategies", []):
            by_strategy[s] += 1

        route = f'{o["buy"]["platform"]} → {o["sell_platform"]}'
        by_platform[route]["count"] += 1
        by_platform[route]["profit"] += o["net_profit_rub"]

        for k, v in (o.get("fee_breakdown") or {}).items():
            if k != "Чистая прибыль":
                fee_totals[k] += v

        p = o["net_profit_rub"]
        if p < 100:
            profit_buckets["0-100"] += 1
        elif p < 300:
            profit_buckets["100-300"] += 1
        elif p < 700:
            profit_buckets["300-700"] += 1
        elif p < 1500:
            profit_buckets["700-1500"] += 1
        else:
            profit_buckets["1500+"] += 1

    payload = {
        "total_opportunities": total,
        "total_net_profit": total_profit,
        "total_expected_value": total_ev,
        "total_fees": total_fees,
        "total_payout_fees": total_payout,
        "total_yookassa_fees": total_yookassa,
        "avg_margin": avg_margin,
        "avg_risk": avg_risk,
        "avg_success_prob": avg_success,
        "best_profit": best["net_profit_rub"],
        "total_investment": total_invest,
        "roi_percent": round(total_profit / total_invest * 100, 2) if total_invest else 0.0,
        "ev_roi_percent": round(total_ev / ev_invest * 100, 2) if ev_invest else 0.0,
        "actionable_count": len(actionable),
        "avg_days_to_sell": avg_days,
        "liquid_share_percent": liquid_share,
        "by_game": {
            k: {"count": v["count"], "profit": round(v["profit"], 2)}
            for k, v in sorted(by_game.items(), key=lambda x: x[1]["profit"], reverse=True)
        },
        "by_confidence": dict(by_conf),
        "by_liquidity": dict(by_liquidity),
        "by_risk": dict(by_risk),
        "by_market_health": dict(by_health),
        "by_strategy": dict(by_strategy),
        "by_platform": {k: {"count": v["count"], "profit": round(v["profit"], 2)} for k, v in by_platform.items()},
        "profit_distribution": profit_buckets,
        "fee_totals": {k: round(v, 2) for k, v in fee_totals.items()},
        "scatter": [
            {"margin": o["margin_percent"], "profit": o["net_profit_rub"], "score": o["match_score"],
             "conf": o["confidence"], "days": o.get("est_days_to_sell", 0),
             "liq": o.get("liquidity_label", "unknown"),
             "title": o["buy"]["title"], "url": o["buy"].get("url"), "sell_url": o.get("sell_url")}
            for o in data
        ],
        "risk_reward": [
            {"risk": o.get("risk_score", 0), "profit": o["net_profit_rub"],
             "ev": o.get("expected_value_rub", 0), "risklabel": o.get("risk_label", "unknown"),
             "title": o["buy"]["title"], "game": o["buy"]["game"],
             "url": o["buy"].get("url"), "sell_url": o.get("sell_url")}
            for o in data
        ],
    }
    _stats_cache = (cache_key, payload)
    return JSONResponse(payload)


@app.get("/api/market")
def get_market(demo: bool = Query(False)) -> JSONResponse:
    data = _load_opportunities(demo)
    by_game: dict[str, dict] = defaultdict(
        lambda: {"buy_prices": [], "sell_prices": [], "optimal_sells": [], "segment_low": [],
                 "segment_high": [], "outliers": [], "margins": [], "risks": [],
                 "days": [], "comp": [], "health": [], "count": 0}
    )
    for o in data:
        g = o["buy"]["game"]
        b = by_game[g]
        b["buy_prices"].append(o["buy"]["price_rub"])
        b["sell_prices"].append(o["sell_price_rub"])
        b["optimal_sells"].append(o.get("optimal_sell_rub") or o["sell_price_rub"])
        b["segment_low"].append(o.get("price_segment_low") or 0)
        b["segment_high"].append(o.get("price_segment_high") or 0)
        b["outliers"].append(o.get("price_outliers_filtered") or 0)
        b["margins"].append(o["margin_percent"])
        b["risks"].append(o.get("risk_score", 0))
        b["days"].append(o.get("est_days_to_sell", 0))
        b["comp"].append(o.get("competition_index", 0))
        b["health"].append(o.get("market_health", "unknown"))
        b["count"] += 1

    def _med(xs):
        return round(statistics.median(xs), 2) if xs else 0.0

    def _avg(xs):
        return round(statistics.fmean(xs), 2) if xs else 0.0

    market = []
    for g, b in sorted(by_game.items(), key=lambda x: x[1]["count"], reverse=True):
        health = Counter(b["health"]).most_common(1)[0][0] if b["health"] else "unknown"
        market.append({
            "game": g,
            "deals": b["count"],
            "median_buy": _med(b["buy_prices"]),
            "median_sell": _med(b["sell_prices"]),
            "optimal_sell": _med(b["optimal_sells"]),
            "segment_low": _med([x for x in b["segment_low"] if x > 0]),
            "segment_high": _med([x for x in b["segment_high"] if x > 0]),
            "outliers_filtered": int(sum(b["outliers"])),
            "avg_margin": _avg(b["margins"]),
            "avg_risk": _avg(b["risks"]),
            "avg_days": _avg(b["days"]),
            "competition_index": _avg(b["comp"]),
            "health": health,
        })
    return JSONResponse(market)


@app.get("/api/strategies")
def get_strategies(demo: bool = Query(False)) -> JSONResponse:
    data = _load_opportunities(demo)
    agg: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "profit": 0.0, "ev": 0.0, "risk": [], "days": []}
    )
    for o in data:
        for s in o.get("strategies", []):
            a = agg[s]
            a["count"] += 1
            a["profit"] += o["net_profit_rub"]
            a["ev"] += o.get("expected_value_rub", 0)
            a["risk"].append(o.get("risk_score", 0))
            a["days"].append(o.get("est_days_to_sell", 0))

    out = []
    for key, a in sorted(agg.items(), key=lambda x: x[1]["ev"], reverse=True):
        out.append({
            "key": key,
            "title": STRATEGY_TITLES.get(key, key),
            "count": a["count"],
            "total_profit": round(a["profit"], 2),
            "total_ev": round(a["ev"], 2),
            "avg_risk": round(statistics.fmean(a["risk"]), 1) if a["risk"] else 0.0,
            "avg_days": round(statistics.fmean(a["days"]), 1) if a["days"] else 0.0,
        })
    return JSONResponse(out)


@app.get("/api/portfolio")
def get_portfolio(
    budget: float = Query(None),
    demo: bool = Query(False),
) -> JSONResponse:
    data = _load_opportunities(demo)
    models = _as_models(data)
    if budget is None:
        budget = float(get_portfolio_config().get("default_budget_rub", 20000))
    pf = build_portfolio(models, budget)
    return JSONResponse({
        "budget": pf.budget,
        "invested": pf.invested,
        "leftover": pf.leftover,
        "expected_profit": pf.expected_profit,
        "expected_value": pf.expected_value,
        "avg_risk": pf.avg_risk,
        "avg_days": pf.avg_days,
        "roi_percent": pf.roi_percent,
        "count": len(pf.items),
        "items": [
            {
                "title": it.opportunity.buy.title,
                "game": it.opportunity.buy.game,
                "route": f"{it.opportunity.buy.platform.value} → {it.opportunity.sell_platform.value}",
                "allocation": it.allocation_rub,
                "net_profit": it.opportunity.net_profit_rub,
                "expected_return": it.expected_return_rub,
                "risk_score": it.opportunity.risk_score,
                "risk_label": it.opportunity.risk_label,
                "days": it.opportunity.est_days_to_sell,
                "strategies": it.opportunity.strategies,
            }
            for it in pf.items
        ],
    })


@app.get("/api/advisor")
def get_advisor(
    budget: float = Query(2000),
    demo: bool = Query(False),
) -> JSONResponse:
    data = _load_opportunities(demo)
    models = _as_models(data)
    plan = build_advisor_plan(models, budget)
    return JSONResponse({
        "budget": plan.budget,
        "affordable_count": plan.affordable_count,
        "strategy_name": plan.strategy_name,
        "strategy_desc": plan.strategy_desc,
        "best_pick": asdict(plan.best_pick) if plan.best_pick else None,
        "picks": [asdict(p) for p in plan.picks],
        "steps": plan.steps,
        "growth": [asdict(g) for g in plan.growth],
        "projected_capital": plan.projected_capital,
        "projected_days": plan.projected_days,
        "projected_roi_percent": plan.projected_roi_percent,
        "warnings": plan.warnings,
    })


@app.get("/api/games")
def get_games(demo: bool = Query(False)) -> JSONResponse:
    data = _load_opportunities(demo=demo)
    games = sorted({o["buy"]["game"] for o in data})
    return JSONResponse(games)


@app.get("/api/autobuy/status")
def autobuy_status() -> JSONResponse:
    return JSONResponse(autobuy_service.get_status())


@app.post("/api/autobuy/settings")
def autobuy_settings(patch: dict = Body(...)) -> JSONResponse:
    allowed = {
        "enabled", "dry_run", "max_per_deal_rub", "max_daily_rub",
        "min_profit_rub", "min_ev_rub", "max_risk_score", "max_deals_per_scan",
        "topup_buffer_rub",
    }
    filtered = {k: v for k, v in patch.items() if k in allowed}
    settings = autobuy_service.update_settings(filtered)
    return JSONResponse({"settings": settings.__dict__, "status": autobuy_service.get_status()})


@app.get("/api/autobuy/history")
def autobuy_history(limit: int = Query(50, ge=1, le=200)) -> JSONResponse:
    return JSONResponse(autobuy_service.get_history(limit))


@app.get("/api/autobuy/queue")
def autobuy_queue() -> JSONResponse:
    return JSONResponse(autobuy_service.get_queue())


@app.post("/api/autobuy/queue/process")
def autobuy_queue_process() -> JSONResponse:
    bought = autobuy_service.process_queue()
    return JSONResponse({"bought": bought, "status": autobuy_service.get_status()})


@app.post("/api/autobuy/queue/cancel")
def autobuy_queue_cancel(payload: dict = Body(...)) -> JSONResponse:
    item_id = str(payload.get("item_id", ""))
    if not item_id:
        return JSONResponse({"ok": False, "error": "item_id обязателен"}, status_code=400)
    return JSONResponse(autobuy_service.cancel_queued(item_id))


@app.get("/api/brain/state")
def brain_state() -> JSONResponse:
    state = brain.get_state()
    state["forecast"] = brain.forecast_open_positions()
    return JSONResponse(state)


@app.post("/api/brain/evolve")
def brain_evolve() -> JSONResponse:
    return JSONResponse(brain.evolve())


@app.post("/api/brain/train")
def brain_train(rounds: int = Query(8, ge=1, le=50), demo: bool = Query(False)) -> JSONResponse:
    """Прогоняет текущие сделки как N сканов, чтобы быстро накопить обучение."""
    data = _load_opportunities(demo)
    source = "offline" if demo else scheduler.state.source
    last = {}
    for _ in range(rounds):
        last = brain.record_scan(data, source=source)
    evolved = brain.evolve()
    return JSONResponse({"trained_rounds": rounds, "last": last, "evolved": evolved,
                         "state": brain.get_state()})


@app.post("/api/brain/improve")
def brain_improve() -> JSONResponse:
    samples = brain._samples_for_evolve()
    return JSONResponse(self_improver.improve(samples))


@app.post("/api/brain/plugins/activate")
def brain_plugin_activate(payload: dict = Body(...)) -> JSONResponse:
    return JSONResponse(self_improver.activate(str(payload.get("id", ""))))


@app.post("/api/brain/plugins/deactivate")
def brain_plugin_deactivate(payload: dict = Body(...)) -> JSONResponse:
    return JSONResponse(self_improver.deactivate(str(payload.get("id", ""))))


@app.get("/api/journal")
def journal_list(status: str = Query("all"), limit: int = Query(100, ge=1, le=500)) -> JSONResponse:
    return JSONResponse({
        "trades": journal.list_trades(status=status, limit=limit),
        "summary": journal.summary(),
        "forecast": brain.forecast_open_positions(),
    })


@app.post("/api/journal/buy")
def journal_buy(payload: dict = Body(...)) -> JSONResponse:
    """Отметить купленную позицию (из таблицы сделок или вручную)."""
    if "buy" in payload and isinstance(payload["buy"], dict):
        buy = payload["buy"]
        payload = {
            **payload,
            "title": buy.get("title", payload.get("title")),
            "game": buy.get("game", payload.get("game")),
            "buy_platform": buy.get("platform", payload.get("buy_platform")),
            "buy_price": buy.get("price_rub", payload.get("buy_price")),
            "buy_url": buy.get("url", payload.get("buy_url")),
            "item_id": buy.get("external_id", payload.get("item_id")),
            "buy_total": payload.get("buy_total_rub", payload.get("buy_total")),
            "margin": payload.get("margin_percent", payload.get("margin")),
            "risk": payload.get("risk_score", payload.get("risk")),
            "days": payload.get("est_days_to_sell", payload.get("days")),
            "liq": payload.get("liquidity_label", payload.get("liq")),
            "health": payload.get("market_health", payload.get("health")),
            "ev": payload.get("expected_value_rub", payload.get("ev")),
            "net_profit": payload.get("net_profit_rub", payload.get("net_profit")),
        }
    return JSONResponse(journal.mark_bought(payload))


@app.post("/api/journal/sell")
def journal_sell(payload: dict = Body(...)) -> JSONResponse:
    trade_id = str(payload.get("id") or payload.get("trade_id") or "")
    if not trade_id:
        return JSONResponse({"ok": False, "error": "id сделки обязателен"}, status_code=400)
    result = journal.record_sold(trade_id, payload)
    if result.get("ok"):
        trade = result["trade"]
        brain.record_real_trade(trade)
        nick = trade.get("buyer_nick") or ""
        if nick:
            client_store.record_sale(
                nick,
                game=str(trade.get("game") or ""),
                price=float(trade.get("sell_price") or 0),
                platform=str(trade.get("sell_platform") or ""),
            )
    return JSONResponse(result)


@app.post("/api/journal/lost")
def journal_lost(payload: dict = Body(...)) -> JSONResponse:
    trade_id = str(payload.get("id") or payload.get("trade_id") or "")
    if not trade_id:
        return JSONResponse({"ok": False, "error": "id сделки обязателен"}, status_code=400)
    result = journal.mark_lost(trade_id, payload)
    if result.get("ok"):
        brain.record_real_trade(result["trade"])
    return JSONResponse(result)


@app.post("/api/journal/delete")
def journal_delete(payload: dict = Body(...)) -> JSONResponse:
    return JSONResponse(journal.delete(str(payload.get("id") or "")))


@app.get("/api/clients")
def clients_list(demo: bool = Query(False)) -> JSONResponse:
    data = _load_opportunities(demo)
    source = "offline" if demo else scheduler.state.source
    return JSONResponse(analyze_clients(data, source))


@app.get("/api/clients/offers")
def clients_offers(demo: bool = Query(False)) -> JSONResponse:
    data = _load_opportunities(demo)
    source = "offline" if demo else scheduler.state.source
    return JSONResponse(build_offers(data, source))


@app.post("/api/clients/offer/send")
def clients_offer_send(offer: dict = Body(...)) -> JSONResponse:
    return JSONResponse(client_hub.send_offer(offer))


@app.post("/api/clients/sale")
def clients_record_sale(payload: dict = Body(...)) -> JSONResponse:
    """Зафиксировать факт продажи клиенту (наполняет реальную базу)."""
    return JSONResponse(client_store.record_sale(
        str(payload.get("nick", "")),
        game=str(payload.get("game", "")),
        price=float(payload.get("price", 0) or 0),
        platform=str(payload.get("platform", "")),
    ))


@app.post("/api/clients/import")
def clients_import(payload: dict = Body(...)) -> JSONResponse:
    """Импорт клиентов из CSV-текста (nick[,game][,price][,purchases][,platform])."""
    csv_text = str(payload.get("csv", ""))
    if not csv_text.strip():
        return JSONResponse({"ok": False, "error": "csv пустой"}, status_code=400)
    return JSONResponse(import_clients_csv(csv_text))


@app.post("/api/clients/clear")
def clients_clear() -> JSONResponse:
    return JSONResponse(client_store.clear())


@app.get("/favicon.png")
def favicon() -> FileResponse:
    return FileResponse(WEB_DIR / "icons" / "favicon.png")


_LOGIN_HTML = """<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ArbScanner — вход</title>
<link rel="icon" href="/static/icons/favicon.png">
<style>
*{box-sizing:border-box;margin:0;font-family:system-ui,sans-serif}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;background:#060810;color:#e6f1ff;
background-image:radial-gradient(600px circle at 30% 10%,rgba(0,240,255,.14),transparent 45%),radial-gradient(600px circle at 80% 90%,rgba(255,43,214,.14),transparent 45%)}
.box{background:rgba(16,22,40,.6);border:1px solid rgba(0,240,255,.18);border-radius:18px;padding:32px 26px;width:min(360px,92vw);backdrop-filter:blur(12px);text-align:center}
h1{font-size:20px;letter-spacing:2px;color:#00f0ff;margin-bottom:6px}
p{color:#7d8bb0;font-size:13px;margin-bottom:20px}
input{width:100%;padding:13px 14px;border-radius:10px;border:1px solid rgba(0,240,255,.25);background:rgba(0,0,0,.3);color:#fff;font-size:16px;margin-bottom:14px;text-align:center;letter-spacing:2px}
button{width:100%;padding:13px;border:none;border-radius:10px;background:linear-gradient(135deg,#00f0ff,#33f5ff);color:#060810;font-weight:700;font-size:16px;cursor:pointer}
.err{color:#ff4d6d;font-size:13px;margin-top:12px;min-height:18px}
</style></head><body>
<form class="box" method="post" action="/api/login">
<h1>ARB SCANNER</h1><p>Введите код доступа</p>
<input type="password" name="token" inputmode="numeric" autocomplete="current-password" placeholder="код доступа" autofocus>
<button type="submit">Войти</button>
<div class="err">__ERR__</div>
</form></body></html>"""


@app.get("/login")
def login_page(bad: bool = Query(False)) -> HTMLResponse:
    html = _LOGIN_HTML.replace("__ERR__", "Неверный код" if bad else "")
    return HTMLResponse(html)


@app.post("/api/login")
def login_submit(request: Request, token: str = Form(...)) -> RedirectResponse:
    if not env.dashboard_token or token == env.dashboard_token:
        resp = RedirectResponse("/", status_code=302)
        # HTTPS-туннель (Cloudflare и т.п.) передаёт X-Forwarded-Proto
        secure = request.headers.get("x-forwarded-proto", "").lower() == "https"
        resp.set_cookie("arb_token", token, max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="lax", secure=secure)
        return resp
    return RedirectResponse("/login?bad=1", status_code=302)


@app.get("/api/auth/status")
def auth_status() -> JSONResponse:
    return JSONResponse({"protected": bool(env.dashboard_token)})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
