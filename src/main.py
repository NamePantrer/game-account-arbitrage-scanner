from __future__ import annotations

import logging

from src.scanner_service import run_scan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("arb-scanner")


def main() -> None:
    logger.info("Starting one-shot arbitrage scan...")
    opportunities, counts, path = run_scan()

    for platform, count in counts.items():
        logger.info("  %s: %d listings", platform.value, count)

    logger.info("Found %d opportunities above thresholds", len(opportunities))

    if opportunities:
        logger.info("Saved to %s", path)
        for opp in opportunities[:5]:
            buy = opp.buy
            print(
                f"\n[{opp.confidence}] +{opp.net_profit_rub:.0f}₽ ({opp.margin_percent:.1f}%) "
                f"| {buy.platform.value} -> {opp.sell_platform.value}"
            )
            print(f"  {buy.title[:70]}")
            print(f"  Buy: {buy.price_rub:.0f}₽  Sell: {opp.sell_price_rub:.0f}₽")
            if opp.notes:
                print(f"  {'; '.join(opp.notes)}")
    else:
        print("\nНет сделок выше порога. Проверь config.yaml и API-токены.")


if __name__ == "__main__":
    main()
