# Scribe: сервіс транскрипції та аналізу аудіо/відео

Технічна документація · v1.0 · Python 3.12

## 1. Requirements / Technical Specification

### 1.1 Overview

**Scribe** — backend-сервіс, у який користувач завантажує аудіо- або відеозапис (зустріч, подкаст, лекція). Система асинхронно витягує аудіо, нормалізує його, розбиває на чанки, транскрибує (faster-whisper), генерує субтитри (`.srt`/`.vtt`), підсумок і дозволяє шукати по тексту з таймкодами. Прогрес обробки користувач бачить у реальному часі через WebSocket.

Стек: FastAPI, Pydantic v2, SQLAlchemy 2 (async) + Alembic, PostgreSQL, MongoDB, Redis, RabbitMQ, Celery, MinIO, FFmpeg, WebSockets, API Gateway (Traefik), Prometheus, Grafana, OpenTelemetry, Docker Compose, pytest + testcontainers, dependency-injector.

Патерни: Unit of Work, Domain Events, Transactional Outbox, Inbox (ідемпотентні споживачі).

### 1.2 Goals

- Надійний асинхронний пайплайн обробки: жодна подія не втрачається і не обробляється двічі з побічними ефектами.
- Користувач бачить статус і прогрес у реальному часі.
- Повний текст і субтитри доступні для перегляду, пошуку та експорту.
- Система спостережувана: метрики, трейси, логи з наскрізним `trace_id`.
- Весь стек піднімається однією командою `docker compose up`.
- Критичні сценарії покриті інтеграційними тестами з реальною інфраструктурою.

### 1.3 Functional requirements

| ID | Вимога |
| --- | --- |
| FR-1 | Реєстрація, логін, оновлення токена (JWT access + refresh). |
| FR-2 | CRUD проєктів (групи записів). |
| FR-3 | Завантаження файлу напряму в MinIO через presigned URL; API лише створює запис і видає URL. |
| FR-4 | Підтвердження завершення завантаження запускає пайплайн обробки. |
| FR-5 | Пайплайн: `extract_audio` → `normalize_and_chunk` → `transcribe` → `build_subtitles` → `summarize` → `index`. |
| FR-6 | Статус запису: `created → uploaded → processing → completed / failed`; для кожного етапу окремий статус і відсоток. |
| FR-7 | WebSocket `/ws/recordings/{id}`: події прогресу, часткові сегменти, завершення, помилка. |
| FR-8 | Отримання транскрипту (сегменти з `start`, `end`, `text`, `speaker`, `confidence`). |
| FR-9 | Експорт: `txt`, `srt`, `vtt`, `json` (файли лежать в MinIO, віддаються presigned URL). |
| FR-10 | Повнотекстовий пошук по транскриптах користувача (MongoDB text index) із таймкодами збігів. |
| FR-11 | Підсумок: короткий summary, ключові теми, action items (опційно через LLM API). |
| FR-12 | Повторний запуск обробки (retry) для `failed` запису, скасування (cancel) для `processing`. |
| FR-13 | Квоти: ліміт хвилин обробки на місяць залежно від тарифу. |
| FR-14 | Видалення запису каскадом: БД, Mongo, файли в MinIO. |
| FR-15 | Webhook-сповіщення клієнта про завершення (опційно, з підписом HMAC і retry). |

### 1.4 Non-functional requirements

**Продуктивність**

- Відповідь API (p95) < 200 мс для всіх ендпоінтів, окрім експорту.
- Транскрипція ≤ 0.5× тривалості аудіо на CPU `small`-моделі або ≤ 0.1× на GPU.
- Підтримка файлів до 2 ГБ та тривалістю до 4 годин.
- Горизонтальне масштабування воркерів незалежно від API.

**Надійність**

- At-least-once доставка подій + Inbox = ефективно exactly-once обробка.
- Retry з exponential backoff (3–5 спроб), після цього DLQ.
- Graceful shutdown воркерів: задача не губиться (`acks_late`, `reject_on_worker_lost`).

**Безпека**

- Паролі: argon2id. JWT: access 15 хв, refresh 7 діб (refresh зберігається в Redis, підтримка відкликання).
- Користувач бачить лише власні ресурси (перевірка власника на рівні сервісу).
- Presigned URL з TTL 15 хв, обмеження `Content-Type` і розміру.
- Rate limiting на Gateway та на рівні Redis (sliding window).
- Секрети лише через змінні середовища / Docker secrets.
- Валідація magic bytes файлу через `ffprobe` перед обробкою.

**Спостережуваність**

- Метрики Prometheus: HTTP (RED), черги, тривалість етапів, розмір Outbox, кількість дублікатів Inbox.
- OpenTelemetry трейси через HTTP → Outbox → RabbitMQ → Celery (propagation у headers повідомлення).
- Структуровані JSON-логи з `trace_id`, `recording_id`.
- Дашборди Grafana: API, пайплайн, інфраструктура. Алерти: DLQ > 0, Outbox lag > 60 с.

