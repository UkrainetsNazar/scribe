"""Load test for Scribe, run against the gateway.

    uv run locust -f loadtests/locustfile.py --headless -u 20 -r 1 -t 3m \
        --host http://localhost:8010 --csv load-results

Every virtual user registers, processes one short recording, then reads and writes.

Verdict (the exit code): the NFR "p95 < 200 ms for every endpoint except export" is judged on ALL
requests that go through the API, the failure ratio must stay below MAX_FAIL_RATIO, and every
judged request needs at least MIN_SAMPLES samples (use at least 20 users and 3 minutes).
Not judged: "export:" (excluded by the spec) and "storage:" (straight to MinIO, not the API).

All virtual users share one IP and the gateway limits /auth/* per IP, so auth requests are paced by
a token bucket below that limit. A throttled request never happens, and so never distorts the
statistics. Locust rounds percentiles above 100 ms to two significant digits.
"""

import io
import os
import time
import uuid
import wave
from typing import Any

from gevent.lock import Semaphore
from locust import HttpUser, between, events, task
from locust.env import Environment

SLO_P95_MS = float(os.environ.get("SLO_P95_MS", "200"))
MAX_FAIL_RATIO = float(os.environ.get("MAX_FAIL_RATIO", "0.01"))
MIN_SAMPLES = int(os.environ.get("MIN_SAMPLES", "20"))
NOT_JUDGED = ("export:", "storage:")
API = "/api/v1"
PASSWORD = "load-test-password-1"  # noqa: S105
TOKEN_MAX_AGE_SEC = 600  # access tokens live 15 minutes


class TokenBucket:
    """A token bucket for gevent greenlets (Locust users are greenlets of one process)."""

    def __init__(self, capacity: int, rate_per_sec: float) -> None:
        self._capacity = float(capacity)
        self._rate = rate_per_sec
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = Semaphore()

    def take(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                time.sleep((1.0 - self._tokens) / self._rate)


# The gateway allows 60 auth requests per minute with a burst of 20
AUTH_BUCKET = TokenBucket(capacity=12, rate_per_sec=0.9)


def silent_wav(seconds: int = 4, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * rate * seconds)
    return buffer.getvalue()


class ScribeUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self) -> None:
        self.email = f"load-{uuid.uuid4().hex[:12]}@example.com"
        self._access = ""
        self._refresh_token = ""
        self._token_time = 0.0
        credentials = {"email": self.email, "password": PASSWORD}
        AUTH_BUCKET.take()
        self._call(
            "POST", "/auth/register", "POST /auth/register", expect=(201,), auth=False,
            json=credentials,
        )  # fmt: skip
        AUTH_BUCKET.take()
        login = self._call("POST", "/auth/login", "POST /auth/login", auth=False, json=credentials)
        self._store_tokens(login.json())
        project = self._call(
            "POST", "/projects", "POST /projects", expect=(201,), json={"name": "Load"}
        )
        self.project_id = project.json()["id"]
        self.recording_id = self._process_one_recording()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _call(
        self,
        method: str,
        path: str,
        name: str,
        *,
        expect: tuple[int, ...] = (200,),
        auth: bool = True,
        **kwargs: Any,
    ) -> Any:
        absolute = path.startswith("http")  # a presigned URL: no prefix, no Authorization header
        headers = self._headers() if auth and not absolute else {}
        url = path if absolute else f"{API}{path}"
        with self.client.request(
            method, url, name=name, headers=headers, catch_response=True, **kwargs
        ) as response:
            if response.status_code in expect:
                response.success()
            else:
                response.failure(f"HTTP {response.status_code}: {response.text[:200]}")
            return response

    def _headers(self) -> dict[str, str]:
        if time.monotonic() - self._token_time > TOKEN_MAX_AGE_SEC:
            self._refresh()
        return {"Authorization": f"Bearer {self._access}"}

    def _store_tokens(self, body: dict[str, Any]) -> None:
        self._access = body["access_token"]
        self._refresh_token = body["refresh_token"]
        self._token_time = time.monotonic()

    def _refresh(self) -> None:
        AUTH_BUCKET.take()
        response = self._call(
            "POST", "/auth/refresh", "POST /auth/refresh", auth=False,
            json={"refresh_token": self._refresh_token},
        )  # fmt: skip
        if response.status_code == 200:
            self._store_tokens(response.json())

    def _process_one_recording(self) -> str:
        data = silent_wav()
        created = self._call(
            "POST", "/recordings", "POST /recordings", expect=(201,),
            json={
                "project_id": self.project_id,
                "title": "Load recording",
                "filename": "load.wav",
                "content_type": "audio/wav",
                "size_bytes": len(data),
            },
        ).json()  # fmt: skip
        recording_id = created["recording"]["id"]
        self._call("PUT", created["upload_url"], "storage: upload to MinIO", data=data)
        self._call(
            "POST", f"/recordings/{recording_id}/complete-upload",
            "POST /recordings/{id}/complete-upload", expect=(202,),
        )  # fmt: skip
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status = self._call(
                "GET", f"/recordings/{recording_id}", "GET /recordings/{id}"
            ).json()["status"]
            if status == "completed":
                return str(recording_id)
            if status in ("failed", "cancelled"):
                raise RuntimeError(f"recording {recording_id} ended as {status}")
            time.sleep(1)
        raise RuntimeError(f"recording {recording_id} did not finish in time")

    # ── steady-state tasks ───────────────────────────────────────────────────

    @task(5)
    def me(self) -> None:
        self._call("GET", "/users/me", "GET /users/me")

    @task(1)
    def refresh_token(self) -> None:
        self._refresh()

    @task(5)
    def list_projects(self) -> None:
        self._call("GET", "/projects", "GET /projects")

    @task(2)
    def create_project(self) -> None:
        self._call(
            "POST", "/projects", "POST /projects", expect=(201,),
            json={"name": f"load-{uuid.uuid4().hex[:10]}"},
        )  # fmt: skip

    @task(2)
    def update_project(self) -> None:
        self._call(
            "PATCH", f"/projects/{self.project_id}", "PATCH /projects/{id}",
            json={"description": uuid.uuid4().hex[:12]},
        )  # fmt: skip

    @task(1)
    def delete_project(self) -> None:
        created = self._call(
            "POST", "/projects", "POST /projects", expect=(201,),
            json={"name": f"tmp-{uuid.uuid4().hex[:10]}"},
        )  # fmt: skip
        if created.status_code == 201:
            self._call(
                "DELETE", f"/projects/{created.json()['id']}", "DELETE /projects/{id}",
                expect=(204,),
            )  # fmt: skip

    @task(4)
    def list_recordings(self) -> None:
        self._call("GET", "/recordings?limit=20", "GET /recordings")

    @task(4)
    def get_recording(self) -> None:
        self._call("GET", f"/recordings/{self.recording_id}", "GET /recordings/{id}")

    @task(4)
    def transcript(self) -> None:
        self._call(
            "GET", f"/recordings/{self.recording_id}/transcript?limit=50",
            "GET /recordings/{id}/transcript",
        )  # fmt: skip

    @task(2)
    def summary(self) -> None:
        self._call(
            "GET", f"/recordings/{self.recording_id}/summary", "GET /recordings/{id}/summary"
        )

    @task(4)
    def search(self) -> None:
        self._call("GET", "/search?q=stub", "GET /search")

    @task(1)
    def create_and_delete_recording(self) -> None:
        created = self._call(
            "POST", "/recordings", "POST /recordings", expect=(201,),
            json={
                "project_id": self.project_id,
                "title": "Never uploaded",
                "filename": "x.wav",
                "content_type": "audio/wav",
                "size_bytes": 1024,
            },
        )  # fmt: skip
        if created.status_code == 201:
            self._call(
                "DELETE", f"/recordings/{created.json()['recording']['id']}",
                "DELETE /recordings/{id}", expect=(204,),
            )  # fmt: skip

    @task(1)
    def webhook(self) -> None:
        path = f"/projects/{self.project_id}/webhook"
        self._call(
            "PUT", path, "PUT /projects/{id}/webhook", json={"url": "https://example.com/hook"}
        )
        self._call("GET", path, "GET /projects/{id}/webhook")

    @task(1)
    def export(self) -> None:
        base = f"/recordings/{self.recording_id}/exports"
        self._call("POST", base, "export: create", expect=(202,), json={"format": "srt"})
        self._call("GET", f"{base}/srt", "export: link")


