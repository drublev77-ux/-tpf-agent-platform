# TPF Agent Platform — пилот

Автономные ИИ‑агенты TPF Technology ведут заказ на сайт или лендинг от заявки клиента до сдачи. Люди подключаются только по эскалации. Архитектура описана в документе «TPF Agent Platform — архитектура»; это код её первого этапа.

## Как идёт заказ

```
Заявка → Аналитик (ТЗ + смета) → [клиент: смета] → [предоплата, если включена]
       → Архитектор → Разработчик → QA/Security → Стейджинг на Vercel
       → [клиент: приёмка] → Релиз на Vercel → Сдан
```

- **Решения клиента — только два:** смета и приёмка. Остальное агенты делают сами.
- **Ревьюер** — отдельная модель. Он проверяет ТЗ, архитектуру и код и возвращает работу автору с замечаниями.
- **Гейты QA:** все маршруты на месте; `next build` проходит; `npm audit` не находит high/critical; ревьюер подтверждает критерии приёмки из ТЗ.
- **Эскалации.** Провал проверок трижды подряд, исчерпанный бюджет токенов, заказ вне пилота или падающая задача ставят заказ на паузу. Инженер получает уведомление и продолжает заказ через `/admin/orders/{id}/resume`.
- **Цена считается кодом, а не моделью:** часы из сметы × `HOURLY_RATE`.
- **Журнал событий только дописывается** (защищено триггером в БД). Из него строятся статус для клиента и аудит.

## Структура

```
src/tpf/
  orchestrator/states.py    граф состояний заказа (чистая логика, без I/O)
  orchestrator/service.py   переходы в транзакции, журнал, артефакты, уведомления
  orchestrator/queue.py     очередь задач на Postgres (FOR UPDATE SKIP LOCKED + аренда)
  llm/gateway.py            Anthropic API, структурный вывод через tool, бюджеты токенов
  agents/                   analyst, architect, developer, qa, devops, reviewer
  tools/sandbox.py          изолированная сборка в Docker
  tools/template.py         фиксированный шаблон Next.js; агентам разрешены только app/, components/, lib/, public/
  tools/deploy.py           деплой на Vercel через REST API
  tools/github.py           (опционально) код заказа в отдельный приватный репозиторий
  api/main.py               FastAPI: приём заказов, решения клиента, поток событий, операторские действия
  worker.py                 воркер: берёт задачи и запускает агентов
```

## Оплата (Stripe, 50/50)

1. Клиент утверждает смету. Заказ ждёт предоплату: воркер создаёт ссылку Stripe Checkout на 50% и отправляет её клиенту.
2. Stripe сообщает об оплате вебхуком на `/webhooks/stripe`. API проверяет подпись, и агенты начинают работу.
3. Клиент принимает стейджинг. Заказ ждёт остаток: вторая ссылка на оставшиеся 50%.
4. После второй оплаты DevOps выкатывает сайт в production.

Суммы всегда берутся из утверждённой сметы и считаются кодом, а не моделью. Повторный вебхук не засчитывается дважды: в базе уникальный индекс. Если ссылка истекла (Stripe держит её 24 ч), клиенту автоматически уходит новая. Для оплаты по счёту укажите `PAYMENT_PROVIDER=manual`: оператор подтверждает оплату через `POST /admin/orders/{id}/payment`.

Как включить:

| Где | Переменные |
| --- | --- |
| Vercel (API) | `PAYMENT_PROVIDER=stripe`, `PREPAYMENT_SHARE=0.5`, `STRIPE_WEBHOOK_SECRET` |
| Railway (воркер) | `PAYMENT_PROVIDER=stripe`, `PREPAYMENT_SHARE=0.5`, `STRIPE_SECRET_KEY`, `PUBLIC_BASE_URL` |
| Stripe → Developers → Webhooks | endpoint `https://tpf-agent-platform.vercel.app/webhooks/stripe`, события `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.expired` |

## Боевое развёртывание: Vercel + Supabase + Railway