**Супровід**

- Чиста архітектура: `domain` → `application` → `infrastructure` → `presentation`.
- Покриття тестами ≥ 80% для domain/application, lint (ruff) та типізація (mypy strict).

### 1.5 API specification (огляд)

Base path: `/api/v1`. Автентифікація: `Authorization: Bearer <access_token>`, окрім `auth/*`. Повна специфікація — у розділі 6.

| Метод | Шлях | Опис | Успіх |
| --- | --- | --- | --- |
| POST | `/auth/register` | Реєстрація | 201 |
| POST | `/auth/login` | Логін, видає пару токенів | 200 |
| POST | `/auth/refresh` | Оновлення токенів | 200 |
| POST | `/auth/logout` | Відкликання refresh | 204 |
| GET | `/users/me` | Профіль і квота | 200 |
| POST | `/projects` | Створити проєкт | 201 |
| GET | `/projects` | Список (пагінація) | 200 |
| GET / PATCH / DELETE | `/projects/{id}` | Отримати / змінити / видалити | 200 / 200 / 204 |
| POST | `/recordings` | Створити запис, отримати upload URL | 201 |
| POST | `/recordings/{id}/complete-upload` | Запустити обробку | 202 |
| GET | `/recordings` | Список із фільтрами `project_id`, `status` | 200 |
| GET | `/recordings/{id}` | Деталі, статус, етапи | 200 |
| POST | `/recordings/{id}/retry` | Повторити після помилки | 202 |
| POST | `/recordings/{id}/cancel` | Скасувати | 202 |
| DELETE | `/recordings/{id}` | Видалити | 204 |
| GET | `/recordings/{id}/transcript` | Сегменти (пагінація за часом) | 200 |
| GET | `/recordings/{id}/summary` | Підсумок | 200 |
| POST | `/recordings/{id}/exports` | Створити експорт `{format}` | 202 |
| GET | `/recordings/{id}/exports/{format}` | Presigned URL | 200 |
| GET | `/search?q=&project_id=` | Пошук по тексту | 200 |
| WS | `/ws/recordings/{id}?token=` | Потік подій прогресу | — |
| GET | `/health/live`, `/health/ready`, `/metrics` | Службові | 200 |

**Формат WebSocket-повідомлень**

```json
{"type": "progress", "recording_id": "...", "stage": "transcribe", "percent": 42}
{"type": "segment", "start": 12.4, "end": 15.1, "text": "..."}
{"type": "completed"}
{"type": "failed", "stage": "transcribe", "error": "..."}
```

### 1.6 Data model

Схему зв’язків (PK/FK, кардинальність) показано на ER-діаграмі в розділі 3. Тут наведено склад полів.

**PostgreSQL** (транзакційні дані, SQLAlchemy 2 + Alembic):

| Таблиця | Поля |
| --- | --- |
| `plans` | `id` PK, `name` unique, `monthly_minutes`, `max_file_mb` |
| `users` | `id` UUID PK, `email` unique, `password_hash`, `plan_id` FK, `is_active`, `created_at` |
| `usage` | `user_id` FK + `period` (YYYY-MM) = composite PK, `minutes_used` |
| `projects` | `id` PK, `owner_id` FK, `name`, `description`, `created_at`, `deleted_at` |
| `recordings` | `id` PK, `project_id` FK, `owner_id` FK, `title`, `source_filename`, `content_type`, `size_bytes`, `duration_sec`, `language`, `status`, `storage_key`, `created_at`, `updated_at` |
| `processing_stages` | `id` PK, `recording_id` FK, `name`, `status`, `progress` (0–100), `attempts`, `started_at`, `finished_at`, `error` |
| `exports` | `id` PK, `recording_id` FK, `format`, `storage_key`, `status`; unique (`recording_id`, `format`) |
| `outbox_events` | `id` PK, `aggregate_type`, `aggregate_id`, `event_type`, `payload` JSONB, `headers` JSONB (trace context), `created_at`, `published_at` (NULL = ще не відправлено) |
| `inbox_messages` | `message_id` + `consumer` = composite PK, `received_at`, `processed_at` |

**MongoDB** (великі документні дані):

| Колекція | Структура |
| --- | --- |
| `transcript_segments` | `recording_id`, `owner_id`, `idx`, `start`, `end`, `text`, `speaker`, `confidence`. Індекси: (`recording_id`, `idx`), text index по `text`, (`owner_id`) |
| `transcripts` | `recording_id`, `language`, `model`, `segment_count`, `created_at` (метадані) |
| `summaries` | `recording_id`, `summary`, `topics[]`, `action_items[]`, `model`, `created_at` |

Сегменти зберігаються окремими документами, щоб не впиратися в ліміт 16 МБ на документ для довгих записів.

**Redis**: `refresh:{jti}` (TTL 7 днів), `rl:{user}:{route}` (rate limit), `progress:{recording_id}` (останній прогрес), канал Pub/Sub `rec:{recording_id}` (для WebSocket між інстансами API), `lock:recording:{id}` (розподілене блокування).

