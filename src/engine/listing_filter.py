"""Фильтр лотов: товары vs услуги + тонкие сегменты (канал≠premium≠акк)."""
from __future__ import annotations

import re
from typing import Iterable

from src.config import yaml_config
from src.models import Listing

_HARD_SERVICE_RE = re.compile(
  r"(?:"
  r"\bуслуг|"
  r"что\s+входит\s+в\s+(?:данную\s+)?услуг|"
  r"\bбуст\b|\bboost\b|прокач|накрут|"
  r"\bfarm\b|\bфарм\b|фармить|фармит|"
  r"фарм\w*\s+(?:примо|гем|монет|опыт|куб|ранг)|"
  r"выполн\w*|"
  r"прохождени\w*|"
  r"пройд[уёе]\b|пройдем|пройдём|"
  r"соберу|соберём|собери|"
  r"\bквест\b|"
  r"зайд[уёе]\s+на|заход\w*\s+на\s+акк|войд[уёе]\s+на|"
  r"выда[её]те\s+данн|"
  r"данн\w*\s+от\s+акк|"
  r"логин\s*/\s*парол|"
  r"во\s+время\s+фарма|"
  r"входить\s+в\s+аккаунт\s+нельзя|"
  r"донат\s+на\s+ваш|пополн\w*\s+ваш|"
  r"на\s+любом\s+бойц|"
  r"за\s+минут[уы]|за\s+2\s+минут|"
  r"\d\s*[-–—]\s*\d\s*час|"
  r"активац\w*\s+ссыл|"
  r"по\s+ссылк|"
  r"расскаж\w*\s+как|"
  r"как\s+фарм|"
  r"промокод|"
  r"установл\w*\s+(?:на\s+пк|игру)|"
  r"окул|oculi|анемок|геокул|электрокул|"
  r"бездн\w*|театр\w*\s+грёз|"
  r"святил|сундук\w*\s+и\s+ключ|"
  # TG Premium «на ваш аккаунт» / подарок-активация
  r"premium\s*/\s*без\s+входа|"
  r"премиум\s*/\s*без\s+входа|"
  r"без\s+входа\s+на\s+ваш|"
  r"на\s+ваш\s+аккаунт|"
  r"подарок\s+premium|gift\s+premium"
  r")",
  re.IGNORECASE,
)

_CURRENCY_RE = re.compile(
  r"(?:"
  r"крис+т+ал|crystall|"
  r"[xх]2\b.*перв|"
  r"перв\w*\s+пополн|пополнен|top\s*-?\s*up|\bdonate\b|"
  r"гем\w*\s*(?:пак|pack|\d)|\d+\s*гем"
  r")",
  re.IGNORECASE,
)

_PASS_SERVICE_RE = re.compile(
  r"(?:brawl|pro)\s*pass|бравл\s*пасс|про\s*пасс",
  re.IGNORECASE,
)

# --- тонкие сегменты Telegram ---
_TG_CHANNEL_RE = re.compile(
  r"(?:"
  r"\bканал\b|\bchannel\b|"
  r"подписчик|subscribers?|"
  r"\bчат\b|\bchat\b|"
  r"телегабанк|telegabank|"
  r"пуст\w*\s+канал"
  r")",
  re.IGNORECASE,
)

_TG_PREMIUM_RE = re.compile(
  r"(?:telegram\s*)?premium|премиум|tg\s*prem",
  re.IGNORECASE,
)

_TG_STARS_RE = re.compile(
  r"(?:telegram\s*)?(?:stars|\bзв[её]зд)",
  re.IGNORECASE,
)

_TG_ACCOUNT_RE = re.compile(
  r"(?:"
  r"отлег|отлёг|"
  r"\+\d|"
  r"\bномер\b|phone|"
  r"\bsession\b|\btdata\b|"
  r"аккаунт|\bакк\b|\baccount\b|"
  r"любой\s+способ\s+входа|"
  r"спамблок|spamblock"
  r")",
  re.IGNORECASE,
)

_ACCOUNT_HINT_RE = re.compile(
  r"(?:"
  r"аккаунт|\bакк\b|\baccount\b|"
  r"кубк|трофе|troph|боец|brawler|"
  r"персон|герой|"
  r"(?:ур|lvl|level|ar)\s*\d|\d+\s*(?:ур|lvl|level|ar)\b|"
  r"регион|европа|америка|азия|"
  r"полная\s+передач|почта\s+привяз|"
  r"\bлеги\b|\blegend|"
  r"нероллен|стартов\w*\s+акк|"
  r"отлег|отлёг|\+\d\s*(?:сша|usa|ру|rf)"
  r")",
  re.IGNORECASE,
)

