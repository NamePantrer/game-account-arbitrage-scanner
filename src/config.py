from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parent.parent
LEARNING_DIR = ROOT / "data" / "learning"
OVERRIDES_PATH = LEARNING_DIR / "overrides.json"


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    lzt_api_token: str = ""
    lzt_balance_id: int = 0

    # FunPay: cookie golden_key (Application → Cookies → funpay.com)
    funpay_golden_key: str = ""

    # PlayerOK: cookie token (+ опционально __ddg5_ / полный cookie-header)
    playerok_token: str = ""
    playerok_ddg5: str = ""
    playerok_cookies: str = ""  # полный Cookie header, если удобнее
    playerok_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    )

    min_profit_rub: float = 50.0
    min_margin_percent: float = 8.0
    scan_interval_minutes: int = 15
    autobuy_enabled: bool = False
    autobuy_dry_run: bool = True
    dashboard_token: str = ""  # опциональный PIN для /login (пусто = без защиты)

    @field_validator("lzt_balance_id", mode="before")
    @classmethod
    def _empty_balance_id(cls, v: object) -> object:
        if v is None or v == "":
            return 0
        return v


def load_yaml_config() -> dict[str, Any]:
    path = ROOT / "config.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


env = EnvSettings()
yaml_config = load_yaml_config()

# --- Слой самонастройки: значения, которые «мозг» подбирает на данных ---
_overrides: dict[str, Any] = {}
_overrides_mtime: float = 0.0


def refresh_overrides() -> dict[str, Any]:
    """Перечитывает авто-подобранные параметры из data/learning/overrides.json."""
    global _overrides, _overrides_mtime
    if OVERRIDES_PATH.exists():
        try:
            mtime = OVERRIDES_PATH.stat().st_mtime
            if mtime == _overrides_mtime and _overrides:
                return _overrides
            _overrides = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8")) or {}
            _overrides_mtime = mtime
        except (json.JSONDecodeError, OSError):
            _overrides = {}
            _overrides_mtime = 0.0
    else:
        _overrides = {}
        _overrides_mtime = 0.0
    return _overrides


def get_overrides() -> dict[str, Any]:
    return dict(_overrides)


def _merged(section: str, base: dict) -> dict:
    """Базовый конфиг из yaml, поверх — авто-подобранные значения."""
    ov = _overrides.get(section)
    if isinstance(ov, dict):
        merged = dict(base)
        merged.update(ov)
        return merged
    return base


refresh_overrides()


def get_platform_fees(platform: str) -> dict[str, float]:
    platforms = yaml_config.get("platforms", {})
    cfg = platforms.get(platform, {})
    return {
        "buy_fee_percent": float(cfg.get("buy_fee_percent", 0)),
        "sell_fee_percent": float(cfg.get("sell_fee_percent", 5)),
        "payout_fee_percent": float(cfg.get("payout_fee_percent", 0)),
        "payout_fixed_rub": float(cfg.get("payout_fixed_rub", 0)),
        "deposit_fee_percent": float(cfg.get("deposit_fee_percent", 0)),
    }


def get_fees_config() -> dict:
    return yaml_config.get("fees", {}) or {}


def get_demand_per_day(game: str) -> float:
    liq = yaml_config.get("liquidity", {})
    default = float(liq.get("default_demand_per_day", 0.4))
    return float(liq.get("demand_per_day", {}).get(game, default))


def get_min_liquidity_score() -> float:
    return float(yaml_config.get("liquidity", {}).get("min_liquidity_score", 40))


def get_risk_config() -> dict:
    return yaml_config.get("risk", {}) or {}


def get_strategies_config() -> dict:
    return _merged("strategies", yaml_config.get("strategies", {}) or {})


def get_portfolio_config() -> dict:
    return _merged("portfolio", yaml_config.get("portfolio", {}) or {})


def get_autobuy_config() -> dict:
    return yaml_config.get("autobuy", {}) or {}


def get_pricing_config() -> dict:
    return _merged("pricing", yaml_config.get("pricing", {}) or {})


def get_learning_config() -> dict:
    return yaml_config.get("learning", {}) or {}


def get_effective_min_margin() -> float:
    """Мин. маржа: авто-подобранная (с потолком), иначе из .env."""
    base = float(env.min_margin_percent)
    thr = _overrides.get("thresholds", {})
    if isinstance(thr, dict) and "min_margin_percent" in thr:
        # мозг не должен задирать порог до «0 сделок»
        return min(float(thr["min_margin_percent"]), max(base, 15.0))
    return base


def get_effective_min_profit() -> float:
    thr = _overrides.get("thresholds", {})
    if isinstance(thr, dict) and "min_profit_rub" in thr:
        return float(thr["min_profit_rub"])
    return float(env.min_profit_rub)