**MinIO**: бакети `uploads` (оригінали), `artifacts` (аудіо, чанки), `exports` (txt/srt/vtt/json). Ключ: `{owner_id}/{recording_id}/{kind}/{filename}`.

### 1.7 Validation rules

| Поле | Правило |
| --- | --- |
| `email` | валідний email (Pydantic `EmailStr`), ≤ 254 символи, нормалізація до lower-case |
| `password` | 10–128 символів, мінімум 1 літера і 1 цифра |
| `project.name` | 1–100 символів, після `strip()` непорожнє, унікальне в межах власника |
| `recording.title` | 1–200 символів |
| `content_type` | `audio/mpeg`, `audio/wav`, `audio/mp4`, `audio/ogg`, `audio/flac`, `video/mp4`, `video/webm`, `video/quicktime` |
| `size_bytes` | 1 … `plan.max_file_mb` (не більше 2 ГБ) |
| `language` | `auto` або код ISO 639-1 зі списку підтримуваних моделлю |
| `export format` | одне з `txt`, `srt`, `vtt`, `json` |
| Пагінація | `limit` 1–100 (за замовчуванням 20), `cursor` — непрозорий рядок |
| `search q` | 2–200 символів |
| Файл після завантаження | `ffprobe` підтверджує валідний медіапотік, наявну аудіодоріжку, тривалість ≤ 4 год |
| Квота | `usage.minutes_used + duration ≤ plan.monthly_minutes`, перевірка після `ffprobe` |
| Стани | перехід статусу дозволений лише за діаграмою станів (інакше 409) |

### 1.8 Error handling

Усі помилки API повертаються у форматі RFC 7807 (`application/problem+json`):

```json
{
  "type": "https://scribe.dev/errors/quota-exceeded",
  "title": "Quota exceeded",
  "status": 422,
  "code": "QUOTA_EXCEEDED",
  "detail": "Monthly limit of 300 minutes reached",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "errors": []
}
```

| HTTP | Code | Коли |
| --- | --- | --- |
| 400 | `BAD_REQUEST` | некоректний JSON, неприпустимий cursor |
| 401 | `UNAUTHORIZED` / `TOKEN_EXPIRED` | немає або недійсний токен |
| 403 | `FORBIDDEN` | дія заборонена |
| 404 | `NOT_FOUND` | ресурсу немає або він чужий (не розкриваємо існування) |
| 409 | `INVALID_STATE` / `EMAIL_TAKEN` | недопустимий перехід статусу, дубль email |
| 413 | `FILE_TOO_LARGE` | розмір понад ліміт тарифу |
| 415 | `UNSUPPORTED_MEDIA` | непідтримуваний формат |
| 422 | `VALIDATION_ERROR` / `QUOTA_EXCEEDED` | помилка валідації (масив `errors` з `loc`, `msg`), вичерпана квота |
| 429 | `RATE_LIMITED` | заголовок `Retry-After` |
| 503 | `DEPENDENCY_UNAVAILABLE` | `/health/ready` не пройдено |

**Помилки пайплайна**

- *Тимчасові* (мережа, таймаут MinIO, OOM): retry з exponential backoff + jitter, максимум 5 спроб, далі `failed` і повідомлення в DLQ.
- *Постійні* (пошкоджений файл, немає аудіодоріжки, непідтримуваний кодек): без retry, одразу `failed` з `error.code`.
- Кожна зміна статусу публікує Domain Event (`StageFailed`), що доходить до клієнта через WebSocket і (за потреби) webhook.
- Дубль повідомлення: Inbox знаходить `message_id`, пропускає обробку і підтверджує (ack) повідомлення.
- Втрата воркера посеред етапу: задача повертається в чергу (`acks_late`), етапи ідемпотентні (повторний запис у MinIO/Mongo через upsert за детермінованим ключем).

### 1.9 Acceptance criteria

| ID | Критерій |
| --- | --- |
| AC-1 | Користувач реєструється, логіниться, створює проєкт і запис; чужі ресурси недоступні (404). |
| AC-2 | Файл 60 хв завантажується через presigned URL, API при цьому не проксює байти. |
| AC-3 | Після `complete-upload` статус стає `processing` і через ≤ 1 с у WebSocket приходить перша подія. |
| AC-4 | Пайплайн завершується станом `completed`; транскрипт, `.srt`, `.vtt`, summary доступні через API. |
| AC-5 | Пошук по словах із транскрипту повертає сегменти з правильними таймкодами. |
| AC-6 | Дворазова доставка однієї й тієї самої події не створює дублікатів і побічних ефектів (Inbox). |
| AC-7 | Падіння брокера або воркера між комітом у БД і публікацією не губить подію (Outbox): після відновлення пайплайн продовжується. |
| AC-8 | Тимчасова помилка етапу ретраїться; постійна переводить запис у `failed` без нескінченних повторів. |
| AC-9 | `retry` і `cancel` працюють лише з дозволених статусів, інакше 409. |
| AC-10 | Перевищення квоти відхиляється з 422 `QUOTA_EXCEEDED` до запуску обробки. |
| AC-11 | У Grafana видно RPS/latency/errors API, глибину черг, тривалість етапів, Outbox lag. |
| AC-12 | Один `trace_id` проходить від HTTP-запиту до останнього етапу Celery (видно в трейсі). |
| AC-13 | `docker compose up` піднімає весь стек; `/health/ready` повертає 200. |
| AC-14 | `pytest` з testcontainers проходить локально і в CI без ручного налаштування. |