_KEY_HINT_RE = re.compile(r"\bключ\b|\bkey\b|\bgift\b|гифт", re.IGNORECASE)


def _cfg() -> dict:
  return dict(yaml_config.get("listing_filter") or {})


def detect_segment(title: str, description: str = "", game: str = "") -> str:
  """Сегмент товара. Для telegram — канал / premium / stars / account раздельно."""
  blob = f"{title} {description}"
  g = (game or "").lower()

  if _HARD_SERVICE_RE.search(blob):
    return "service"

  if "telegram" in g or "telegram" in blob.lower() or "телеграм" in blob.lower():
    if _TG_PREMIUM_RE.search(blob):
      return "tg_premium"
    if _TG_STARS_RE.search(blob):
      return "tg_stars"
    if _TG_CHANNEL_RE.search(blob):
      return "tg_channel"
    if _TG_ACCOUNT_RE.search(blob):
      return "tg_account"
    # «полная передача прав» без слова канал — часто канал
    if re.search(r"полн\w*\s+передач", blob, re.I) and re.search(r"подпис", blob, re.I):
      return "tg_channel"
    return "tg_other"

  if _CURRENCY_RE.search(blob) and not _ACCOUNT_HINT_RE.search(blob):
    return "currency"
  if _KEY_HINT_RE.search(blob) and not _ACCOUNT_HINT_RE.search(blob):
    return "key"
  if _ACCOUNT_HINT_RE.search(blob):
    return "account"
  return "other"


def is_resellable_goods(listing: Listing, *, category_hint: str = "") -> bool:
  cfg = _cfg()
  if not cfg.get("goods_only", True):
    return True

  title = f"{listing.title or ''} {listing.description or ''}"
  cat = (category_hint or listing.category or "").lower()
  blob = title.lower()
  game = (listing.game or "").lower()

  extra_reject = [str(x).lower() for x in (cfg.get("reject_keywords") or [])]
  if any(k and k in blob for k in extra_reject):
    return False

  if _HARD_SERVICE_RE.search(title):
    return False

  if _PASS_SERVICE_RE.search(title) and not _ACCOUNT_HINT_RE.search(title):
    return False

  if cfg.get("reject_currency_topups", True):
    if _CURRENCY_RE.search(title) and not _ACCOUNT_HINT_RE.search(title):
      return False

  seg = detect_segment(listing.title, listing.description, game=listing.game)

  # Telegram Premium-активация / stars-gift без товара — не арбитражим
  if seg == "tg_premium":
    return False  # почти всегда подарок на чужой акк, не перепродажа
  if seg == "service":
    return False
  if seg == "tg_other":
    # неизвестный TG-лот — только если явный аккаунт/канал
    if not (_TG_CHANNEL_RE.search(title) or _TG_ACCOUNT_RE.search(title)):
      return False

  if cat.startswith("account") and cfg.get("require_goods_for_accounts", True):
    if game == "telegram":
      return seg in {"tg_channel", "tg_account", "tg_stars"}
    if _ACCOUNT_HINT_RE.search(title) or _KEY_HINT_RE.search(title):
      return True
    if listing.platform.value == "playerok":
      return False
    if re.search(r"\d{3,}", title) or re.search(r"\b(?:ar|lvl|ур)\s*\d+", title, re.I):
      return True
    return False

  return seg not in {"service", "other", "tg_other"}


def annotate_listing(listing: Listing, forced_segment: str | None = None) -> Listing:
  if forced_segment:
    listing.segment = forced_segment
  else:
    listing.segment = detect_segment(listing.title, listing.description, game=listing.game)
  return listing


def filter_goods_listings(
  listings: Iterable[Listing],
  *,
  category_hint: str = "",
  forced_segment: str | None = None,
) -> list[Listing]:
  """forced_segment — из config (напр. FunPay /lots/702 = всегда tg_channel)."""
  out: list[Listing] = []
  for x in listings:
    title = f"{x.title or ''} {x.description or ''}"
    if _HARD_SERVICE_RE.search(title):
      continue
    if forced_segment:
      if forced_segment in {"tg_premium", "service", "currency", "other", "tg_other"}:
        continue
      if (x.game or "").lower() == "telegram" and forced_segment not in {
        "tg_channel",
        "tg_account",
        "tg_stars",
      }:
        continue
      x.segment = forced_segment
      out.append(x)
      continue
    if not is_resellable_goods(x, category_hint=category_hint):
      continue
    out.append(annotate_listing(x))
  return out
