# Support Drafts: чернетки відповідей підтримки з LLM

Сервіс приймає звернення з хелпдеску через вебхук, знаходить релевантні статті бази знань,
генерує LLM-чернетку відповіді з посиланнями на них, показує її оператору на модерацію і
після підтвердження надсилає відповідь клієнту через API хелпдеску.

- **Стек:** Python 3.12, FastAPI, PostgreSQL 16, SQLAlchemy 2 (async), Alembic, Anthropic SDK, Docker Compose.
- **Завдання:** [docs/task.md](docs/task.md). **Архітектура й рішення:** [docs/design.md](docs/design.md).
- **Інструкції для AI-агента:** [CLAUDE.md](CLAUDE.md).

---

## Швидкий старт

### Вимоги

- Docker 24+ і Docker Compose v2.24+ (`docker compose version`): потрібен для `env_file` з `required: false`.
- Вільні порти `8000` (сервіс) і `8001` (мок хелпдеску).
- API-ключ Anthropic **не обов'язковий**.

### Запуск

```bash
git clone <repo-url> support-drafts
cd support-drafts
cp .env.example .env          # необов'язково: ANTHROPIC_API_KEY та будь-які налаштування з app/config.py
docker compose up --build
```

Compose піднімає:

| Сервіс | Що робить | Адреса |
|---|---|---|
| `db` | PostgreSQL 16 (дані в томі `pgdata`) | всередині мережі compose |
| `migrate` | одноразово: `alembic upgrade head` + завантаження бази знань з `kb/*.md` | — |
| `api` | вебхук, `/health`, UI оператора, JSON API | http://localhost:8000 |
| `worker` | генерація чернеток і надсилання відповідей | — |
| `helpdesk-mock` | фейковий хелпдеск: шле вебхуки, приймає відповіді | http://localhost:8001 |

Перевірка:

```bash
curl http://localhost:8000/health
# OK
```

### Демо за 2 хвилини

1. Відкрийте **мок хелпдеску** http://localhost:8001 і натисніть «Надіслати вебхук» (порожнє поле
   дасть випадкове звернення) або запустіть «Пік навантаження».
2. Відкрийте **UI оператора** http://localhost:8000/ui/tickets. Звернення одразу має статус
   «Генерується чернетка». За 5–30 секунд з'явиться чернетка з посиланнями на статті бази знань
   (сторінка оновлюється сама кожні 10 с, поки ви нічого не редагуєте).
3. За потреби відредагуйте текст і натисніть **«Підтвердити й надіслати»**. Інші дії:
   **«Відхилити»** (можна вказати причину) і **«Згенерувати знову»**.
4. Поверніться на http://localhost:8001. Відповідь з'явиться в таблиці «Відповіді клієнтам».
   Мок навмисно повертає 5xx (20%) і «губить» відповіді (10%), тож у статистиці будуть
   `failed_5xx`, `lost_responses`, `idempotent_replays`, а лічильник **`duplicate_messages`
   має лишатися 0**.

### Режим з API-ключем

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
```

```bash
docker compose up -d --force-recreate worker
```

Без ключа воркер **все одно надсилає реальний запит** до `api.anthropic.com` (отримує 401),
після чого використовує статичну чернетку з випадковою затримкою 5–30 с. У логах воркера видно
`no-key mode: provider answered HTTP 401, using static response`, а в UI поле «модель» показує
`static-stub`. Статична відповідь проходить той самий парсер і валідацію, що й реальна, тому
посилання на знайдені статті підставляються так само.

---

## Ручна перевірка через curl

Вебхук з тілом у форматі завдання. `reply_url` вказує на мок, щоб відповідь було куди надіслати:

```bash
cat > /tmp/event.json <<'EOF'
{
  "event_id": "evt_8f2a",
  "ticket_id": "T-1042",
  "customer_email": "user@example.com",
  "message": "Не можу експортувати звіт у PDF",
  "reply_url": "http://helpdesk-mock:8001/tickets/T-1042/replies"
}
EOF

curl -i -X POST http://localhost:8000/webhooks/helpdesk \
     -H 'Content-Type: application/json' --data-binary @/tmp/event.json
# HTTP/1.1 202 Accepted   {"status":"accepted","event_id":"evt_8f2a"}

# повтор тієї ж події: 2xx, але другого звернення не буде
curl -i -X POST http://localhost:8000/webhooks/helpdesk \
     -H 'Content-Type: application/json' --data-binary @/tmp/event.json
# HTTP/1.1 200 OK         {"status":"duplicate","event_id":"evt_8f2a"}

