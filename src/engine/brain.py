"""Саморазвивающийся движок: обучение на сканах и реальном журнале сделок.

Что делает «мозг»:
  1. Журнал наблюдений + симуляция исхода (пока нет реальных продаж).
  2. Реальные покупки/продажи из journal — сильнее влияют на эволюцию.
  3. Эволюция порогов (маржа/риск/срок) и авто-настройка overrides.
  4. Самосовершенствование: дописывает себе блоки стратегий (плагины),
     бэктестит и активирует лучшие.
  5. Навыки, приёмы, прогноз PnL по открытым позициям.
"""
from __future__ import annotations

import json
import logging
import random
import statistics
import threading
from datetime import datetime, timezone
from pathlib import Path

from src.config import (
    LEARNING_DIR,
    OVERRIDES_PATH,
    get_learning_config,
    refresh_overrides,
)
from src.engine.web_intel import get_trend

logger = logging.getLogger("arb-scanner.brain")

# ленивые импорты journal/self_improve внутри методов — избегаем циклов

HISTORY_PATH = LEARNING_DIR / "history.jsonl"
KNOWLEDGE_PATH = LEARNING_DIR / "knowledge.json"

_LIQ_SCORE = {"fast": 1.0, "medium": 0.75, "slow": 0.45, "dead": 0.15, "unknown": 0.5}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