| Часть | Где | Как |
| --- | --- | --- |
| API | Vercel, проект `tpf-agent-platform`, регион fra1 | `app.py` + `vercel.json`, деплой из этого репозитория |
| База | Supabase, проект `tpf-agent-platform` (eu-central-1) | миграции 001–003; приложение ходит под ролью `tpf_app` через пулер `:6543` |
| Воркер | Railway (или Fly.io/Render) | `Dockerfile.worker` + `railway.json`, `SANDBOX=vercel` |

Воркеру Docker не нужен: сборку и `npm audit` каждого сайта выполняет Vercel как превью‑деплой. Зелёное превью сразу становится стейджингом.

Переменные воркера: `DATABASE_URL` (та же строка, что у API), `ANTHROPIC_API_KEY`, `VERCEL_TOKEN`, `SANDBOX=vercel`, при желании `NOTIFIER=webhook` + `NOTIFY_WEBHOOK_URL`.

Бесплатный тариф Vercel (Hobby) по правилам Vercel предназначен только для некоммерческого использования. Перед работой с реальными клиентами переведите проект на Pro.

## Локальный запуск

```bash
cp .env.example .env        # задайте ANTHROPIC_API_KEY, VERCEL_TOKEN, ADMIN_TOKEN
docker compose up --build
```

API будет на `http://localhost:8000`, документация — на `/docs`.

```bash
# Клиент оставляет заявку
curl -X POST localhost:8000/orders -H 'content-type: application/json' -d '{
  "client_name": "Анна", "client_email": "anna@example.com", "language": "ru",
  "request_text": "Нужен лендинг для кофейни: меню, адрес, часы работы, кнопка звонка"}'
# → {"order": {...}, "client_token": "..."}

# Статус в реальном времени (SSE)
curl -N localhost:8000/orders/1/events -H 'X-Order-Token: <client_token>'

# Решения клиента
curl -X POST localhost:8000/orders/1/estimate   -H 'X-Order-Token: …' -H 'content-type: application/json' -d '{"approved": true}'
curl -X POST localhost:8000/orders/1/acceptance -H 'X-Order-Token: …' -H 'content-type: application/json' -d '{"approved": false, "comment": "Крупнее кнопку звонка"}'

# Оператор
curl localhost:8000/admin/orders?state=paused -H 'X-Admin-Token: …'
curl -X POST localhost:8000/admin/orders/1/resume -H 'X-Admin-Token: …' -H 'content-type: application/json' -d '{"note": "поправил конфиг"}'
```

Уведомления клиенту уходят на `NOTIFY_WEBHOOK_URL` (`NOTIFIER=webhook`). Оттуда их удобно разводить по e‑mail, Telegram, WhatsApp или WeChat, например через n8n.

## Тесты

```bash
pip install -e ".[dev]"
TEST_DATABASE_URL=postgresql://tpf:tpf@localhost:5432/tpf_test pytest
```

Тесты прогоняют заказ целиком на настоящем Postgres, с подменёнными моделью, песочницей и деплоем:

- путь от заявки до сдачи;
- вопросы клиенту;
- цикл «провал сборки → доработка → эскалация → продолжение»;
- отказ ревьюера и исправление;
- заказ вне пилота;
- исчерпание бюджета;
- правки клиента после стейджинга;
- отмена заказа;
- неизменяемость журнала;
- API.

## Что проверено, а что нет

- **Проверено:** 24 теста проходят. Шаблон сайта реально собирается (`next build`, Next.js 16) и проходит `npm audit` без high/critical. Next.js 15 гейт безопасности отклонил из‑за уязвимости в postcss, поэтому шаблон переведён на 16.
- **Не проверено в работе:** вызовы Anthropic API и Vercel API — они написаны по документации, но в тестах заменены заглушками. Docker‑песочница тоже не запускалась: в среде разработки не было Docker. Первый реальный заказ стоит провести под наблюдением.

Ограничения пилота:

- Песочнице нужен доступ к npm‑реестру. Ограничьте ей исходящий трафик белым списком на уровне сети хоста.
- Воркер получает Docker‑сокет, а это полный контроль над Docker хоста. Запускайте воркеры на отдельной сборочной машине.
- Бюджет проверяется перед каждым вызовом модели, поэтому фактический расход может превысить лимит на один вызов.
- Один заказ ведётся одной задачей за раз. Параллельная разработка страниц — этап 2 вместе с переходом на Temporal.
