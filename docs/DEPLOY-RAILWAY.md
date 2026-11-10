# Деплой на Railway

Бот работает в режиме long polling: ему **не нужен** публичный домен, webhook и
сертификат. Открыт только порт для `/healthz` — по нему Railway понимает, что
контейнер жив. Миграции накатываются сами при старте (`entrypoint.sh`), поэтому
руками ничего запускать не нужно.

---

## 1. Что должно быть в репозитории

Уже есть, менять не нужно:

| Файл | Зачем |
|---|---|
| `Dockerfile` | образ на `python:3.11-slim`, не от root, `HEALTHCHECK` уважает `$PORT` |
| `entrypoint.sh` | 10 попыток `alembic upgrade head`, затем `exec python -m app.main` |
| `railway.json` | builder = Dockerfile, healthcheck `/healthz`, 1 реплика, restart on failure |
| `app/health_server.py` | `/healthz` (жив) и `/readyz` (Postgres + Redis реально отвечают) |

## 2. Создание проекта

1. Railway → **New Project** → **Deploy from GitHub repo** → этот репозиторий,
   ветка `main`.
2. В проекте → **New** → **Database** → **PostgreSQL**.
3. В проекте → **New** → **Database** → **Redis**.
4. В сервисе бота → **Variables** → добавьте:

| Переменная | Значение | Обязательно |
|---|---|---|
| `BOT_TOKEN` | токен из @BotFather | да |
| `ADMINS` | ваш Telegram ID (можно несколько через запятую) | да |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` | да |
| `REDIS_URL` | `${{Redis.REDIS_URL}}` | да |
| `OPERATOR_NAME` | юрлицо/ИП для `/terms` и `/privacy` | да (для РФ) |
| `SUPPORT_CONTACT` | @ваш_саппорт | да |
| `PAYMENTS_PROVIDER_TOKEN` | токен платёжного провайдера Telegram | если продаёте Premium |
| `ALERTS_CHAT_ID` | чат/канал для алертов и оплат | опционально |
| `LOG_LEVEL` | `INFO` | опционально |

Остальные настройки имеют безопасные дефолты (см. `.env.example`).

> Railway подставляет `${{Postgres.DATABASE_URL}}` как
> `postgresql://user:pass@host:port/db` — бот сам переводит его в
> `postgresql+asyncpg://`. `?sslmode=require` в URL тоже поддерживается.

5. **Settings → Networking**: публичный домен **не включайте** — он не нужен.
   Railway сам сканирует порт и стучится в `/healthz` на нём (бот слушает `$PORT`,
   по умолчанию 8080).

## 3. Первый деплой

Деплой запускается сам после пуша. Что должно быть в логах:

```
migration attempt ...
INFO  Running upgrade  ... -> 0007_promo_codes
health_server_started host=0.0.0.0 port=8080
bot_start admins=1 moderators=0 support=0 payments=False vision=True
```

Если видите `bot_token_rejected` — неверный `BOT_TOKEN`.
Если `telegram_conflict` — где-то ещё запущен этот же бот (второй сервис,
локальная копия). Бот сам снимает webhook при старте, так что «старый webhook» —
не причина; причина именно второй поллер.

## 4. Одна реплика — это важно

В боте стоит singleton-лок в Redis: второй экземпляр при старте пишет
`singleton_lock_busy` и **выходит с кодом 1**. Так сделано специально — два
поллера на одном токене рвут друг другу апдейты.

В `railway.json` зафиксировано `numReplicas: 1`. Не увеличивайте, пока не
перейдёте на webhook-режим.

## 5. Обновления

`git push origin main` → Railway собирает образ и перезапускает контейнер.
Миграции применяются автоматически и защищены advisory-локом: два одновременных
старта не подерутся.

## 6. Проверка после деплоя

```bash
curl -f https://<ваш-домен-если-включали>/healthz   # ok
curl -f .../readyz                                  # ready (проверяет PG и Redis)
```

Без публичного домена проще всего смотреть `Deployments → View Logs`: любая
проблема с базой или Redis видна как `db:` / `redis:` в `/readyz` и как
`pool_timeout` / `ConnectionError` в логах.

В Telegram:

1. `/start` — экран согласия;
2. регистрация: пол → дата рождения → город → кого ищем;
3. `/admin` — панель должна открыться у `ADMINS`;
4. `/promo КОД` — проверить, что промокод активируется (создайте его в
   `/admin` → 🎟 Промокоды).

## 7. Если хочется webhook-режим

Сейчас его нет. Переезд на webhook нужен только при нескольких репликах и
требует: публичный домен, `set_webhook` с секретом, проверку
`X-Telegram-Bot-Api-Secret-Token`, снятие singleton-лока. Это отдельная задача,
не «галочка».

## 8. Деньги

- **Telegram Payments** (`PAYMENTS_PROVIDER_TOKEN`): чеки формирует провайдер,
  бот только активирует Premium по `successful_payment`.
- **ЮKassa** (`YOOKASSA_SHOP_ID` + `YOOKASSA_SECRET_KEY`): нужен публичный
  домен для webhook `/yookassa/webhook`, иначе используйте polling-режим
  проверки платежа (он уже есть в боте).
- `SUBSCRIPTION_PRICE_RUB` / `SUBSCRIPTION_DAYS` — цена и срок.

## 9. Бэкапы

Railway Postgres делает бэкапы на платных планах. Если план бесплатный —
настройте `pg_dump` по расписанию во внешнее хранилище. Redis можно потерять:
в нём только очереди, локи и кэш, все деньги и пользователи — в Postgres.