### 1.10 Testing requirements

| Рівень | Що перевіряємо | Інструменти |
| --- | --- | --- |
| Unit (domain) | Агрегат `Recording`: стани, інваріанти, генерація Domain Events; value objects; політика квот | pytest, без I/O |
| Unit (application) | Use cases з fake/in-memory UoW і репозиторіями; DI-контейнер з підміненими провайдерами | pytest, `container.override` |
| Integration (БД) | Репозиторії та UoW на реальному PostgreSQL; міграції Alembic (`upgrade head` + `downgrade base`); Mongo-репозиторій і text index | testcontainers |
| Integration (messaging) | Outbox relay публікує події в RabbitMQ; Inbox відкидає дублі; DLQ після вичерпання retry | testcontainers (RabbitMQ, Postgres) |
| Integration (storage) | presigned upload/download, життєвий цикл файлів | testcontainers (MinIO) |
| Pipeline | Кожен Celery-таск окремо + увесь ланцюжок на коротких (3–5 с) фікстурах; транскрибер замінюється стабом | `task_always_eager` для логіки, реальний воркер для e2e, ffmpeg у контейнері |
| API / contract | Усі ендпоінти: статус-коди, формат помилок, авторизація, пагінація; відповідність OpenAPI-схемі | httpx `AsyncClient`, schemathesis |
| WebSocket | Автентифікація, отримання подій у правильному порядку, робота між двома інстансами через Redis Pub/Sub | Starlette `TestClient` |
| Concurrency | Два одночасних `complete-upload` → одна обробка; гонка `retry`/`cancel` | `asyncio.gather` + блокування |
| Resilience | Зупинка RabbitMQ/воркера посеред пайплайна → подія не втрачена | testcontainers `stop()/start()` |
| Observability | `/metrics` містить очікувані метрики; `trace_id` у заголовках повідомлень | pytest + in-memory OTel exporter |

Вимоги: всі інтеграційні тести ізольовані (окрема БД або транзакція на тест), фікстури контейнерів — `scope="session"`, тести паралелізуються через `pytest-xdist`, у CI запускаються `ruff`, `mypy`, `pytest --cov`.

## 2. Implementation Plan

Орієнтовна оцінка для одного розробника: 8–10 тижнів. Кожен етап завершується робочим, протестованим інкрементом, який можна запустити через Docker Compose.

### 2.1 Структура репозиторію

```text
scribe/
├── src/scribe/
│   ├── domain/            # агрегати, value objects, Domain Events (без залежностей від фреймворків)
│   ├── application/       # use cases, інтерфейси (UoW, репозиторії, storage), event handlers
│   ├── infrastructure/    # SQLAlchemy, Mongo, Redis, MinIO, RabbitMQ, FFmpeg, whisper
│   ├── presentation/      # FastAPI routers, schemas (Pydantic), WebSocket
│   ├── workers/           # Celery app, таски, outbox relay, inbox consumers
│   └── container.py       # dependency-injector: DeclarativeContainer
├── migrations/            # Alembic
├── deploy/                # compose, traefik, prometheus, grafana, otel-collector
├── tests/{unit,integration,e2e}/
└── pyproject.toml
```

### 2.2 Етапи

