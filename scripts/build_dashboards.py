"""Generates the Grafana dashboards in deploy/grafana/dashboards.

Run after changing a definition:  uv run python scripts/build_dashboards.py
The generated JSON files are committed; Grafana loads them through provisioning.
"""

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parents[1] / "deploy" / "grafana" / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}

Target = tuple[str, str]  # (PromQL expression, legend)

HTTP = "scribe_http_request_duration_seconds"
STAGE = "scribe_pipeline_stage_duration_seconds"
CONSUMER = "scribe_consumer_handler_duration_seconds"
MAIN_QUEUES = (
    'queue=~"celery.pipeline|pipeline.start|progress.events|recording.cleanup|webhook.delivery"'
)


def quantile(q: float, metric: str, by: str = "", selector: str = "", window: str = "5m") -> str:
    group = f"le, {by}" if by else "le"
    return f"histogram_quantile({q}, sum by ({group}) (rate({metric}_bucket{selector}[{window}])))"


def panel(
    pid: int,
    title: str,
    targets: list[Target],
    *,
    x: int,
    y: int,
    w: int = 12,
    h: int = 8,
    unit: str = "short",
    kind: str = "timeseries",
) -> dict[str, Any]:
    return {
        "id": pid,
        "type": kind,
        "title": title,
        "datasource": PROM,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [
            {"refId": chr(ord("A") + i), "datasource": PROM, "expr": expr, "legendFormat": legend}
            for i, (expr, legend) in enumerate(targets)
        ],
    }


def dashboard(uid: str, title: str, panels: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "tags": ["scribe"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "10s",
        "time": {"from": "now-30m", "to": "now"},
        "panels": panels,
    }


def api() -> dict[str, Any]:
    return dashboard(
        "scribe-api",
        "Scribe: API",
        [
            panel(1, "Requests per second by status", [
                ("sum by (status) (rate(scribe_http_requests_total[1m]))", "{{status}}"),
            ], x=0, y=0, unit="reqps"),
            panel(2, "5xx ratio (5m)", [
                ('sum(rate(scribe_http_requests_total{status=~"5.."}[5m])) '
                 "/ sum(rate(scribe_http_requests_total[5m]))", "5xx"),
            ], x=12, y=0, w=6, unit="percentunit", kind="stat"),
            panel(3, "Open WebSocket connections", [
                ("sum(scribe_ws_connections)", "connections"),
            ], x=18, y=0, w=6, kind="stat"),
            panel(4, "Latency", [
                (quantile(0.5, HTTP), "p50"),
                (quantile(0.95, HTTP), "p95"),
                (quantile(0.99, HTTP), "p99"),
            ], x=0, y=8, unit="s"),
            panel(5, "p95 latency by handler", [
                (quantile(0.95, HTTP, by="handler"), "{{handler}}"),
            ], x=12, y=8, unit="s"),
            panel(6, "Gateway: requests per second by status code", [
                ('sum by (code) (rate(traefik_entrypoint_requests_total{entrypoint="web"}[1m]))',
                 "{{code}}"),
            ], x=0, y=16, unit="reqps"),
            panel(7, "Gateway: rate limited requests per second (429)", [
                ('sum(rate(traefik_entrypoint_requests_total{entrypoint="web",code="429"}[1m]))',
                 "429"),
            ], x=12, y=16, unit="reqps"),
        ],
    )  # fmt: skip


def pipeline() -> dict[str, Any]:
    return dashboard(
        "scribe-pipeline",
        "Scribe: pipeline and messaging",
        [
            panel(1, "Stage duration p95 (completed runs)", [
                (quantile(0.95, STAGE, by="stage", selector='{outcome="completed"}', window="15m"),
                 "{{stage}}"),
            ], x=0, y=0, unit="s"),
            panel(2, "Stage runs per second by outcome", [
                ("sum by (outcome) (rate(scribe_pipeline_stage_runs_total[5m]))", "{{outcome}}"),
            ], x=12, y=0, unit="ops"),
            panel(3, "Queue depth (ready messages)", [
                (f"sum by (queue) (rabbitmq_detailed_queue_messages_ready{{{MAIN_QUEUES}}})",
                 "{{queue}}"),
            ], x=0, y=8),
            panel(4, "Dead-letter queues", [
                ('sum by (queue) (rabbitmq_detailed_queue_messages_ready{queue=~".+dlq"})',
                 "{{queue}}"),
            ], x=12, y=8, w=6),
            panel(5, "Retry queues", [
                ('sum by (queue) (rabbitmq_detailed_queue_messages_ready{queue=~".+retry.+"})',
                 "{{queue}}"),
            ], x=18, y=8, w=6),
            panel(6, "Outbox", [
                ("scribe_outbox_unpublished_events", "unpublished events"),
                ("scribe_outbox_lag_seconds", "lag, seconds"),
            ], x=0, y=16),
            panel(7, "Outbox relay", [
                ("rate(scribe_outbox_published_total[1m])", "published/s"),
                ("rate(scribe_outbox_publish_errors_total[1m])", "errors/s"),
            ], x=12, y=16, unit="ops"),
            panel(8, "Consumer messages per second", [
                ("sum by (queue, outcome) (rate(scribe_consumer_messages_total[5m]))",
                 "{{queue}} {{outcome}}"),
            ], x=0, y=24, unit="ops"),
            panel(9, "Inbox duplicates (1h)", [
                ("sum(increase(scribe_inbox_duplicates_total[1h]))", "duplicates"),
            ], x=12, y=24, w=6, kind="stat"),
            panel(10, "Consumer handler duration p95", [
                (quantile(0.95, CONSUMER, by="queue"), "{{queue}}"),
            ], x=18, y=24, w=6, unit="s"),
        ],
    )  # fmt: skip


def infrastructure() -> dict[str, Any]:
    return dashboard(
        "scribe-infra",
        "Scribe: infrastructure",
        [
            panel(1, "Scrape targets (1 = up)", [("up", "{{job}}")], x=0, y=0),
            panel(2, "CPU by process group", [
                ("sum by (job) (rate(process_cpu_seconds_total[1m]))", "{{job}}"),
            ], x=12, y=0, unit="percentunit"),
            panel(3, "Resident memory", [
                ("sum by (job) (process_resident_memory_bytes)", "{{job}}"),
            ], x=0, y=8, unit="bytes"),
            panel(4, "RabbitMQ: unacknowledged messages", [
                (f"sum by (queue) (rabbitmq_detailed_queue_messages_unacked{{{MAIN_QUEUES}}})",
                 "{{queue}}"),
            ], x=12, y=8),
            panel(5, "Gateway: 5xx per second", [
                ('sum(rate(traefik_entrypoint_requests_total{entrypoint="web",code=~"5.."}[1m]))',
                 "5xx"),
            ], x=0, y=16, unit="reqps"),
        ],
    )  # fmt: skip


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in (("api", api), ("pipeline", pipeline), ("infrastructure", infrastructure)):
        (OUT / f"{name}.json").write_text(json.dumps(build(), indent=2) + "\n")
        print(f"wrote {name}.json")


if __name__ == "__main__":
    main()