# 20 одночасних доставок однієї події: рівно одне звернення
sed 's/evt_8f2a/evt_parallel/' /tmp/event.json > /tmp/event2.json
seq 20 | xargs -P20 -I{} curl -s -o /dev/null -w '%{http_code}\n' \
     -X POST http://localhost:8000/webhooks/helpdesk \
     -H 'Content-Type: application/json' --data-binary @/tmp/event2.json | sort | uniq -c
#   1 202
#  19 200
```

> На Windows (Git Bash/PowerShell) передавайте JSON з кирилицею з файлу (`--data-binary @file`),
> а не рядком у командному рядку, інакше консоль може зіпсувати кодування.

Робота оператора через JSON API:

```bash
curl http://localhost:8000/api/stats
curl 'http://localhost:8000/api/tickets?status=draft_ready'
curl -X POST http://localhost:8000/api/tickets/1/approve -H 'Content-Type: application/json' -d '{}'
curl -X POST http://localhost:8000/api/tickets/1/approve -H 'Content-Type: application/json' -d '{}'
# 409: cannot approve ticket 1 in status 'approved'
```

Пік навантаження з моку (500 звернень за 10 хвилин, 30% дубльованих доставок):

```bash
curl -X POST http://localhost:8001/simulate/burst -H 'Content-Type: application/json' \
     -d '{"count": 500, "spread_seconds": 600, "duplicate_rate": 0.3}'