| # | Етап | Основні завдання | Залежить від | Критерій завершення |
| --- | --- | --- | --- | --- |
| 0 | **Каркас** | pyproject (uv), ruff, mypy, pre-commit, CI; Compose з Postgres, Mongo, Redis, RabbitMQ, MinIO; `pydantic-settings`; DI-контейнер; логування | — | `docker compose up` піднімає інфраструктуру; порожній FastAPI відповідає на `/health/live`; CI зелений |
| 1 | **Ядро та авторизація** | SQLAlchemy 2 async, Alembic, `plans`/`users`; абстракція UoW; JWT, refresh у Redis; обробники помилок RFC 7807 | 0 | AC-1 (авторизація), міграції `up/down` проходять, інтеграційні тести на testcontainers зелені |
| 2 | **Проєкти та записи** | Агрегат `Recording` зі state machine; репозиторії; use cases; presigned upload у MinIO; `complete-upload`; `ffprobe`-валідація; Domain Events у пам’яті | 1 | AC-2; unit-тести стану агрегата; завантаження файлу працює end-to-end |
| 3 | **Outbox / Inbox та RabbitMQ** | Таблиці `outbox_events`/`inbox_messages`; запис події в одній транзакції з агрегатом (UoW); relay-процес (`SELECT … FOR UPDATE SKIP LOCKED`); топологія exchanges/queues, DLQ, retry-черги; базовий idempotent consumer | 2 | AC-6, AC-7; тест «зупинили брокер → подія доставлена після старту» |
| 4 | **Пайплайн обробки** | Celery-воркери; таски `extract_audio`, `normalize_and_chunk` (FFmpeg), `transcribe` (faster-whisper), `build_subtitles`, `summarize`, `index` (Mongo); `processing_stages`; retry/backoff; `retry`/`cancel`; квоти | 3 | AC-4, AC-8, AC-9, AC-10; тестовий файл 5 с проходить увесь ланцюжок |
| 5 | **Real-time** | WebSocket endpoint з автентифікацією; публікація прогресу в Redis Pub/Sub; доставка подій клієнтам кількох інстансів API | 3, 4 | AC-3; тест двох інстансів API; перезʼєднання клієнта отримує останній стан із `progress:{id}` |
| 6 | **Результати, пошук, експорт** | `transcript`, `summary`, `search` (Mongo text index), `exports` (txt/srt/vtt/json) | 4 | AC-5; формат `.srt`/`.vtt` валідний (перевірка парсером) |
| 7 | **API Gateway** | Traefik: маршрутизація, forward-auth JWT, rate limiting, CORS, підтримка WebSocket upgrade; окремі `api` і `ws` сервіси | 5, 6 | Усі зовнішні запити йдуть тільки через Gateway; 429 при перевищенні ліміту |
| 8 | **Observability** | `prometheus-fastapi-instrumentator` + кастомні метрики; OTel SDK + Collector → Tempo/Jaeger; propagation через Outbox і Celery; Grafana дашборди й алерти | 4 (розширюється з кожним етапом) | AC-11, AC-12; дашборди в репозиторії як код (provisioning) |
| 9 | **Додатково** | Webhooks із HMAC; діаризація спікерів; видалення з каскадом (FR-14); GPU-профіль воркера | 6 | За вибором; кожне з тестами |
| 10 | **Завершення** | E2E-сценарій, навантажувальний тест (locust/k6), README, діаграми, демо-скрипт | 0–8 | AC-13, AC-14; новий розробник запускає проєкт за README за ≤ 10 хв |

### 2.3 Залежності між етапами

Основний ланцюжок критичного шляху: **0 → 1 → 2 → 3 → 4 → 5 / 6 → 7 → 10**.

- Етап 3 (Outbox/Inbox) блокує все, що працює з подіями: 4 і 5.
- Етапи 5 (WebSocket) і 6 (результати) незалежні між собою й виконуються паралельно після етапу 4.
- Етап 8 (Observability) закладається із самого початку (базові метрики й логи в етапі 0–1) і поглиблюється поступово, а не робиться «в кінці».
- Етап 7 (Gateway) можна зробити раніше (після 2), але зручніше після появи WebSocket, щоб одразу перевірити upgrade.

### 2.4 Рекомендований порядок реалізації

1. Вертикальний зріз: **Auth → Recording → Upload → один фіктивний етап пайплайна → WebSocket**. Це доводить, що Outbox, Inbox, Celery і WS працюють разом, поки логіка ще проста.
2. Потім замінюємо фіктивний етап реальними кроками (FFmpeg, whisper) один за одним.
3. Тести пишемо разом із кодом: domain-тести спочатку, інтеграційні для кожного інфраструктурного адаптера одразу після його появи.
4. Транскрибер ховаємо за інтерфейсом `Transcriber` (стаб у тестах, faster-whisper у проді) — це пришвидшує CI на порядок.

### 2.5 Definition of Done для будь-якого етапу

- Код проходить `ruff`, `mypy --strict`, усі тести зелені, покриття нових модулів domain/application ≥ 80%.
- Є Alembic-міграція (якщо змінювалась схема) з перевіркою `downgrade`.
- Оновлено OpenAPI (генерується з коду) і, за потреби, діаграми.
- Додано метрики та span-и для нових операцій.
- Функціональність запускається через `docker compose up` без ручних кроків.

## 3. ER Diagram

&#91;embedded content: ER-діаграма · 7 таблиць PostgreSQL\]

Центральна сутність — `recordings`: належить проєкту й користувачу, має етапи обробки та експорти.

| Зв’язок | Кардинальність | FK |
| --- | --- | --- |
| `plans` → `users` | 1:N | `users.plan_id` |
| `users` → `usage` | 1:N | `usage.user_id` |
| `users` → `projects` | 1:N | `projects.owner_id` |
| `projects` → `recordings` | 1:N | `recordings.project_id` |
| `users` → `recordings` | 1:N | `recordings.owner_id` |
| `recordings` → `processing_stages` | 1:N | `processing_stages.recording_id` |
| `recordings` → `exports` | 1:N (унікально за `format`) | `exports.recording_id` |

