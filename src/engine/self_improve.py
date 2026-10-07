"""Самосовершенствование: ИИ дописывает себе блоки стратегий (плагины).

Плагины — декларативные правила-скоринги (не произвольный exec всего проекта).
Формат безопасный: только whitelist признаков и арифметика.

Цикл:
  1. На реальных + симулированных сэмплах перебираем кандидатов.
  2. Бэктест: uplift vs базовая политика.
  3. Лучшие → active plugins; слабые → archive.
  4. score(opp) используется при отборе/разметке сделок.
"""
from __future__ import annotations

import json
import logging
import statistics
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.config import LEARNING_DIR, get_learning_config

logger = logging.getLogger("arb-scanner.self-improve")

PLUGINS_DIR = LEARNING_DIR / "plugins"
ACTIVE_PATH = PLUGINS_DIR / "active.json"
DRAFTS_PATH = PLUGINS_DIR / "drafts.json"
ARCHIVE_PATH = PLUGINS_DIR / "archive.json"

# Разрешённые признаки сделки для формул плагинов
FEATURES = ("margin", "risk", "days", "buy_total", "liq_score", "sold", "realized_profit")
_LIQ = {"fast": 1.0, "medium": 0.75, "slow": 0.45, "dead": 0.15, "unknown": 0.5}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _feat(sample: dict) -> dict[str, float]:
    return {
        "margin": float(sample.get("margin") or sample.get("margin_percent") or 0),
        "risk": float(sample.get("risk") or sample.get("risk_score") or 0),
        "days": float(sample.get("days") or sample.get("est_days_to_sell") or 0),
        "buy_total": float(sample.get("buy_total") or sample.get("buy_total_rub") or 0),
        "liq_score": float(_LIQ.get(str(sample.get("liq") or sample.get("liquidity_label") or "unknown"), 0.5)),
        "sold": 1.0 if sample.get("sold") else 0.0,
        "realized_profit": float(sample.get("realized_profit") or 0),
    }


def score_plugin(plugin: dict, sample: dict) -> float:
    """Скоринг плагина: weight * gated features. Чем выше — тем лучше кандидат."""
    f = _feat(sample)
    gates = plugin.get("gates") or {}
    if f["margin"] < float(gates.get("margin_min", 0)):
        return float("-inf")
    if f["risk"] > float(gates.get("risk_max", 100)):
        return float("-inf")
    if f["days"] > float(gates.get("days_max", 999)):
        return float("-inf")
    if gates.get("liq_min") is not None and f["liq_score"] < float(gates["liq_min"]):
        return float("-inf")

    weights = plugin.get("weights") or {}
    # нормализованный скор
    s = (
        float(weights.get("margin", 1.0)) * (f["margin"] / 100.0)
        + float(weights.get("low_risk", 1.0)) * (1.0 - f["risk"] / 100.0)
        + float(weights.get("speed", 0.5)) * (1.0 / max(f["days"], 0.5))
        + float(weights.get("liquidity", 0.5)) * f["liq_score"]
        - float(weights.get("capital", 0.2)) * (f["buy_total"] / 10000.0)
    )
    return round(s, 4)


def passes_plugin(plugin: dict, sample: dict) -> bool:
    return score_plugin(plugin, sample) > float("-inf")