curl http://localhost:8001/api/state   # статистика доставок і відповідей
```

Swagger з усіма ендпоінтами: http://localhost:8000/docs.

---

## Ендпоінти

| Метод | Шлях | Опис |
|---|---|---|
| `GET` | `/health` | `200 OK` |
| `POST` | `/webhooks/helpdesk` | нове звернення: `202` прийнято, `200` дублікат, `422` невалідне тіло, `503` не вдалося зберегти за 1.5 с (БД недоступна чи перевантажена) |
| `GET` | `/ui/tickets?status=&page=` | UI оператора: фільтр за статусом, по 50 звернень на сторінку; фільтр і сторінка зберігаються в посиланнях пагінації та після дій |
| `GET` | `/api/tickets?status=&limit=&offset=` | список звернень (новіші першими) |
| `GET` | `/api/tickets/{id}` | одне звернення |
| `GET` | `/api/stats` | кількість звернень за статусами |
| `POST` | `/api/tickets/{id}/approve` | `{"text": "...", "draft_version": N}` або `{}` (надіслати чернетку як є). Якщо `draft_version` застарів, `409` |
| `POST` | `/api/tickets/{id}/reject` | `{"reason": "..."}` |
| `POST` | `/api/tickets/{id}/regenerate` | нова чернетка (з `draft_ready`, `rejected`, `generation_failed`) |
| `POST` | `/api/tickets/{id}/retry-send` | повторити надсилання з `send_failed` (той самий ключ ідемпотентності) |

Мок хелпдеску (`:8001`): `POST /tickets/{id}/replies`, `GET /tickets/{id}/replies`,
`POST /simulate/ticket`, `POST /simulate/burst`, `POST /config`, `GET /api/state`, `GET /`.

---

## Як виконано вимоги

Деталі в [docs/design.md](docs/design.md). Коротко:

- **Вебхук відповідає за мілісекунди.** Він лише валідує тіло й виконує одну транзакцію
  (`INSERT ticket ... ON CONFLICT (event_id) DO NOTHING` + `INSERT job`). Усе збереження,
  включно з очікуванням вільного з'єднання в пулі, обмежене одним дедлайном 1.5 с
  (`WEBHOOK_TIMEOUT_SECONDS`). Якщо не встигли, відповідаємо `503`, і хелпдеск повторить
  доставку. LLM і пошук працюють у воркері.
- **Дублікати, зокрема одночасні**, відсікаються унікальним індексом на `event_id`. Друга транзакція
  чекає на індексі й нічого не вставляє. Відповідь у будь-якому разі 2xx, тож хелпдеск
  припиняє повтори.
- **Звернення не губляться.** Відповідь 2xx повертається лише після коміту в Postgres. Черга
  задач теж у Postgres (`FOR UPDATE SKIP LOCKED`, lease, fencing), тому рестарт або падіння
  воркера нічого не губить: задачу підбере інший процес після закінчення lease.
- **60 запитів/хв на весь сервіс.** Ковзне вікно в Postgres (`llm_request_log` +
  `pg_advisory_xact_lock`) діє для всіх реплік воркера. Конкурентність генерації 30, бо
  60 запитів/хв × до 30 с на виклик; цього вистачає на пік 50 звернень/хв.
- **Клієнт не отримує відповідь двічі.** Підтвердження робиться умовним UPDATE
  (`draft_ready → approved`), тож подвійний клік не створить дві відправки. На тікет є одна
  задача надсилання. Заголовок `Idempotency-Key` генерується один раз і однаковий для всіх
  ретраїв, навіть після таймауту, коли невідомо, чи дійшла відповідь. Мок хелпдеску підтримує
  цей заголовок, як Stripe/Zendesk. Обґрунтування й альтернативу див. у design.md.
- **5xx і таймаути хелпдеску** ретраяться з експоненційним backoff, jitter і `Retry-After`
  (до 12 спроб). Потім тікет переходить у статус «Помилка надсилання» з кнопкою «Повторити».
  4xx не ретраяться.
- **LLM:** Anthropic `claude-opus-5-5` (`LLM_MODEL`), structured outputs (JSON Schema),
  `effort=low`, server-side fallbacks. Модель повертає **номери** статей, а посилання
  підставляє код з бази знань, тож вигадати URL модель не може. Текст клієнта відокремлено в промпті
  (захист від prompt injection).
- **База знань:** 8 статей про вигаданий продукт «ЗвітХаб» у [kb/](kb), згенеровані LLM.
  Пошук через Postgres full-text (`tsvector` + GIN) з власною нормалізацією українських слів
  (Postgres не має українського стемера), top-3 за `ts_rank_cd`.

---

## Конфігурація

Усі параметри задаються змінними оточення (див. [app/config.py](app/config.py), [.env.example](.env.example)).
Compose передає весь `.env` у контейнери сервісу через `env_file` (`required: false`), тож без файлу
все теж запускається, із значеннями за замовчуванням. Змінні з `environment` у compose
(`DATABASE_URL`, адреси моку) мають пріоритет над `.env`. Після зміни `.env`: `docker compose up -d`.

| Змінна | За замовчуванням | Опис |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://support:support@db:5432/support` | БД |
| `ANTHROPIC_API_KEY` | порожньо | порожньо = режим без ключа |
| `LLM_MODEL` | `claude-opus-5-5` | модель |
| `LLM_EFFORT` | `low` | `low`/`medium`/`high` |
| `LLM_RATE_LIMIT_PER_MINUTE` | `60` | глобальний ліміт запитів до LLM |
| `LLM_STUB_MIN_DELAY_SECONDS` / `LLM_STUB_MAX_DELAY_SECONDS` | `5` / `30` | затримка статичної відповіді |
| `LLM_SERVER_FALLBACKS` | `true` | `fallbacks: "default"` у запиті до Anthropic |
| `GENERATION_CONCURRENCY` / `SEND_CONCURRENCY` | `30` / `10` | паралельність воркера |
| `GENERATION_MAX_ATTEMPTS` / `SEND_MAX_ATTEMPTS` | `6` / `12` | ретраї |
| `WEBHOOK_TIMEOUT_SECONDS` | `1.5` | загальний дедлайн збереження події у вебхуку (з очікуванням з'єднання з БД), після нього `503` |
| `HELPDESK_TIMEOUT_SECONDS` | `10` | таймаут запиту до хелпдеску |
| `REPLY_URL_ALLOWED_HOSTS` | `helpdesk-mock,helpdesk.example.com` у compose | allowlist хостів `reply_url` (захист від SSRF), порожньо = будь-які |
| `KB_PUBLIC_BASE_URL` | `https://help.zvithub.example/articles` | база для посилань на статті |
| `MOCK_REPLY_FAIL_RATE` | `0.2` | мок: частка 5xx |
| `MOCK_REPLY_LOST_RESPONSE_RATE` | `0.1` | мок: частка «збережено, але відповідь загубилась» |

Масштабування: `docker compose up -d --scale worker=3`. Ліміт LLM і черга спільні,
тож репліки не перевищать 60 запитів/хв і не візьмуть ту саму задачу.

---

## Тести

Тести працюють з реальним PostgreSQL (без SQLite), бо логіка спирається на `SKIP LOCKED`,
`ON CONFLICT`, advisory locks і `tsvector`.

```bash
docker compose --profile test run --rm tests
```

Що покрито (103 тести): одночасні дублікати вебхука (25 паралельних доставок дають 1 звернення),
пік із 100 подій, конкурентний claim черги, протухлий lease і fencing, глобальний rate limiter
між кількома інстансами, LLM-клієнт (no-key режим справді шле запит, класифікація помилок,
refusal), парсинг чернеток і екранування prompt injection, повний сценарій вебхук → чернетка →
підтвердження → надсилання з 5xx і таймаутами (один `Idempotency-Key` на всі спроби), подвійне
підтвердження, ручний повтор, раннер воркера, ідемпотентність моку.

### Наскрізний прогін (вручну)

Конфігурація: API + воркер + мок як окремі процеси, Postgres 16, режим без ключа (реальні запити до
Anthropic отримують 401, затримка 5–30 с), мок з 25% 5xx і 15% «загублених» відповідей:

| Що | Результат |
|---|---|
| Пік: 41 звернення, 50% подій продубльовано (одночасно й із запізненням) | 81 доставка вебхука: 41 звернення прийнято, 39 дублікатів відкинуто |
| Латентність вебхука | 5–15 мс |
| Генерація | 41/41 чернетка з правильною статтею в джерелах, 41 реальний запит до провайдера |
| Надсилання 41 відповіді | 57 запитів до хелпдеску: 9 × 5xx, 7 × таймаут після збереження, 7 повторів дедупліковано ключем. **41 відповідь, рівно по одній на тікет** |

Локально без Docker (потрібен Python 3.12 і доступний Postgres; тестова БД перестворюється):

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/support_test pytest
pre-commit install            # хуки з .pre-commit-config.yaml при кожному коміті
pre-commit run --all-files    # typos, ruff, ruff format, порожній рядок після блоків, заборона noqa, ty
```

Запуск без Docker (для розробки):

```bash
export DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/support
alembic upgrade head && python -m app.kb.seed
uvicorn app.main:create_app --factory --port 8000          # API + UI
python -m app.worker                                        # воркер
MOCK_WEBHOOK_URL=http://localhost:8000/webhooks/helpdesk MOCK_PUBLIC_BASE_URL=http://localhost:8001 \
  uvicorn helpdesk_mock.main:app --port 8001                # мок
```

---

## Структура

```
app/
  main.py            FastAPI app factory
  config.py          налаштування (env)
  models.py          tickets, jobs, kb_articles, llm_request_log
  tickets.py         use-cases: прийом, approve/reject/regenerate/retry-send
  queue.py           черга задач у Postgres
  drafts.py          промпт, JSON Schema, парсинг, посилання
  helpdesk.py        клієнт API хелпдеску
  kb/                нормалізація тексту, завантаження й пошук статей
  llm/               клієнт Anthropic (+ no-key режим), rate limiter
  web/               вебхук, JSON API, HTML UI
  worker/            обробники задач і раннер
helpdesk_mock/       мок хелпдеску
kb/                  статті бази знань (Markdown)
migrations/          Alembic
tests/               pytest
scripts/             хук pre-commit «порожній рядок після блоку»
docs/                завдання та дизайн
CLAUDE.md            правила для AI-агента
.claude/agents/      субагенти: backend-engineer, test-engineer, software-architect
.claude/rules/       стиль коду, типізація, лінтери (codestyle.md)
.claude/skills/      навички агента: address-review (обробка зауважень ревʼю)
```

## Як я працював з агентом

Розробка велася в Claude Code. Спершу в репозиторій потрапили вхідна специфікація
([docs/task.md](docs/task.md)), дизайн-документ з рішеннями ([docs/design.md](docs/design.md)) і
[CLAUDE.md](CLAUDE.md) з інваріантами, які агент не має порушувати (вебхук < 2 с, ідемпотентність,
жодних черг у пам'яті тощо). Код писався інкрементально з перевіркою на реальному Postgres:
оцінка якості пошуку на прикладах запитів виявила баг стемера, а прогін тестів виявив дві помилки
в самих тестах. Історія комітів збережена як є.

## Обмеження й наступні кроки

- Немає автентифікації оператора (UI/API варто закрити SSO або basic auth за reverse proxy).
- Немає підпису вебхука (HMAC). Формат підпису в завданні не визначено.
- Немає метрик (глибина черги, латентність LLM, ретраї) і алертів на `send_failed`.
- Пошук лексичний. Для більшої бази знань варто додати векторний (pgvector) і eval-набір для якості чернеток.
- Якщо реальний хелпдеск не підтримує `Idempotency-Key`, перед повтором після таймауту треба
  перевіряти через його API, чи відповідь уже опублікована (точка розширення: `app/helpdesk.py`).

## Корисне

```bash
docker compose logs -f worker      # що робить воркер
docker compose down -v             # зупинити й видалити дані (БД)
```
