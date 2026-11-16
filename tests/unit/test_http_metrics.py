import httpx
from prometheus_client import REGISTRY

from scribe.presentation.http_metrics import handler_label


def _count(handler: str, status: str, method: str = "GET") -> float:
    labels = {"method": method, "handler": handler, "status": status}
    return REGISTRY.get_sample_value("scribe_http_requests_total", labels) or 0.0


def test_handler_labels_are_low_cardinality() -> None:
    uid = "11111111-1111-1111-1111-111111111111"
    assert (
        handler_label(f"/api/v1/recordings/{uid}/transcript", 200)
        == "/api/v1/recordings/{id}/transcript"
    )
    assert handler_label("/api/v1/projects/not-a-uuid", 422) == "/api/v1/projects/{id}"
    assert (
        handler_label(f"/api/v1/recordings/{uid}/exports/pdf", 422)
        == "/api/v1/recordings/{id}/exports/{format}"
    )
    assert handler_label("/api/v1/users/me", 200) == "/api/v1/users/me"
    assert handler_label("/api/v1/whatever", 404) == "unmatched"
    assert handler_label("/random", 200) == "other"


async def test_requests_are_counted_by_normalized_handler(client: httpx.AsyncClient) -> None:
    uid = "11111111-1111-1111-1111-111111111111"
    before = _count("/api/v1/recordings/{id}", "401")
    await client.get(f"/api/v1/recordings/{uid}")  # no token: 401
    assert _count("/api/v1/recordings/{id}", "401") == before + 1


async def test_unknown_paths_share_one_label(client: httpx.AsyncClient) -> None:
    before = _count("unmatched", "404")
    await client.get("/api/v1/nope-1")
    await client.get("/nope-2")
    assert _count("unmatched", "404") == before + 2


async def test_metrics_endpoint_skips_itself_and_health_checks(client: httpx.AsyncClient) -> None:
    await client.get("/health/live")
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "scribe_http_requests_total" in resp.text
    assert 'handler="/health/live"' not in resp.text
    assert 'handler="/metrics"' not in resp.text
