# game-account-arbitrage-scanner

Учебный **сканер арбитража игровых аккаунтов** между FunPay, PlayerOK и Lolz Market на Python.
Собирает публичные лоты с трёх площадок, сопоставляет похожие, считает чистую прибыль с учётом
всех комиссий, оценивает риск и ликвидность и показывает результат в веб-дашборде на FastAPI.

Токены площадок задаются через `.env` (см. `.env.example`) и **в репозиторий не коммитятся**.

## Состав

| Путь | Назначение |
| --- | --- |
| `src/main.py` | Точка входа: один прогон скана, печать топ-сделок |
| `src/scanner_service.py` | Полный цикл: сбор лотов → поиск арбитража → сохранение JSON в `data/` |
| `src/api.py` | FastAPI-бэкенд для веб-дашборда |
| `src/config.py` | Загрузка `.env` (pydantic-settings) и `config.yaml`, слой самонастройки |
| `src/models.py` | Модели данных: `Listing`, `ArbitrageOpportunity`, `Platform` |
| `src/check_auth.py` | Проверка доступов к площадкам (`python -m src.check_auth`) |
| `src/sample_data.py` | Оффлайн-выборка для интерфейса без API-токенов |
| `src/parsers/funpay.py` | Парсер публичных лотов FunPay (без логина) |
| `src/parsers/playerok.py` | Парсер PlayerOK через GraphQL (нужен cookie-token) |
| `src/parsers/lolz.py` | Клиент Lolz Market API |
| `src/parsers/base.py` | Общий интерфейс парсеров |
| `src/engine/scanner.py` | Поиск арбитража между всеми парами площадок |
| `src/engine/matcher.py` | Сопоставление похожих лотов (fuzzy matching) |
| `src/engine/profit.py` | Расчёт чистой прибыли и маржи сделки |
| `src/engine/fees.py` | Полная модель комиссий: покупка, продажа, вывод |
| `src/engine/liquidity.py` | Оценка ликвидности: как быстро продастся лот |
| `src/engine/risk.py` | Риск-скоринг, вероятность провала, Expected Value |
| `src/engine/market.py` | Рыночная аналитика по категориям, аномалии цен |
| `src/engine/pricing.py` | Робастный анализ цен с фильтрацией выбросов |
| `src/engine/strategies.py` | Теги торговых стратегий и распределение бюджета |
| `src/engine/listing_filter.py` | Фильтр товары/услуги и сегментация лотов |
| `src/engine/scheduler.py` | Фоновый авто-скан каждые N минут |
| `src/engine/autobuy.py` | Автопокупка выгодных лотов на Lolz (по умолчанию выключена) |
| `src/engine/brain.py`, `self_improve.py`, `advisor.py` | Эксперименты с самонастройкой параметров на истории сканов |
| `src/engine/clients.py`, `journal.py`, `web_intel.py` | Клиенты/скидки (черновики), журнал сделок, сигналы спроса |
| `src/payments/lolz_buyer.py` | Покупка лотов через официальный Lolz API |
| `web/` | Статика дашборда (`index.html`, `app.js`, `style.css`) |
| `config.yaml` | Категории мониторинга, комиссии, фильтры, пороги стратегий |
| `.env.example` | Шаблон переменных окружения (токены — пустые) |
| `requirements.txt` | Зависимости Python |

## Требования

- Python 3.11+
- Доступ в интернет к площадкам
- Токен Lolz Market API (для скана Lolz и автопокупки)
- Опционально cookie `golden_key` FunPay и cookie `token` PlayerOK (см. ниже)

## Установка

```bash
cd game-account-arbitrage-scanner
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # Windows: copy .env.example .env
```

Затем заполните токены в `.env`:

- `LZT_API_TOKEN` — на [lzt.market](https://lzt.market) → Настройки → API → scope `market`.
- `FUNPAY_GOLDEN_KEY` — cookie `golden_key` с funpay.com (для кабинета; скан публичных лотов работает и без него).
- `PLAYEROK_TOKEN` — cookie `token` (JWT) с `.playerok.com` (без него GraphQL PlayerOK недоступен).

Проверка доступов:

```bash
python -m src.check_auth
```

## Запуск

Разовый скан из консоли:

```bash
python -m src.main
```

Веб-дашборд:

```bash
python -m uvicorn src.api:app --port 8778
```

Как работает арбитраж (упрощённо): лот на Lolz за 500 ₽ против похожего лота на FunPay за 800 ₽;
после комиссии FunPay ~5% продавец получает ~760 ₽, прибыль ~260 ₽. Сканер собирает лоты,
сопоставляет их, считает прибыль с комиссиями и фильтрует по `MIN_PROFIT_RUB` и `MIN_MARGIN_PERCENT`.

## Что это не делает

- Не гарантирует прибыль: реальные спреды 5–15%, а не 50%.
- Не проверяет легальность аккаунта за вас — в `config.yaml` заданы разрешённые источники Lolz
  (`personal`, `autoreg`, `resale`), заблокированы `stealer`/`phishing`/`brute`.
- Не выдаёт товар на FunPay и не делает полуавтомат «купить/выставить» — это пункты roadmap.
- Автопокупка по умолчанию выключена (`autobuy.enabled: false`, `dry_run: true`); включать — на свой риск.
- Токены не хранятся в коде — только в локальном `.env`.

## Структура

```
game-account-arbitrage-scanner/
├── config.yaml        # категории, комиссии, фильтры, пороги
├── .env.example       # шаблон токенов (.env не коммитится)
├── requirements.txt
├── src/
│   ├── main.py            # точка входа (one-shot скан)
│   ├── scanner_service.py # полный цикл скана
│   ├── api.py             # FastAPI-дашборд
│   ├── config.py, models.py
│   ├── parsers/           # funpay, lolz, playerok
│   ├── engine/            # matcher, profit, fees, risk, market, ...
│   └── payments/          # lolz_buyer
├── web/               # index.html, app.js, style.css
└── data/              # результаты сканов (создаётся на лету, в git не хранится)
```

## Лицензия

Учебный код, 2026. Все права у автора репозитория, пока не указано иное.
