# Webhooks

A project can notify your system when one of its recordings finishes (`recording.completed`) or
fails (`recording.failed`).

## Setup

```bash
curl -X PUT $API/projects/$PROJECT/webhook -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/hooks/scribe"}'
# {"url": "https://example.com/hooks/scribe", "secret": "whsec_..."}
```

The secret is shown only by `PUT` and by `POST .../webhook/rotate-secret`; keep it safe. Changing
the URL keeps the secret. `DELETE .../webhook` turns the notifications off. The URL must be a public
`http(s)` address without credentials; private, loopback and link-local destinations are refused.

To try webhooks against a receiver on your machine set WEBHOOK_ALLOW_PRIVATE_NETWORKS=true in .env and recreate the consumer

## The request

`POST` with a JSON body, redirects are not followed, any 2xx answer counts as delivered:

```json
{
  "id": "0d1d0c2e-...",
  "type": "recording.completed",
  "created_at": "2026-11-17T10:15:30.123456+00:00",
  "data": {
    "recording_id": "...", "project_id": "...", "title": "Weekly sync",
    "status": "completed", "run": 1, "duration_sec": 312.4, "language": "en"
  }
}
```

For `recording.failed`, `data` also has `stage` and `error`.

| Header | Meaning |
| --- | --- |
| `X-Scribe-Event` | the event type |
| `X-Scribe-Delivery` | the event id, identical in every retry: deduplicate by it |
| `X-Scribe-Signature` | `t=<unix seconds>,v1=<hex>` |

## Verifying the signature

`v1` is `HMAC-SHA256(secret, "<t>." + raw request body)` in hex. Compute it over the raw bytes
(before any JSON parsing), compare in constant time and reject timestamps that are older than a few
minutes.

```python
import hashlib, hmac, time


def verify(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp, candidate = int(parts["t"]), parts["v1"]
    except (ValueError, KeyError):
        return False
    if abs(time.time() - timestamp) > tolerance:
        return False
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(candidate, expected)
```

`scripts/webhook_receiver.py` is a ready-made receiver for experiments.

## Retries

A failed attempt (network error, timeout, non-2xx answer) is retried after 5 s, 30 s, 2 min and
10 min. After that the message goes to the `webhook.delivery.dlq` queue and the
`DeadLetterQueueNotEmpty` alert fires. Delivery is at-least-once: a receiver may see the same
`X-Scribe-Delivery` more than once.