Зв’язків M:N немає. Якщо згодом знадобиться «запис у кількох проєктах», додається таблиця `project_recordings`. Видалення: `processing_stages` і `exports` — `ON DELETE CASCADE`; `projects` — soft delete через `deleted_at`; файли й документи Mongo прибирає фоновий таск за подією `RecordingDeleted`.

## 4. Architecture Diagram

&#91;embedded content: C4 Container · 5 сховищ, 3 процеси бекенда, стек спостережуваності\]

API, Relay і воркери — це один кодовий пакет, що запускається трьома процесами з різними командами. Кожен процес використовує лише потрібні йому сховища; шина на схемі показує доступ, а не єдину залежність.

**C4 Model**

| Рівень | Що показує |
| --- | --- |
| 1. Context | Користувач/клієнтський застосунок → Scribe; необов’язкова зовнішня система: LLM API для підсумків |
| 2. Container | Схема вище: Gateway, API, Outbox Relay, Celery Workers, PostgreSQL, MongoDB, Redis, RabbitMQ, MinIO, стек спостережуваності |
| 3. Component (API) | `presentation` (routers, WS) → `application` (use cases, event handlers) → `domain` (Recording, Project, User, Domain Events) ← `infrastructure` (репозиторії, UoW, адаптери MinIO/Redis/RabbitMQ); усе збирає `dependency-injector` |
| 4. Code | Ключові класи: `Recording` (state machine), `UnitOfWork`, `OutboxRepository`, `InboxGuard`, `Transcriber` (інтерфейс), `ProgressPublisher` |

**Ролі DI-контейнера.** `Container` оголошує провайдери: `Singleton` для engine/клієнтів (Mongo, Redis, MinIO), `Factory` для UoW і use cases, `Resource` для життєвого циклу підключень. У тестах `container.override_providers(...)` підміняє транскрибер, storage і годинник.

## 5. Sequence Diagram

&#91;embedded content: Sequence · завантаження і обробка запису, 8 учасників\]

Сценарій: «завантаження запису → запуск обробки → прогрес у реальному часі». Два виділені кроки — ключові: зміна статусу та подія зберігаються в одній транзакції (Outbox), а воркер перед обробкою перевіряє, чи вже бачив це повідомлення (Inbox).

**Альтернативні гілки**

- Релей впав після публікації, але до позначки `published_at`: подія надійде двічі, другу відсіче Inbox.
- Воркер впав під час етапу: повідомлення не підтверджене (`acks_late`), брокер повертає його іншому воркеру; етап ідемпотентний.
- Помилка на етапі: воркер пише `StageFailed` в Outbox, клієнт отримує `failed` через WebSocket; тимчасова помилка — retry з backoff, після ліміту — DLQ.
- WebSocket-клієнт під’єднався пізно: отримує останній стан з `progress:{id}` у Redis, далі — нові події.

**Діаграма станів запису (дозволені переходи)**

| З стану | В стан | Подія / дія |
| --- | --- | --- |
| `created` | `uploaded` | `complete-upload` |
| `created` | `deleted` | `DELETE` |
| `uploaded` | `processing` | валідація `ffprobe` + квота пройшли |
| `uploaded` | `failed` | файл невалідний або квоту вичерпано |
| `processing` | `completed` | всі етапи `completed` |
| `processing` | `failed` | постійна помилка або вичерпано retry |
| `processing` | `cancelled` | `cancel` |
| `failed`, `cancelled` | `processing` | `retry` |
| `completed`, `failed`, `cancelled` | `deleted` | `DELETE` |

Будь-який інший перехід відхиляється агрегатом і повертає 409 `INVALID_STATE`.

## 6. API Contract / OpenAPI

Нижче скорочений контракт OpenAPI 3.1 для ключових сценаріїв. Повну специфікацію FastAPI генерує з Pydantic-схем (`/openapi.json`, UI на `/docs`); цей фрагмент є еталоном, з яким звіряються contract-тести (schemathesis).

**Загальні правила**

- Аутентифікація: `Authorization: Bearer <JWT>` для всіх ендпоінтів, окрім `/auth/register`, `/auth/login`, `/auth/refresh`, `/health/*`. WebSocket: `?token=<access_token>`.
- Усі ID: UUID. Дати: ISO 8601 UTC. Час у транскрипті: секунди (float).
- Пагінація курсорна: `?limit=20&cursor=...` → `{ "items": [], "next_cursor": null }`.
- Помилки: `application/problem+json` зі схемою `Problem` (див. 1.8). Кожен захищений ендпоінт може повернути 401, 429.
- Ідемпотентність: `POST /recordings/{id}/complete-upload` та `/retry` — повторний виклик у тому самому стані повертає 202 без повторного запуску.

