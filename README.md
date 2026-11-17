# Scribe

Upload an audio or video recording and get a transcript, subtitles (`.srt`/`.vtt`), a summary and
full-text search with timecodes. Processing is asynchronous and its progress is streamed over
WebSocket in real time.

```text
upload ─► validate ─► extract audio ─► normalize + chunk ─► transcribe ─► subtitles ─► summary ─► index
```

Stack: FastAPI, SQLAlchemy 2 (async) + Alembic, PostgreSQL, MongoDB, Redis, RabbitMQ, Celery,
MinIO, FFmpeg, faster-whisper, Traefik, Prometheus, Grafana, Jaeger (OpenTelemetry).
Patterns: Unit of Work, Domain Events, Transactional Outbox, idempotent consumers (Inbox).

Documentation: [architecture and diagrams](docs/architecture.md) · [webhooks](docs/webhooks.md).

## Quick start

Requirements: Docker (Compose v2), [uv](https://docs.astral.sh/uv/), `openssl`.

```bash
git clone <repository-url> scribe && cd scribe
cp .env.example .env
echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env

docker compose up --build -d
until curl -fsS localhost:8010/health/ready; do sleep 2; done

uv run python scripts/demo.py
```

The first build takes a few minutes. The demo registers a user, uploads a generated tone, shows the
live progress, then prints the transcript, the summary and a search result, downloads all four
exports into `demo-output/` and prints links to the trace in Jaeger and to the dashboards.

Everything is reachable only on `127.0.0.1`:

| What | URL | Login |
| --- | --- | --- |
| API through the gateway (Swagger UI at `/docs`) | http://localhost:8010 | register through the API |
| Grafana (dashboards in the *Scribe* folder) | http://localhost:3030 | anonymous viewer, `admin`/`admin` to edit |
| Jaeger (traces) | http://localhost:16686 | |
| Prometheus (targets, alerts) | http://localhost:9090 | |
| Traefik dashboard | http://localhost:8080/dashboard/ | |
| RabbitMQ management | http://localhost:15672 | `scribe` / `scribe` |
| MinIO console | http://localhost:9101 | `scribe` / `scribe-secret` |

PostgreSQL `localhost:5433` (`scribe`/`scribe`, database `scribe`), MongoDB `localhost:27017`,
Redis `localhost:6380`, MinIO API `localhost:9100` (presigned URLs point there).

If a port is taken by another project, change only the left part of the mapping in
`docker-compose.yml` (and `MINIO_PUBLIC_ENDPOINT` if you move MinIO).

### Real speech recognition

The default transcriber is a stub that returns placeholder text. For real transcription put this
into `.env` and rebuild the worker (the first run downloads the model, ~460 MB for `small`):

```dotenv
TRANSCRIBER=whisper
WORKER_EXTRAS="--extra whisper"
```

```bash
docker compose up -d --build worker
```

## Using the API

Every request goes through the gateway: `Authorization: Bearer <access token>`, JSON errors follow
RFC 7807 (`application/problem+json`). Interactive documentation: http://localhost:8010/docs.

| Step | Request |
| --- | --- |
| Register, log in | `POST /api/v1/auth/register`, `/auth/login`, `/auth/refresh`, `/auth/logout` |
| Project | `POST/GET /api/v1/projects`, `GET/PATCH/DELETE /projects/{id}` |
| Recording | `POST /api/v1/recordings` returns a presigned `upload_url` |
| Upload | `PUT` the file to `upload_url` (straight to MinIO, the API never proxies bytes) |
| Start processing | `POST /api/v1/recordings/{id}/complete-upload` |
| Progress | WebSocket `ws://localhost:8010/ws/recordings/{id}?token=<access token>` |
| Control | `POST /recordings/{id}/retry`, `/cancel`, `DELETE /recordings/{id}` |
| Results | `GET /recordings/{id}/transcript`, `/summary`, `GET /api/v1/search?q=` |
| Exports | `POST /recordings/{id}/exports` `{"format": "txt\|srt\|vtt\|json"}`, then `GET /recordings/{id}/exports/{format}` |
| Webhooks | `PUT/GET/DELETE /projects/{id}/webhook`, `POST /projects/{id}/webhook/rotate-secret` |

WebSocket messages: `status`, `progress` (`stage`, `percent`, `overall`), `segment`, `completed`,
`failed`, `cancelled`. After a final message the server closes the connection; reconnecting returns
the last state.

## Development

```bash
uv sync --all-extras --dev       # add --extra whisper only if you run the real transcriber
uv run ruff format . && uv run ruff check .
uv run mypy src
uv run pytest tests/unit         # fast, no Docker
uv run pytest tests/integration  # testcontainers: needs Docker and ffmpeg (brew install ffmpeg)
```

End-to-end tests run against a live stack (`TRANSCRIBER=stub`):

```bash
docker compose up -d --build
uv run pytest tests/e2e -m e2e -v -s
```

Load test (Locust, against the gateway; the verdict is p95 < 200 ms and < 1% failures):

```bash
uv run locust -f loadtests/locustfile.py --headless -u 20 -r 1 -t 2m \
  --host http://localhost:8010 --csv load-results
# or with a web UI at http://localhost:8089:  uv run locust -f loadtests/locustfile.py --host http://localhost:8010
```

Database migrations (Alembic) are applied by the `migrate` service on every `docker compose up`.
To create one after changing the models:

```bash
uv run alembic revision --autogenerate -m "describe the change"   # then read the generated file
uv run alembic upgrade head && uv run alembic check
```

Grafana dashboards are generated code: edit `scripts/build_dashboards.py`, run it, commit the JSON.

## Configuration

Settings come from environment variables (see `src/scribe/config.py`; `.env` is read for local runs).

| Variable | Default | Meaning |
| --- | --- | --- |
| `JWT_SECRET` | none, required | at least 32 characters; the process refuses to start with the public development value unless `APP_ENV=test` |
| `TRANSCRIBER` | `whisper` (compose: `stub`) | `stub` or `whisper` |
| `WHISPER_MODEL` / `WHISPER_DEVICE` | `small` / `auto` | faster-whisper model and device |
| `CHUNK_SECONDS` | `600` | length of the audio chunks that are transcribed |
| `PIPELINE_MAX_RETRIES` | `5` | retries of a pipeline stage on transient errors (exponential backoff with jitter) |
| `RABBITMQ_RETRY_DELAYS_SEC` | `[5,30,120,600]` | consumer retry tiers before the dead-letter queue |
| `ACCESS_TOKEN_TTL_MIN` / `REFRESH_TOKEN_TTL_DAYS` | `15` / `7` | token lifetimes |
| `PRESIGNED_URL_TTL_MIN` | `15` | lifetime of upload and download URLs |
| `OTLP_ENDPOINT` | unset | OTLP/HTTP endpoint (for example `http://jaeger:4318`); tracing is off when unset |
| `WEBHOOK_ALLOW_PRIVATE_NETWORKS` | `false` (compose: `true`) | must stay `false` in production (SSRF protection) |
| `APP_ROLE` | `all` | `api`, `ws` or `all`: which routes a process serves |

Plans (`free` 300 min/month, `pro`, `business`) live in the `plans` table.

## Operations

| Alert | Meaning | First steps |
| --- | --- | --- |
| `DeadLetterQueueNotEmpty` | a message failed all retries | inspect `*.dlq` in RabbitMQ (header `x-dead-letter-reason`), fix the cause, move or purge the message |
| `OutboxLagHigh` | the oldest unpublished event is older than 60 s | `docker compose logs relay`, check RabbitMQ; events are not lost, they wait in PostgreSQL |
| `PipelineStageFailing` | a recording moved to `failed` | the stage and the error are in the recording (`stages`) and in the worker log; `POST /retry` resumes from the failed stage |
| `TargetDown` | Prometheus cannot scrape a service | `docker compose ps`, `docker compose logs <service>` |
| `ApiErrorRateHigh` | more than 5% of requests answer 5xx | find the trace of a failing request (the `X-Trace-Id` header) in Jaeger |

Every response carries `X-Trace-Id`, the same id as in Jaeger, in the logs (`trace_id`) and in
error bodies. `/health/live` is a liveness probe, `/health/ready` checks the dependencies of the
process.

## Repository layout

```text
src/scribe/
  domain/          entities, state machine, events, quota rules (no framework imports)
  application/     use cases, ports, event handlers, the stage runner
  infrastructure/  PostgreSQL, MongoDB, Redis, MinIO, RabbitMQ, FFmpeg, Celery, telemetry
  presentation/    FastAPI routers, schemas, error handlers, middleware, WebSocket
  workers/         entry points of the relay, the consumer and the Celery worker
deploy/            Traefik, Prometheus, Grafana, RabbitMQ plugin configuration
migrations/        Alembic
tests/             unit, integration (testcontainers), e2e (live stack)
loadtests/         Locust
scripts/           demo, webhook receiver, dashboard generator
```

## Known limitations

- A presigned PUT cannot restrict size or content type, so both are checked after the upload,
  and the validated file is copied to a private key before it is processed.
- Cancelling is noticed at the next progress report; a running FFmpeg or Whisper call finishes.
- Deleting a project does not delete its recordings; deleting a recording removes its files,
  documents and rows asynchronously.
- Webhook secrets are stored unencrypted and rotating a secret has no grace period.
- The gateway's own errors (429, unrouted paths, 5xx) are plain text, not RFC 7807.
- No per-user rate limit (only per client IP) and no LLM summaries (the summary is extractive).
- Not implemented: speaker diarization, a GPU profile for the worker.
- Grafana allows anonymous viewing and several services publish ports: a development setup.