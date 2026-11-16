from prometheus_client import Counter, Gauge, Histogram

HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1.0, 2.5, 5.0, 10.0)

# ── API ──
HTTP_REQUESTS = Counter(
    "scribe_http_requests_total",
    "HTTP requests handled by the API",
    ["method", "handler", "status"],
)
HTTP_DURATION = Histogram(
    "scribe_http_request_duration_seconds",
    "HTTP request duration",
    ["method", "handler"],
    buckets=HTTP_BUCKETS,
)
WS_CONNECTIONS = Gauge("scribe_ws_connections", "Open WebSocket connections")

# ── Outbox relay ──
OUTBOX_UNPUBLISHED = Gauge(
    "scribe_outbox_unpublished_events", "Outbox events waiting to be published"
)
OUTBOX_LAG = Gauge(
    "scribe_outbox_lag_seconds", "Age of the oldest unpublished outbox event (0 when none)"
)
OUTBOX_PUBLISHED = Counter("scribe_outbox_published_total", "Outbox events published to the broker")
OUTBOX_PUBLISH_ERRORS = Counter(
    "scribe_outbox_publish_errors_total", "Failed attempts to publish an outbox event"
)

# ── Consumers ──
CONSUMER_MESSAGES = Counter(
    "scribe_consumer_messages_total",
    "Messages settled by a consumer (processed, duplicate, retried, dead_lettered)",
    ["queue", "outcome"],
)
INBOX_DUPLICATES = Counter(
    "scribe_inbox_duplicates_total", "Messages skipped by the inbox as duplicates", ["consumer"]
)
CONSUMER_DURATION = Histogram(
    "scribe_consumer_handler_duration_seconds",
    "Time to process one message",
    ["queue"],
    buckets=(0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0),
)

# ── Pipeline (Celery workers, multiprocess mode) ──
STAGE_RUNS = Counter(
    "scribe_pipeline_stage_runs_total",
    "Executions of a pipeline stage by outcome",
    ["stage", "outcome"],
)
STAGE_DURATION = Histogram(
    "scribe_pipeline_stage_duration_seconds",
    "Duration of a pipeline stage execution",
    ["stage", "outcome"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200),
)
