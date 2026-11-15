# game-account-arbitrage-scanner

Cross-marketplace scanner for public game-account listings (FunPay, PlayerOK, Lolz Market).

Учебный сканер арбитража игровых аккаунтов. Собирает публичные лоты с FunPay, PlayerOK и Lolz Market, сопоставляет похожие, считает чистую прибыль с комиссиями и показывает результат в консоли и веб-дашборде.

**Автор:** Пётр Калинкин, студент Университета ИТМО (информационная безопасность).

Проект считает спред между площадками. Он не проверяет юридическую чистоту конкретного лота и не гарантирует, что сделка закроется с расчётной прибылью.

## Что делает

1. Парсеры забирают лоты из категорий в `config.yaml` (сейчас: Steam, Brawl Stars, Genshin Impact, Valorant, Telegram).
2. Фильтр оставляет перепродаваемые товары и отсекает услуги, буст и пополнение валюты.
3. Движок ищет пары между площадками (RapidFuzz), считает комиссии, маржу, ликвидность, риск и expected value.
4. Сделки выше `MIN_PROFIT_RUB` и `MIN_MARGIN_PERCENT` печатаются в консоль и сохраняются в `data/scan_*.json`.
5. FastAPI отдаёт тот же результат в дашборд: советник, обзор, риски, рынок, стратегии, портфель, сделки, журнал, обучение, клиенты, автопокупка.

Если живых лотов нет, дашборд показывает оффлайн-выборку из `src/sample_data.py` — те же формулы прибыли, но синтетические названия. Это не результат реального скана.

Пример расчёта, не снимок рынка: лот на Lolz за 500 ₽ и похожий лот на FunPay за 800 ₽. При комиссии продажи FunPay 5% на руки остаётся около 760 ₽, оценка прибыли около 260 ₽. Фактические комиссии задаются в `config.yaml`.

## Стек

| Слой | Технологии |
| --- | --- |
| Язык | Python 3.11+ |
| API и сервер | FastAPI, Uvicorn, python-multipart |
| Модели и конфиг | Pydantic, pydantic-settings, PyYAML, python-dotenv |
| FunPay | `requests`, BeautifulSoup, lxml — публичный HTML `/lots/` |
| PlayerOK | `curl_cffi`, GraphQL `playerok.com/graphql` |
| Lolz Market | официальный HTTP API `prod-api.lzt.market` |
| Сопоставление | RapidFuzz |
| Интерфейс | статика в `web/` (HTML, CSS, JS) и Chart.js 4 с CDN |

В `requirements.txt` также указаны `aiohttp` и `schedule`. Текущий код их не импортирует: фоновый скан крутится на `threading`.

## Установка

Нужны Python 3.11+ и сеть до площадок.

```bash
cd game-account-arbitrage-scanner
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # Windows: copy .env.example .env
```

Токены только в локальном `.env`. Файл в git не попадает (см. `.gitignore`).

| Переменная | Зачем |
| --- | --- |
| `LZT_API_TOKEN` | Lolz Market → Настройки → API, scope `market`. Без него лоты Lolz не читаются. |
| `FUNPAY_GOLDEN_KEY` | Свой cookie `golden_key` с funpay.com. Для публичного скана лотов не нужен; нужен для проверки кабинета. |
| `PLAYEROK_TOKEN` | Свой cookie `token` с `.playerok.com`. Без него GraphQL PlayerOK недоступен. |
| `PLAYEROK_DDG5`, `PLAYEROK_COOKIES` | Дополнительные cookie, если площадка отдала проверку. Пустые значения допустимы. |
| `AUTOBUY_ENABLED`, `AUTOBUY_DRY_RUN` | По умолчанию `false` и `true`: покупок нет. |
| `MIN_PROFIT_RUB`, `MIN_MARGIN_PERCENT` | Пороги сделки. В шаблоне 50 ₽ и 8%. |
| `SCAN_INTERVAL_MINUTES` | Интервал фонового скана дашборда. В шаблоне 15 минут. |
| `DASHBOARD_TOKEN` | Необязательный PIN. Если задан, дашборд открывается через `/login`. |