class LearningBrain:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.samples: list[dict] = []
        self.knowledge: dict = self._empty_knowledge()
        self._scans_seen = 0
        LEARNING_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    # ---------- persistence ----------
    @staticmethod
    def _empty_knowledge() -> dict:
        return {
            "version": 1,
            "updated_at": None,
            "scans_seen": 0,
            "samples": 0,
            "skills": [],
            "tricks": [],
            "discovered_strategies": [],
            "tuning": {},
            "trends": {},
            "performance": {},
            "real_trades": {},
            "self_improve": {},
        }

    def _load(self) -> None:
        if KNOWLEDGE_PATH.exists():
            try:
                self.knowledge = json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))
                self._scans_seen = int(self.knowledge.get("scans_seen", 0))
            except (json.JSONDecodeError, OSError):
                self.knowledge = self._empty_knowledge()
        if HISTORY_PATH.exists():
            limit = int(get_learning_config().get("history_limit", 2500))
            self.samples = self._load_history_tail(HISTORY_PATH, limit)
            if self.samples:
                self._scans_seen = max(
                    self._scans_seen,
                    max(int(s.get("scan", 0)) for s in self.samples),
                )

    @staticmethod
    def _load_history_tail(path: Path, limit: int) -> list[dict]:
        """Читает только хвост файла — не грузит всю историю в RAM."""
        try:
            size = path.stat().st_size
            if size == 0:
                return []
            # ~90 байт на slim-запись; запас на неполную первую строку
            chunk = min(size, limit * 96 + 8192)
            with path.open("rb") as f:
                f.seek(max(0, size - chunk))
                raw = f.read().decode("utf-8", errors="ignore")
            lines = raw.splitlines()
            if size > chunk and lines:
                lines = lines[1:]
            out: list[dict] = []
            for ln in lines[-limit:]:
                ln = ln.strip()
                if ln:
                    out.append(json.loads(ln))
            return out
        except (json.JSONDecodeError, OSError):
            return []

    def _persist_knowledge(self) -> None:
        KNOWLEDGE_PATH.write_text(
            json.dumps(self.knowledge, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def _append_history(self, rows: list[dict]) -> None:
        limit = int(get_learning_config().get("history_limit", 2500))
        with HISTORY_PATH.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
        # Полная перезапись — только при существенном переполнении (дорогая операция)
        trim_at = int(limit * 1.15)
        if len(self.samples) > trim_at:
            self.samples = self.samples[-limit:]
            HISTORY_PATH.write_text(
                "\n".join(
                    json.dumps(r, ensure_ascii=False, separators=(",", ":"))
                    for r in self.samples
                ) + "\n",
                encoding="utf-8",
            )

    # ---------- feedback loop ----------
    @staticmethod
    def _simulate_outcome(opp: dict, seed: int) -> dict:
        """Модель исхода сделки: продастся ли и с какой реальной прибылью.

        Пока нет подтверждённых продаж с площадок, обратная связь строится
        на вероятностной модели (успех = f(ликвидность, риск, шанс успеха)).
        Реальные подтверждения потом просто заменят симуляцию.
        """
        rng = random.Random(seed)
        liq = _LIQ_SCORE.get(opp.get("liquidity_label", "unknown"), 0.5)
        risk = float(opp.get("risk_score", 50)) / 100.0
        p = opp.get("success_probability")
        if p is None:
            p = 0.5
        p = float(p) * 0.55 + liq * 0.30 + (1 - risk) * 0.15
        p = _clamp(p, 0.03, 0.97)

        sold = rng.random() < p
        buy_total = float(opp.get("buy_total_rub", opp.get("buy", {}).get("price_rub", 0)) or 0)
        net = float(opp.get("net_profit_rub", 0))
        days = float(opp.get("est_days_to_sell", 7) or 7)
        if sold:
            realized = net
            realized_days = days
        else:
            # не продали в срок → вынужденная уценка / замороженный капитал
            realized = -round(buy_total * 0.12, 2)
            realized_days = days * 2
        return {"sold": sold, "p_sold": round(p, 3), "realized_profit": round(realized, 2),
                "realized_days": round(realized_days, 1)}

    def record_scan(self, opportunities: list[dict], source: str = "offline") -> dict:
        cfg = get_learning_config()
        if not cfg.get("enabled", True):
            return {"skipped": True}

        rows: list[dict] = []
        with self._lock:
            self._scans_seen += 1
            scan_no = self._scans_seen
            for i, opp in enumerate(opportunities):
                outcome = self._simulate_outcome(opp, seed=hash((scan_no, i, source)) & 0xFFFFFFFF)
                rows.append({
                    "ts": _now_iso(),
                    "scan": scan_no,
                    "source": source,
                    "game": opp.get("buy", {}).get("game", "unknown"),
                    "buy_total": float(opp.get("buy_total_rub", 0) or 0),
                    "margin": float(opp.get("margin_percent", 0) or 0),
                    "risk": float(opp.get("risk_score", 0) or 0),
                    "days": float(opp.get("est_days_to_sell", 0) or 0),
                    "liq": opp.get("liquidity_label", "unknown"),
                    "health": opp.get("market_health", "unknown"),
                    **outcome,
                })
            self.samples.extend(rows)
            self._append_history(rows)
            self.knowledge["scans_seen"] = self._scans_seen
            self.knowledge["samples"] = len(self.samples)

        every = int(cfg.get("evolve_every_scans", 8))
        result = {"recorded": len(rows), "scan": scan_no}
        if scan_no % max(1, every) == 0:
            if cfg.get("async_evolve", True):
                threading.Thread(
                    target=self._evolve_safe, daemon=True, name="brain-evolve",
                ).start()
                result["evolve"] = "scheduled"
            else:
                result["evolved"] = self.evolve()
        return result

    def _evolve_safe(self) -> None:
        try:
            self.evolve()
        except Exception:
            logger.exception("Brain evolution failed")

    def _samples_for_evolve(self) -> list[dict]:
        """Симуляция + реальные сделки из журнала (реальные весят больше)."""
        with self._lock:
            samples = list(self.samples)
        limit = int(get_learning_config().get("evolve_sample_limit", 1500))
        sim = samples[-limit:] if len(samples) > limit else samples
        try:
            from src.engine.journal import journal
            real = journal.closed_as_samples()
        except Exception:
            real = []
        boosted: list[dict] = []
        for r in real:
            boosted.extend([r, dict(r), dict(r)])
        merged = boosted + sim
        return merged[-limit:] if len(merged) > limit else merged

    def record_real_trade(self, trade: dict) -> dict:
        """После продажи/списания — факт в history и эволюция под реальные данные."""
        if trade.get("status") not in {"sold", "lost"}:
            return {"ok": False, "error": "нужна закрытая сделка"}
        row = {
            "ts": trade.get("sold_at") or _now_iso(),
            "scan": self._scans_seen,
            "source": "real",
            "game": trade.get("game", "unknown"),
            "buy_total": float(trade.get("buy_total") or 0),
            "margin": float(trade.get("margin_at_buy") or 0),
            "risk": float(trade.get("risk_at_buy") or 0),
            "days": float(trade.get("days_est") or 0),
            "liq": trade.get("liq", "unknown"),
            "health": trade.get("health", "unknown"),
            "sold": trade.get("status") == "sold" and float(trade.get("realized_profit") or 0) > 0,
            "p_sold": 1.0 if trade.get("status") == "sold" else 0.0,
            "realized_profit": float(trade.get("realized_profit") or 0),
            "realized_days": 0,
            "real": True,
            "trade_id": trade.get("id"),
        }
        with self._lock:
            self.samples.append(row)
            self._append_history([row])
            self.knowledge["samples"] = len(self.samples)
        cfg = get_learning_config()
        result: dict = {"ok": True, "recorded": 1}
        if cfg.get("evolve_on_real_trade", True):
            if cfg.get("async_evolve", True):
                threading.Thread(target=self._evolve_safe, daemon=True, name="brain-real").start()
                result["evolve"] = "scheduled"
            else:
                result["evolved"] = self.evolve()
        return result

    # ---------- evolution ----------
    def evolve(self) -> dict:
        cfg = get_learning_config()
        min_samples = int(cfg.get("min_samples_to_learn", 40))
        samples = self._samples_for_evolve()
        real_n = sum(1 for s in samples if s.get("real") or s.get("source") == "real")

        if len(samples) < min_samples and real_n < 5:
            with self._lock:
                self.knowledge["updated_at"] = _now_iso()
                self.knowledge["scans_seen"] = self._scans_seen
                self.knowledge["samples"] = len(self.samples)
                self._persist_knowledge()
            return {"status": "not_enough_data", "have": len(samples), "need": min_samples}

        discovered = self._discover_strategies(samples)
        with self._lock:
            prev = list(self.knowledge.get("discovered_strategies") or [])
        discovered = self._merge_strategies(prev, discovered)
        tuning = self._tune_parameters(samples, cfg)
        with self._lock:
            prev_tuning = dict(self.knowledge.get("tuning") or {})
        tuning = self._blend_tuning(prev_tuning, tuning)
        skills = self._generate_skills(samples)
        trends, rising = self._collect_trends(samples)
        tricks = self._generate_tricks(samples, rising=rising)
        perf = self._performance(samples, tuning)

        improve_result: dict = {}
        try:
            from src.engine.self_improve import self_improver
            improve_result = self_improver.improve(samples)
        except Exception:
            logger.exception("Self-improve failed")
            improve_result = {"status": "error"}

        try:
            from src.engine.journal import journal
            real_summary = journal.summary()
        except Exception:
            real_summary = {}

        with self._lock:
            self.knowledge.update({
                "updated_at": _now_iso(),
                "scans_seen": self._scans_seen,
                "samples": len(self.samples),
                "skills": skills,
                "tricks": tricks,
                "discovered_strategies": discovered,
                "tuning": tuning,
                "trends": trends,
                "performance": perf,
                "real_trades": real_summary,
                "self_improve": {
                    "activated": improve_result.get("activated", []),
                    "ready": improve_result.get("ready", 0),
                    "top": improve_result.get("top", []),
                    "active_count": improve_result.get("active_count", 0),
                },
            })
            self._persist_knowledge()

        if cfg.get("auto_apply_tuning", True):
            self._apply_overrides(tuning)

        logger.info(
            "Brain evolved: %d strategies, %d skills, plugins=%s (real=%d)",
            len(discovered), len(skills), improve_result.get("activated"), real_n,
        )
        return {
            "status": "ok",
            "strategies": len(discovered),
            "skills": len(skills),
            "tricks": len(tricks),
            "tuning": tuning,
            "real_samples": real_n,
            "self_improve": improve_result,
        }

    def _discover_strategies(self, samples: list[dict]) -> list[dict]:
        """Перебор порогов (маржа/риск/срок/здоровье) → лучшие правила.

        Иерархическая фильтрация: каждый уровень сужает подмножество,
        вместо 256 полных проходов по всем samples.
        """
        results = []
        margins = [10, 20, 30, 45]
        risks = [25, 40, 55, 70]
        days_opts = [3, 7, 14, 30]
        healths = [None, "buyers_market", "balanced", "sellers_market"]
        n = len(samples)
        min_support = max(8, int(n * 0.05))

        for m in margins:
            subset_m = [s for s in samples if s["margin"] >= m]
            if len(subset_m) < min_support:
                continue
            for r in risks:
                subset_mr = [s for s in subset_m if s["risk"] <= r]
                if len(subset_mr) < min_support:
                    continue
                for d in days_opts:
                    subset_mrd = [s for s in subset_mr if s["days"] <= d]
                    if len(subset_mrd) < min_support:
                        continue
                    for h in healths:
                        if h is None:
                            subset = subset_mrd
                        else:
                            subset = [s for s in subset_mrd if s["health"] == h]
                        if len(subset) < min_support:
                            continue
                        profits = [s["realized_profit"] for s in subset]
                        avg = statistics.fmean(profits)
                        winrate = sum(1 for s in subset if s["realized_profit"] > 0) / len(subset)
                        roi_base = statistics.fmean([s["buy_total"] for s in subset]) or 1.0
                        confidence = round(min(0.95, len(subset) / (len(subset) + 30) * (0.5 + winrate / 2)), 3)
                        results.append({
                            "id": f"M{m}/R{r}/D{d}" + (f"/{h[:3]}" if h else ""),
                            "rule": {"margin_min": m, "risk_max": r, "days_max": d, "health": h},
                            "support": len(subset),
                            "avg_realized_profit": round(avg, 1),
                            "roi_percent": round(avg / roi_base * 100, 1),
                            "winrate": round(winrate, 3),
                            "confidence": confidence,
                            "score": round(avg * (0.5 + winrate / 2) * confidence, 2),
                        })
        results.sort(key=lambda x: x["score"], reverse=True)
        # уникальные по id, топ-6
        seen, top = set(), []
        for r in results:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            top.append(r)
            if len(top) >= 6:
                break
        return top

    def _merge_strategies(self, previous: list[dict], fresh: list[dict]) -> list[dict]:
        """Не сбрасывает стратегии: обновляет старые, добавляет новые, держит топ."""
        by_id: dict[str, dict] = {}
        for old in previous:
            oid = str(old.get("id") or "")
            if not oid:
                continue
            row = dict(old)
            row["generation"] = int(row.get("generation") or 1)
            row["sticky"] = True
            by_id[oid] = row

        for neu in fresh:
            nid = str(neu.get("id") or "")
            if not nid:
                continue
            if nid in by_id:
                prev = by_id[nid]
                # EMA: 70% старый опыт + 30% новый срез (отталкиваемся от имеющегося)
                merged = dict(prev)
                for key in ("support", "avg_realized_profit", "roi_percent", "winrate", "confidence", "score"):
                    try:
                        old_v = float(prev.get(key) or 0)
                        new_v = float(neu.get(key) or 0)
                        merged[key] = round(0.7 * old_v + 0.3 * new_v, 3 if key in {"winrate", "confidence"} else 2)
                    except (TypeError, ValueError):
                        merged[key] = neu.get(key, prev.get(key))
                merged["rule"] = neu.get("rule") or prev.get("rule")
                merged["generation"] = int(prev.get("generation") or 1) + 1
                merged["updated_at"] = _now_iso()
                by_id[nid] = merged
            else:
                row = dict(neu)
                row["generation"] = 1
                row["sticky"] = False
                row["created_at"] = _now_iso()
                row["updated_at"] = _now_iso()
                by_id[nid] = row

        ranked = sorted(by_id.values(), key=lambda x: float(x.get("score") or 0), reverse=True)
        # держим до 8: сначала sticky/старые с хорошим score, потом свежие
        sticky = [r for r in ranked if r.get("sticky") or int(r.get("generation") or 0) >= 2]
        fresh_only = [r for r in ranked if r not in sticky]
        out = (sticky + fresh_only)[:8]
        out.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
        return out

    def _blend_tuning(self, previous: dict, fresh: dict) -> dict:
        """Плавно двигает пороги — без скачков «каждый скан новые настройки»."""
        if not previous:
            return fresh
        out = dict(fresh)
        for key in ("min_margin_percent", "max_risk_score", "optimal_sell_percentile"):
            if key not in previous or key not in fresh:
                continue
            try:
                old_v = float(previous[key])
                new_v = float(fresh[key])
                out[key] = round(0.75 * old_v + 0.25 * new_v, 3 if "percentile" in key else 2)
            except (TypeError, ValueError):
                pass
        out["blended_from"] = previous.get("basis_samples")
        return out

    def _tune_parameters(self, samples: list[dict], cfg: dict) -> dict:
        bounds = cfg.get("bounds", {})
        n = len(samples)

        # --- мин. маржа: порог с лучшей средней реальной прибылью ---
        mlo, mhi = bounds.get("min_margin_percent", [5.0, 25.0])
        best_m, best_m_val = mlo, float("-inf")
        for thr in [x / 2 for x in range(int(mlo * 2), int(mhi * 2) + 1)]:
            subset = [s for s in samples if s["margin"] >= thr]
            if len(subset) < max(10, n * 0.1):
                continue
            avg = statistics.fmean([s["realized_profit"] for s in subset])
            if avg > best_m_val:
                best_m_val, best_m = avg, thr

        # --- потолок риска: где средняя реальная прибыль уходит в минус ---
        rlo, rhi = bounds.get("max_risk_score", [35.0, 70.0])
        best_r = rhi
        for thr in range(int(rhi), int(rlo) - 1, -5):
            above = [s for s in samples if s["risk"] > thr]
            if len(above) >= max(8, n * 0.05):
                if statistics.fmean([s["realized_profit"] for s in above]) < 0:
                    best_r = thr
                    break
        best_r = _clamp(best_r, rlo, rhi)

        # --- перцентиль продажи: от доли реальных продаж ---
        plo, phi = bounds.get("optimal_sell_percentile", [0.40, 0.65])
        sold_rate = sum(1 for s in samples if s["sold"]) / n
        # мало продаём → дешевле/быстрее (ниже перцентиль); много → можно дороже
        pct = plo + (phi - plo) * _clamp((sold_rate - 0.45) / 0.4, 0.0, 1.0)

        return {
            "min_margin_percent": round(best_m, 2),
            "max_risk_score": round(best_r, 1),
            "optimal_sell_percentile": round(pct, 3),
            "sold_rate": round(sold_rate, 3),
            "basis_samples": n,
        }

    def _apply_overrides(self, tuning: dict) -> None:
        # мержим с уже существующими overrides — не затираем чужие ключи
        existing: dict = {}
        if OVERRIDES_PATH.exists():
            try:
                existing = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                existing = {}
        thresholds = dict(existing.get("thresholds") or {})
        pricing = dict(existing.get("pricing") or {})
        portfolio = dict(existing.get("portfolio") or {})
        if "min_margin_percent" in tuning:
            thresholds["min_margin_percent"] = tuning["min_margin_percent"]
        if "optimal_sell_percentile" in tuning:
            pricing["optimal_sell_percentile"] = tuning["optimal_sell_percentile"]
        if "max_risk_score" in tuning:
            portfolio["max_risk_score"] = tuning["max_risk_score"]
        overrides = {
            **existing,
            "thresholds": thresholds,
            "pricing": pricing,
            "portfolio": portfolio,
        }
        OVERRIDES_PATH.write_text(
            json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        refresh_overrides()

    def _generate_skills(self, samples: list[dict]) -> list[dict]:
        skills: list[dict] = []
        by_game: dict[str, list[dict]] = {}
        for s in samples:
            by_game.setdefault(s["game"], []).append(s)

        for game, rows in by_game.items():
            if len(rows) < 8:
                continue
            avg = statistics.fmean([r["realized_profit"] for r in rows])
            winrate = sum(1 for r in rows if r["realized_profit"] > 0) / len(rows)
            skills.append({
                "type": "game",
                "subject": game,
                "text": f"{game}: ср. реальная прибыль {avg:.0f}₽, доля прибыльных {winrate*100:.0f}% "
                        f"({len(rows)} набл.)",
                "value": round(avg, 1),
                "confidence": round(min(0.95, len(rows) / (len(rows) + 25)), 3),
                "positive": avg > 0,
            })

        # риск как фактор
        hi_risk = [s for s in samples if s["risk"] > 55]
        if len(hi_risk) >= 8:
            avg = statistics.fmean([s["realized_profit"] for s in hi_risk])
            skills.append({
                "type": "factor", "subject": "risk",
                "text": f"Сделки с риском >55 в среднем дают {avg:.0f}₽ — "
                        f"{'избегать' if avg < 0 else 'терпимо'}",
                "value": round(avg, 1), "confidence": 0.7, "positive": avg > 0,
            })

        # ликвидность
        fast = [s for s in samples if s["liq"] == "fast"]
        if len(fast) >= 8:
            avg = statistics.fmean([s["realized_profit"] for s in fast])
            skills.append({
                "type": "factor", "subject": "liquidity",
                "text": f"Fast-ликвидность: ср. {avg:.0f}₽, продаётся быстро — приоритет капитала",
                "value": round(avg, 1), "confidence": 0.75, "positive": avg > 0,
            })

        skills.sort(key=lambda x: abs(x["value"]), reverse=True)
        return skills[:12]

    def _generate_tricks(self, samples: list[dict], rising: list[tuple[str, float]] | None = None) -> list[dict]:
        """Легальные тактики заработка, выведенные из данных."""
        tricks: list[dict] = []
        n = len(samples)
        sold_rate = sum(1 for s in samples if s["sold"]) / n

        unsold_high_margin = [s for s in samples if not s["sold"] and s["margin"] >= 30]
        if len(unsold_high_margin) >= max(5, n * 0.05):
            tricks.append({
                "title": "Минимальный андеркат",
                "text": "Много дорогих лотов зависает непроданными. Ставь цену на 1–3% ниже "
                        "медианы честного сегмента — продаётся быстрее без большой потери маржи.",
                "impact": "скорость оборота ↑",
            })

        volume = [s for s in samples if s["buy_total"] <= 800 and s["realized_profit"] > 0]
        if len(volume) >= max(5, n * 0.05):
            games = statistics.multimode([s["game"] for s in volume])[:2]
            tricks.append({
                "title": "Объём на дешёвых",
                "text": f"Дешёвые лоты ({', '.join(games)}) стабильно прибыльны. "
                        "Бери объёмом и реинвестируй — компаундинг быстрее раскручивает капитал.",
                "impact": "оборачиваемость ↑",
            })

        if sold_rate < 0.5:
            tricks.append({
                "title": "Скидка активным клиентам",
                "text": "Доля продаж низкая. Предлагай персональную скидку постоянным покупателям "
                        "в чате площадки (в рамках правил) — конверсия выше, капитал не мёрзнет.",
                "impact": "конверсия ↑",
            })

        if rising:
            g, t = rising[0]
            tricks.append({
                "title": "Тайминг под растущий спрос",
                "text": f"Спрос на {g} растёт (тренд ×{t:.2f}). Наращивай закуп в этой категории "
                        "и продавай на пике — маржа и скорость выше.",
                "impact": "маржа ↑",
            })

        tricks.append({
            "title": "Бандлы и апселл",
            "text": "Объединяй похожие дешёвые аккаунты в набор или предлагай доп-товар к покупке — "
                    "средний чек растёт при тех же комиссиях.",
            "impact": "средний чек ↑",
        })
        return tricks

    def _collect_trends(self, samples: list[dict]) -> tuple[dict, list[tuple[str, float]]]:
        trends: dict = {}
        rising: list[tuple[str, float]] = []
        for game in {s["game"] for s in samples}:
            sig = get_trend(game)
            trends[game] = {"trend_index": sig.trend_index, "source": sig.source}
            if sig.trend_index > 1.12:
                rising.append((game, sig.trend_index))
        rising.sort(key=lambda x: x[1], reverse=True)
        return trends, rising

    @staticmethod
    def _rolling(values: list, window: int = 4) -> list:
        """Скользящее среднее — сглаживает шум симуляции исходов."""
        out = []
        for i in range(len(values)):
            chunk = [v for v in values[max(0, i - window + 1): i + 1] if v is not None]
            out.append(round(statistics.fmean(chunk), 1) if chunk else None)
        return out

    def _performance(self, samples: list[dict], tuning: dict) -> dict:
        n = len(samples)
        realized = [s["realized_profit"] for s in samples]
        sold = sum(1 for s in samples if s["sold"])
        invested = sum(s["buy_total"] for s in samples) or 1.0

        # «Политика ИИ» = выученные пороги (маржа/риск). Кривая показывает,
        # что отобранные движком сделки стабильно лучше, чем все подряд.
        m_min = float(tuning.get("min_margin_percent", 0))
        r_max = float(tuning.get("max_risk_score", 100))

        by_scan: dict[int, list[dict]] = {}
        for s in samples:
            by_scan.setdefault(s["scan"], []).append(s)
        scans = sorted(by_scan)[-30:]

        base_raw, pol_raw = [], []
        for k in scans:
            rows = by_scan[k]
            base_raw.append(statistics.fmean([r["realized_profit"] for r in rows]))
            picked = [r["realized_profit"] for r in rows
                      if r["margin"] >= m_min and r["risk"] <= r_max]
            pol_raw.append(statistics.fmean(picked) if picked else None)

        base_s = self._rolling(base_raw)
        pol_s = self._rolling(pol_raw)
        curve = [{"scan": k, "baseline": b, "policy": p}
                 for k, b, p in zip(scans, base_s, pol_s)]

        pol_vals = [p for p in pol_raw if p is not None]
        base_mean = statistics.fmean(base_raw) if base_raw else 0.0
        uplift = round(statistics.fmean(pol_vals) - base_mean, 1) if pol_vals else 0.0

        return {
            "samples": n,
            "sold_rate": round(sold / n, 3),
            "avg_realized_profit": round(statistics.fmean(realized), 1),
            "total_realized_profit": round(sum(realized), 1),
            "realized_roi_percent": round(sum(realized) / invested * 100, 2),
            "policy_uplift": uplift,
            "curve": curve,
        }

    # ---------- read API ----------
    def get_state(self) -> dict:
        with self._lock:
            k = dict(self.knowledge)
        cfg = get_learning_config()
        k["enabled"] = bool(cfg.get("enabled", True))
        k["min_samples_to_learn"] = int(cfg.get("min_samples_to_learn", 40))
        k["auto_apply_tuning"] = bool(cfg.get("auto_apply_tuning", True))
        try:
            from src.engine.self_improve import self_improver
            k["plugins"] = self_improver.get_state()
        except Exception:
            k["plugins"] = {"active": [], "drafts": []}
        try:
            from src.engine.journal import journal
            k["real_trades"] = journal.summary()
        except Exception:
            pass
        return k

    def classify(self, opp: dict) -> list[str]:
        """Помечает сделку выученными стратегиями + активными плагинами."""
        tags = []
        for strat in self.knowledge.get("discovered_strategies", []):
            r = strat["rule"]
            if (opp.get("margin_percent", 0) >= r["margin_min"]
                    and opp.get("risk_score", 100) <= r["risk_max"]
                    and opp.get("est_days_to_sell", 999) <= r["days_max"]
                    and (r["health"] is None or opp.get("market_health") == r["health"])):
                tags.append(strat["id"])
        try:
            from src.engine.self_improve import self_improver
            plug = self_improver.score_opportunity(opp)
            tags.extend(plug.get("plugins") or [])
        except Exception:
            pass
        return tags

    def forecast_open_positions(self) -> dict:
        """Прогноз дохода по открытым позициям с учётом текущей политики ИИ."""
        try:
            from src.engine.journal import journal
            opens = journal.list_trades(status="open", limit=200)
        except Exception:
            return {"count": 0, "forecast_pnl": 0, "items": []}

        tuning = self.knowledge.get("tuning") or {}
        m_min = float(tuning.get("min_margin_percent", 0))
        r_max = float(tuning.get("max_risk_score", 100))
        items = []
        total = 0.0
        for t in opens:
            est = float(t.get("net_profit_est") or 0)
            # если позиция не проходит текущую политику — дисконт прогноза
            ok = (float(t.get("margin_at_buy") or 0) >= m_min
                  and float(t.get("risk_at_buy") or 100) <= r_max)
            adj = est if ok else est * 0.55
            try:
                from src.engine.self_improve import self_improver
                sc = self_improver.score_opportunity({
                    "margin": t.get("margin_at_buy"),
                    "risk": t.get("risk_at_buy"),
                    "days": t.get("days_est"),
                    "buy_total": t.get("buy_total"),
                    "liq": t.get("liq"),
                })
                if sc.get("accepted") and sc.get("score", 0) > 0:
                    adj *= 1.0 + min(0.25, sc["score"] * 0.1)
                elif self_improver.active and not sc.get("accepted"):
                    adj *= 0.7
            except Exception:
                pass
            adj = round(adj, 1)
            total += adj
            items.append({
                "id": t.get("id"),
                "title": t.get("title"),
                "est_raw": est,
                "forecast": adj,
                "policy_ok": ok,
            })
        return {"count": len(opens), "forecast_pnl": round(total, 1), "items": items}


brain = LearningBrain()