class SelfImprover:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        self.active: list[dict] = self._load(ACTIVE_PATH)
        self.drafts: list[dict] = self._load(DRAFTS_PATH)
        self.archive: list[dict] = self._load(ARCHIVE_PATH)

    @staticmethod
    def _load(path: Path) -> list[dict]:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, path: Path, rows: list[dict]) -> None:
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_state(self) -> dict:
        with self._lock:
            return {
                "active": list(self.active),
                "drafts": list(self.drafts),
                "archive_count": len(self.archive),
            }

    def score_opportunity(self, opp: dict) -> dict:
        """Оценка сделки активными плагинами → score + теги."""
        with self._lock:
            plugins = list(self.active)
        if not plugins:
            return {"score": 0.0, "plugins": [], "accepted": True}
        tags = []
        scores = []
        for p in plugins:
            sc = score_plugin(p, opp)
            if sc > float("-inf"):
                tags.append(p.get("id", "?"))
                scores.append(sc)
        if not scores:
            return {"score": 0.0, "plugins": [], "accepted": False}
        return {
            "score": round(statistics.fmean(scores), 4),
            "plugins": tags,
            "accepted": True,
        }

    def _backtest(self, plugin: dict, samples: list[dict]) -> dict:
        picked = [s for s in samples if passes_plugin(plugin, s)]
        if len(picked) < 5:
            return {"ok": False, "support": len(picked)}
        profits = [float(s.get("realized_profit") or 0) for s in picked]
        all_p = [float(s.get("realized_profit") or 0) for s in samples]
        avg = statistics.fmean(profits)
        base = statistics.fmean(all_p) if all_p else 0.0
        winrate = sum(1 for p in profits if p > 0) / len(profits)
        return {
            "ok": True,
            "support": len(picked),
            "avg_profit": round(avg, 1),
            "baseline_avg": round(base, 1),
            "uplift": round(avg - base, 1),
            "winrate": round(winrate, 3),
            "score": round(avg * (0.5 + winrate / 2), 2),
        }

    def _candidates(self, samples: list[dict]) -> list[dict]:
        """Генерирует блоки стратегий из данных (самодописывание)."""
        real = [s for s in samples if s.get("real") or s.get("source") == "real"]
        pool = real if len(real) >= 8 else samples
        if len(pool) < 8:
            return []

        # эвристики из квантилей успешных сделок
        winners = [s for s in pool if float(s.get("realized_profit") or 0) > 0]
        if len(winners) < 5:
            winners = pool

        margins = sorted(float(s.get("margin") or 0) for s in winners)
        risks = sorted(float(s.get("risk") or 0) for s in winners)
        days = sorted(float(s.get("days") or 0) for s in winners)

        def q(arr: list[float], p: float) -> float:
            if not arr:
                return 0.0
            i = min(len(arr) - 1, max(0, int(len(arr) * p)))
            return arr[i]

        cands = []
        specs = [
            ("strict", q(margins, 0.5), q(risks, 0.5), q(days, 0.6), 0.7),
            ("balanced", q(margins, 0.35), q(risks, 0.65), q(days, 0.75), 0.5),
            ("volume", max(5.0, q(margins, 0.2)), q(risks, 0.75), q(days, 0.8), 0.4),
            ("fast_liq", q(margins, 0.3), q(risks, 0.55), min(7.0, q(days, 0.4)), 0.75),
        ]
        for name, m_min, r_max, d_max, liq_min in specs:
            pid = f"auto_{name}_{int(m_min)}_{int(r_max)}"
            # веса чуть разные под профиль
            if name == "fast_liq":
                weights = {"margin": 0.8, "low_risk": 1.0, "speed": 1.4, "liquidity": 1.2, "capital": 0.3}
            elif name == "volume":
                weights = {"margin": 0.6, "low_risk": 0.8, "speed": 0.8, "liquidity": 0.7, "capital": 0.5}
            elif name == "strict":
                weights = {"margin": 1.3, "low_risk": 1.2, "speed": 0.6, "liquidity": 0.8, "capital": 0.2}
            else:
                weights = {"margin": 1.0, "low_risk": 1.0, "speed": 0.8, "liquidity": 0.8, "capital": 0.25}

            code = (
                f"# auto-generated strategy block\n"
                f"# id: {pid}\n"
                f"def accept(opp):\n"
                f"    return (opp['margin'] >= {m_min:.1f}\n"
                f"            and opp['risk'] <= {r_max:.1f}\n"
                f"            and opp['days'] <= {d_max:.1f}\n"
                f"            and opp.get('liq_score', 0.5) >= {liq_min:.2f})\n"
            )
            cands.append({
                "id": pid,
                "name": name,
                "created_at": _now_iso(),
                "origin": "self_improve",
                "gates": {
                    "margin_min": round(m_min, 1),
                    "risk_max": round(r_max, 1),
                    "days_max": round(d_max, 1),
                    "liq_min": round(liq_min, 2),
                },
                "weights": weights,
                "code": code,
                "status": "draft",
            })
        return cands

    def improve(self, samples: list[dict], *, auto_activate: bool | None = None) -> dict:
        """Генерирует/бэктестит/активирует улучшенные блоки стратегий."""
        cfg = get_learning_config()
        if auto_activate is None:
            auto_activate = bool(cfg.get("auto_activate_plugins", True))
        min_uplift = float(cfg.get("plugin_min_uplift", 30))

        candidates = self._candidates(samples)
        tested = []
        for c in candidates:
            bt = self._backtest(c, samples)
            c["backtest"] = bt
            if bt.get("ok") and bt.get("uplift", 0) >= min_uplift:
                c["status"] = "ready"
            tested.append(c)

        tested.sort(key=lambda x: (x.get("backtest") or {}).get("score", -999), reverse=True)

        with self._lock:
            # drafts — все ready/draft
            self.drafts = tested[:8]
            self._save(DRAFTS_PATH, self.drafts)

            activated = []
            if auto_activate:
                for c in tested:
                    if c.get("status") != "ready":
                        continue
                    # заменяем одноимённый профиль
                    self.active = [a for a in self.active if a.get("name") != c.get("name")]
                    active = dict(c)
                    active["status"] = "active"
                    active["activated_at"] = _now_iso()
                    self.active.append(active)
                    activated.append(active["id"])
                    if len(self.active) > 6:
                        old = self.active.pop(0)
                        old["status"] = "archived"
                        self.archive.append(old)
                self.active.sort(
                    key=lambda x: (x.get("backtest") or {}).get("score", 0), reverse=True
                )
                self._save(ACTIVE_PATH, self.active)
                if self.archive:
                    self.archive = self.archive[-40:]
                    self._save(ARCHIVE_PATH, self.archive)

        logger.info(
            "Self-improve: %d candidates, activated=%s",
            len(tested), activated if auto_activate else [],
        )
        return {
            "status": "ok",
            "candidates": len(tested),
            "ready": sum(1 for c in tested if c.get("status") == "ready"),
            "activated": activated if auto_activate else [],
            "active_count": len(self.active),
            "top": [
                {
                    "id": c["id"],
                    "name": c["name"],
                    "uplift": (c.get("backtest") or {}).get("uplift"),
                    "winrate": (c.get("backtest") or {}).get("winrate"),
                    "support": (c.get("backtest") or {}).get("support"),
                    "status": c.get("status"),
                }
                for c in tested[:4]
            ],
        }

    def activate(self, plugin_id: str) -> dict:
        with self._lock:
            draft = next((d for d in self.drafts if d.get("id") == plugin_id), None)
            if not draft:
                return {"ok": False, "error": "черновик не найден"}
            self.active = [a for a in self.active if a.get("id") != plugin_id]
            active = dict(draft)
            active["status"] = "active"
            active["activated_at"] = _now_iso()
            self.active.append(active)
            self._save(ACTIVE_PATH, self.active)
            return {"ok": True, "plugin": active}

    def deactivate(self, plugin_id: str) -> dict:
        with self._lock:
            kept = []
            found = None
            for a in self.active:
                if a.get("id") == plugin_id:
                    found = a
                else:
                    kept.append(a)
            if not found:
                return {"ok": False, "error": "не найден"}
            found["status"] = "archived"
            self.archive.append(found)
            self.active = kept
            self._save(ACTIVE_PATH, self.active)
            self._save(ARCHIVE_PATH, self.archive[-40:])
            return {"ok": True}


self_improver = SelfImprover()