Проверка доступов:

```bash
python -m src.check_auth
```

## Запуск

Один прогон и топ сделок в консоли:

```bash
python -m src.main
```

Дашборд (при старте поднимает фоновый скан и наблюдатель автопокупки):

```bash
python -m uvicorn src.api:app --port 8778
```

Открыть `http://127.0.0.1:8778`. Пока сканов нет, интерфейс живёт на оффлайн-выборке.

## Структура

```
game-account-arbitrage-scanner/
├── config.yaml              # категории, комиссии, фильтры, пороги
├── .env.example             # шаблон переменных, токены пустые
├── requirements.txt
├── src/
│   ├── main.py              # разовый скан
│   ├── scanner_service.py   # сбор лотов → арбитраж → JSON в data/
│   ├── api.py               # FastAPI-дашборд
│   ├── config.py            # .env и config.yaml
│   ├── models.py            # Listing, ArbitrageOpportunity, Platform
│   ├── check_auth.py        # проверка доступов к площадкам
│   ├── sample_data.py       # оффлайн-выборка для интерфейса
│   ├── parsers/             # funpay, lolz, playerok
│   ├── engine/              # matcher, profit, fees, risk, market, scheduler, ...
│   └── payments/            # покупка на Lolz через официальный API
└── web/                     # index.html, app.js, style.css, icons/
```

Каталог `data/` создаётся при скане (последние 30 файлов `scan_*.json`, журнал, очередь автопокупки). В git он не хранится.

| Путь | Назначение |
| --- | --- |
| `src/engine/scanner.py` | Поиск арбитража по парам площадок |
| `src/engine/matcher.py` | Похожесть названий |
| `src/engine/profit.py`, `fees.py` | Чистая прибыль и комиссии покупки, продажи, вывода |
| `src/engine/liquidity.py`, `risk.py` | Срок продажи, риск, expected value |
| `src/engine/pricing.py`, `market.py` | Цены без выбросов (IQR), сводка по игре |
| `src/engine/strategies.py` | Теги стратегий и распределение бюджета |
| `src/engine/listing_filter.py` | Товары против услуг и сегмент лота |
| `src/engine/scheduler.py` | Фон: скан каждые N минут |
| `src/engine/autobuy.py`, `src/payments/lolz_buyer.py` | Автопокупка только на Lolz, по умолчанию выключена |
| `src/engine/advisor.py` | План под заданный бюджет |
| `src/engine/journal.py` | Журнал своих сделок: купил / продал / потеря |
| `src/engine/brain.py`, `self_improve.py` | Подстройка порогов по истории сканов и журналу |
| `src/engine/clients.py` | База своих покупателей и черновики предложений |
| `src/engine/web_intel.py` | Поправка спроса. По умолчанию offline-эвристика, живой источник не подключён |

Покупка через API есть только у Lolz. На FunPay и PlayerOK сделка остаётся ручной. Вывод денег в коде не проводится. Предложения клиентам при `auto_send: false` сохраняются как черновики: доставки в чат площадки нет.

## Дисклеймер

Это учебный проект и личная автоматизация своего кабинета. Он смотрит публичные лоты и, при своём токене, официальный API Lolz Market.

- Чужие аккаунты, обход правил площадок и массовые рассылки в задачу кода не входят.
- В `config.yaml` для Lolz разрешены источники `personal`, `autoreg`, `resale`, `self_registration`. Источники `stealer`, `phishing`, `brute`, `retrieve_via_support` отфильтрованы.
- Фильтр источника не заменяет проверку конкретного лота. Прибыль на экране — оценка по комиссиям из конфига, не обещание дохода.
- Автопокупка выключена: `autobuy.enabled: false` и `dry_run: true`. Лимиты суммы и риска лежат в том же блоке `config.yaml`.
- Секреты остаются в `.env` на своей машине.

## Лицензия

Файла `LICENSE` в репозитории нет. Пока лицензия не выбрана, условия использования кода определяет автор.
