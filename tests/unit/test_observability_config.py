import json
import re
from pathlib import Path

import yaml
from prometheus_client import REGISTRY

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
DASHBOARDS = sorted((DEPLOY / "grafana" / "dashboards").glob("*.json"))


def test_dashboards_and_alerts_use_only_metrics_the_code_defines() -> None:
    import scribe.infrastructure.metrics  # noqa: F401  (registers the metrics)

    families = {family.name for family in REGISTRY.collect()}
    texts = [p.read_text() for p in DASHBOARDS] + [
        (DEPLOY / "prometheus" / "alerts.yml").read_text()
    ]
    used = {name for text in texts for name in re.findall(r"\bscribe_[a-z0-9_]+", text)}
    assert used
    unknown = {n for n in used if re.sub(r"_(total|bucket|sum|count)$", "", n) not in families}
    assert not unknown, f"unknown metrics: {sorted(unknown)}"


def test_dashboards_are_generated_and_well_formed() -> None:
    assert {p.stem for p in DASHBOARDS} == {"api", "pipeline", "infrastructure"}
    for path in DASHBOARDS:
        dashboard = json.loads(path.read_text())
        ids = [panel["id"] for panel in dashboard["panels"]]
        assert len(ids) == len(set(ids)), path.name
        assert all(panel["targets"] for panel in dashboard["panels"]), path.name


def test_alerts_target_existing_jobs_and_are_documented() -> None:
    config = yaml.safe_load((DEPLOY / "prometheus" / "prometheus.yml").read_text())
    jobs = {job["job_name"] for job in config["scrape_configs"]}
    alerts_text = (DEPLOY / "prometheus" / "alerts.yml").read_text()
    for selector in re.findall(r'job=~?"([^"]+)"', alerts_text):
        assert set(selector.split("|")) <= jobs, selector

    rules = [r for group in yaml.safe_load(alerts_text)["groups"] for r in group["rules"]]
    assert {r["alert"] for r in rules} >= {"DeadLetterQueueNotEmpty", "OutboxLagHigh"}
    for rule in rules:
        assert rule["labels"]["severity"], rule["alert"]
        assert rule["annotations"]["summary"], rule["alert"]
