"""Проверка доступов к площадкам: python -m src.check_auth"""
from __future__ import annotations

import json

from src.config import env
from src.parsers.funpay import FunPayParser
from src.parsers.playerok import PlayerOKParser


def main() -> None:
    print("=== FunPay ===")
    fp = FunPayParser()
    print("golden_key set:", bool(env.funpay_golden_key))
    print(json.dumps(fp.check_auth(), ensure_ascii=False, indent=2))
    lots = fp.fetch_listings("brawl-stars", "accounts", lot_path="/lots/436/", limit=3)
    print(f"public listings sample: {len(lots)}")
    if lots:
        sample = lots[0]
        print(
            " sample:",
            (sample.title or "")[:60].encode("ascii", "replace").decode(),
            sample.price_rub,
            sample.url,
        )

    print("\n=== PlayerOK ===")
    pok = PlayerOKParser()
    print("token set:", bool(env.playerok_token or env.playerok_cookies))
    print(json.dumps(pok.check_auth(), ensure_ascii=False, indent=2))
    if pok.is_authenticated:
        items = pok.fetch_listings(
            "steam", "accounts", category_url="https://playerok.com/games/steam", limit=3
        )
        print(f"listings: {len(items)}")
        if items:
            print(" ", items[0].title[:60], items[0].price_rub, items[0].url)
    else:
        print("Как взять token:")
        print("  1. Войди на https://playerok.com")
        print("  2. F12 → Application → Cookies → .playerok.com")
        print("  3. Скопируй значение cookie `token`")
        print("  4. Вставь в .env: PLAYEROK_TOKEN=...")

    print("\n=== Lolz ===")
    print("token set:", bool(env.lzt_api_token), "len", len(env.lzt_api_token or ""))


if __name__ == "__main__":
    main()