@events.quitting.add_listener
def judge(environment: Environment, **_: Any) -> None:
    print(f"\n{'request':<46}{'n':>7}{'fail':>7}{'p95 ms':>9}{'p99 ms':>9}  verdict")
    judged = failed = 0
    problems: list[str] = []
    for (name, _method), entry in sorted(environment.stats.entries.items()):
        p95 = float(entry.get_response_time_percentile(0.95) or 0)
        p99 = float(entry.get_response_time_percentile(0.99) or 0)
        if name.startswith(NOT_JUDGED):
            verdict = "not judged"
        else:
            judged += entry.num_requests
            failed += entry.num_failures
            if entry.num_requests < MIN_SAMPLES:
                verdict = f"TOO FEW ({entry.num_requests} < {MIN_SAMPLES})"
                problems.append(f"{name}: only {entry.num_requests} samples")
            elif p95 >= SLO_P95_MS:
                verdict = f"SLOW (>= {SLO_P95_MS:.0f} ms)"
                problems.append(f"{name}: p95 {p95:.0f} ms")
            else:
                verdict = "ok"
        print(
            f"{name:<46.46}{entry.num_requests:>7}{entry.num_failures:>7}{p95:>9.0f}{p99:>9.0f}  {verdict}"  # noqa: E501
        )

    if judged == 0:
        problems.append("no API request was made (did the users fail to start?)")
    elif failed / judged > MAX_FAIL_RATIO:
        problems.append(f"failure ratio {failed / judged:.2%} exceeds {MAX_FAIL_RATIO:.2%}")

    print(f"\nverdict: {'FAILED' if problems else 'passed'}")
    for problem in problems:
        print(f"  - {problem}")
    environment.process_exit_code = 1 if problems else 0