```yaml
openapi: 3.1.0
info:
  title: Scribe API
  version: 1.0.0
servers:
  - url: https://api.scribe.local/api/v1
security:
  - bearerAuth: []

paths:
  /auth/register:
    post:
      security: []
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: '#/components/schemas/RegisterRequest' }
      responses:
        '201': { description: Created, content: { application/json: { schema: { $ref: '#/components/schemas/User' } } } }
        '409': { $ref: '#/components/responses/Problem' }   # EMAIL_TAKEN
        '422': { $ref: '#/components/responses/Problem' }

  /auth/login:
    post:
      security: []
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: '#/components/schemas/LoginRequest' }
      responses:
        '200': { description: OK, content: { application/json: { schema: { $ref: '#/components/schemas/TokenPair' } } } }
        '401': { $ref: '#/components/responses/Problem' }

  /projects:
    get:
      parameters:
        - { $ref: '#/components/parameters/Limit' }
        - { $ref: '#/components/parameters/Cursor' }
      responses:
        '200': { description: OK, content: { application/json: { schema: { $ref: '#/components/schemas/ProjectPage' } } } }
    post:
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: '#/components/schemas/ProjectCreate' }
      responses:
        '201': { description: Created, content: { application/json: { schema: { $ref: '#/components/schemas/Project' } } } }
        '409': { $ref: '#/components/responses/Problem' }
        '422': { $ref: '#/components/responses/Problem' }

  /recordings:
    post:
      summary: Створити запис і отримати URL для завантаження
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: '#/components/schemas/RecordingCreate' }
      responses:
        '201': { description: Created, content: { application/json: { schema: { $ref: '#/components/schemas/RecordingCreated' } } } }
        '404': { $ref: '#/components/responses/Problem' }   # проєкт не знайдено
        '413': { $ref: '#/components/responses/Problem' }
        '415': { $ref: '#/components/responses/Problem' }
        '422': { $ref: '#/components/responses/Problem' }
    get:
      parameters:
        - { name: project_id, in: query, schema: { type: string, format: uuid } }
        - { name: status, in: query, schema: { $ref: '#/components/schemas/RecordingStatus' } }
        - { $ref: '#/components/parameters/Limit' }
        - { $ref: '#/components/parameters/Cursor' }
      responses:
        '200': { description: OK, content: { application/json: { schema: { $ref: '#/components/schemas/RecordingPage' } } } }

  /recordings/{id}:
    parameters:
      - { name: id, in: path, required: true, schema: { type: string, format: uuid } }
    get:
      responses:
        '200': { description: OK, content: { application/json: { schema: { $ref: '#/components/schemas/Recording' } } } }
        '404': { $ref: '#/components/responses/Problem' }
    delete:
      responses:
        '204': { description: Deleted }
        '404': { $ref: '#/components/responses/Problem' }
        '409': { $ref: '#/components/responses/Problem' }

  /recordings/{id}/complete-upload:
    post:
      parameters:
        - { name: id, in: path, required: true, schema: { type: string, format: uuid } }
      responses:
        '202': { description: Обробку прийнято, content: { application/json: { schema: { $ref: '#/components/schemas/Recording' } } } }
        '404': { $ref: '#/components/responses/Problem' }
        '409': { $ref: '#/components/responses/Problem' }   # INVALID_STATE
        '422': { $ref: '#/components/responses/Problem' }   # QUOTA_EXCEEDED, файл невалідний

  /recordings/{id}/transcript:
    get:
      parameters:
        - { name: id, in: path, required: true, schema: { type: string, format: uuid } }
        - { name: from, in: query, schema: { type: number, minimum: 0 } }
        - { $ref: '#/components/parameters/Limit' }
        - { $ref: '#/components/parameters/Cursor' }
      responses:
        '200': { description: OK, content: { application/json: { schema: { $ref: '#/components/schemas/TranscriptPage' } } } }
        '404': { $ref: '#/components/responses/Problem' }
        '409': { $ref: '#/components/responses/Problem' }   # ще не готово

  /recordings/{id}/exports/{format}:
    get:
      parameters:
        - { name: id, in: path, required: true, schema: { type: string, format: uuid } }
        - { name: format, in: path, required: true, schema: { type: string, enum: [txt, srt, vtt, json] } }
      responses:
        '200': { description: OK, content: { application/json: { schema: { $ref: '#/components/schemas/ExportLink' } } } }
        '404': { $ref: '#/components/responses/Problem' }

  /search:
    get:
      parameters:
        - { name: q, in: query, required: true, schema: { type: string, minLength: 2, maxLength: 200 } }
        - { name: project_id, in: query, schema: { type: string, format: uuid } }
        - { $ref: '#/components/parameters/Limit' }
      responses:
        '200': { description: OK, content: { application/json: { schema: { $ref: '#/components/schemas/SearchResult' } } } }
        '422': { $ref: '#/components/responses/Problem' }

components:
  securitySchemes:
    bearerAuth: { type: http, scheme: bearer, bearerFormat: JWT }

  parameters:
    Limit:  { name: limit,  in: query, schema: { type: integer, minimum: 1, maximum: 100, default: 20 } }
    Cursor: { name: cursor, in: query, schema: { type: string } }

  responses:
    Problem:
      description: Помилка
      content:
        application/problem+json:
          schema: { $ref: '#/components/schemas/Problem' }

  schemas:
    Problem:
      type: object
      required: [type, title, status, code]
      properties:
        type:     { type: string, format: uri }
        title:    { type: string }
        status:   { type: integer }
        code:     { type: string, example: QUOTA_EXCEEDED }
        detail:   { type: string }
        trace_id: { type: string }
        errors:
          type: array
          items:
            type: object
            properties: { loc: { type: array, items: { type: string } }, msg: { type: string } }

    RegisterRequest:
      type: object
      required: [email, password]
      properties:
        email:    { type: string, format: email, maxLength: 254 }
        password: { type: string, minLength: 10, maxLength: 128 }
    LoginRequest:
      type: object
      required: [email, password]
      properties:
        email:    { type: string, format: email }
        password: { type: string }
    TokenPair:
      type: object
      required: [access_token, refresh_token, token_type, expires_in]
      properties:
        access_token:  { type: string }
        refresh_token: { type: string }
        token_type:    { type: string, enum: [bearer] }
        expires_in:    { type: integer, example: 900 }
    User:
      type: object
      properties:
        id:         { type: string, format: uuid }
        email:      { type: string, format: email }
        plan:       { type: string }
        created_at: { type: string, format: date-time }

    ProjectCreate:
      type: object
      required: [name]
      properties:
        name:        { type: string, minLength: 1, maxLength: 100 }
        description: { type: string, maxLength: 1000 }
    Project:
      allOf:
        - { $ref: '#/components/schemas/ProjectCreate' }
        - type: object
          properties:
            id:         { type: string, format: uuid }
            created_at: { type: string, format: date-time }
    ProjectPage:
      type: object
      properties:
        items:       { type: array, items: { $ref: '#/components/schemas/Project' } }
        next_cursor: { type: [string, 'null'] }

    RecordingStatus:
      type: string
      enum: [created, uploaded, processing, completed, failed, cancelled]
    RecordingCreate:
      type: object
      required: [project_id, title, filename, content_type, size_bytes]
      properties:
        project_id:   { type: string, format: uuid }
        title:        { type: string, minLength: 1, maxLength: 200 }
        filename:     { type: string }
        content_type: { type: string, example: audio/mpeg }
        size_bytes:   { type: integer, minimum: 1, maximum: 2147483648 }
        language:     { type: string, default: auto }
    Stage:
      type: object
      properties:
        name:     { type: string, enum: [extract_audio, normalize_and_chunk, transcribe, build_subtitles, summarize, index] }
        status:   { type: string, enum: [pending, running, completed, failed] }
        progress: { type: integer, minimum: 0, maximum: 100 }
        error:    { type: [string, 'null'] }
    Recording:
      type: object
      properties:
        id:           { type: string, format: uuid }
        project_id:   { type: string, format: uuid }
        title:        { type: string }
        status:       { $ref: '#/components/schemas/RecordingStatus' }
        duration_sec: { type: [number, 'null'] }
        language:     { type: [string, 'null'] }
        stages:       { type: array, items: { $ref: '#/components/schemas/Stage' } }
        created_at:   { type: string, format: date-time }
        updated_at:   { type: string, format: date-time }
    RecordingCreated:
      type: object
      properties:
        recording:  { $ref: '#/components/schemas/Recording' }
        upload_url: { type: string, format: uri }
        expires_at: { type: string, format: date-time }
    RecordingPage:
      type: object
      properties:
        items:       { type: array, items: { $ref: '#/components/schemas/Recording' } }
        next_cursor: { type: [string, 'null'] }

    Segment:
      type: object
      properties:
        idx:        { type: integer }
        start:      { type: number }
        end:        { type: number }
        text:       { type: string }
        speaker:    { type: [string, 'null'] }
        confidence: { type: number, minimum: 0, maximum: 1 }
    TranscriptPage:
      type: object
      properties:
        language:    { type: string }
        items:       { type: array, items: { $ref: '#/components/schemas/Segment' } }
        next_cursor: { type: [string, 'null'] }
    ExportLink:
      type: object
      properties:
        format:     { type: string }
        url:        { type: string, format: uri }
        expires_at: { type: string, format: date-time }
    SearchResult:
      type: object
      properties:
        items:
          type: array
          items:
            type: object
            properties:
              recording_id: { type: string, format: uuid }
              segment:      { $ref: '#/components/schemas/Segment' }
              snippet:      { type: string }
```

**Не показано у фрагменті, але входить до контракту:** `auth/refresh`, `auth/logout`, `users/me`, `projects/{id}` (GET/PATCH/DELETE), `recordings/{id}/retry`, `/cancel`, `/summary`, `/exports` (POST), `health/*`, `/metrics` і WebSocket `/ws/recordings/{id}` (OpenAPI не описує WS, тому формат повідомлень задано в розділі 1.5; для нього можна додати AsyncAPI).
