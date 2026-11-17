"""Load test for Scribe, run against the gateway.

    uv run locust -f loadtests/locustfile.py --headless -u 20 -r 1 -t 2m \
        --host http://localhost:8010 --csv load-results

Every virtual user registers, processes one short recording, then reads. The verdict (exit code)
covers the steady-state requests only: p95 below SLO_P95_MS and a failure ratio below
MAX_FAIL_RATIO. Requests named "setup: ..." (register, login, upload, complete-upload, polling) and
"export: ..." are printed but not judged: registering hashes a password with argon2id and
complete-upload runs ffprobe synchronously, so both are expected to be slower than 200 ms.

The gateway limits requests per client IP, and every user here comes from one IP: a low spawn
rate (-r 1) keeps the registrations under the auth limit, and throttled setup calls are retried
after Retry-After.
"""

import io
import os
import time
import uuid
import wave
from typing import Any

from locust import HttpUser, between, events, task
from locust.env import Environment

SLO_P95_MS = float(os.environ.get("SLO_P95_MS", "200"))
MAX_FAIL_RATIO = float(os.environ.get("MAX_FAIL_RATIO", "0.01"))
MIN_SAMPLES = 20  # a percentile of fewer requests is not meaningful
NOT_JUDGED = ("setup:", "export:")
API = "/api/v1"
PASSWORD = "load-test-password-1"  # noqa: S105
TOKEN_MAX_AGE_SEC = 600  # access tokens live 15 minutes


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
        self._register_and_log_in()
        project = self._call(
            "POST", "/projects", "setup: create project", expect=(201,), json={"name": "Load"}
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
        retry_throttle: bool = False,
        **kwargs: Any,
    ) -> Any:
        absolute = path.startswith("http")  # a presigned URL: no prefix, no Authorization header
        for _ in range(60 if retry_throttle else 1):
            headers = {} if absolute or not self._access else self._headers()
            url = path if absolute else f"{API}{path}"
            with self.client.request(
                method, url, name=name, headers=headers, catch_response=True, **kwargs
            ) as response:
                if retry_throttle and response.status_code == 429:
                    response.success()  # expected: wait as the gateway asks and try again
                    time.sleep(float(response.headers.get("Retry-After", "1")))
                    continue
                if response.status_code in expect:
                    response.success()
                else:
                    response.failure(f"HTTP {response.status_code}: {response.text[:200]}")
                return response
        raise RuntimeError(f"{name}: still rate limited")

    def _headers(self) -> dict[str, str]:
        if time.monotonic() - self._token_time > TOKEN_MAX_AGE_SEC:
            self._refresh()
        return {"Authorization": f"Bearer {self._access}"}

    def _store_tokens(self, body: dict[str, Any]) -> None:
        self._access = body["access_token"]
        self._refresh_token = body["refresh_token"]
        self._token_time = time.monotonic()

    def _register_and_log_in(self) -> None:
        credentials = {"email": self.email, "password": PASSWORD}
        self._call(
            "POST", "/auth/register", "setup: register", expect=(201,),
            retry_throttle=True, json=credentials,
        )  # fmt: skip
        login = self._call(
            "POST", "/auth/login", "setup: login", retry_throttle=True, json=credentials
        )
        self._store_tokens(login.json())

    def _refresh(self) -> None:
        response = self._call(
            "POST", "/auth/refresh", "setup: refresh token", retry_throttle=True,
            json={"refresh_token": self._refresh_token},
        )  # fmt: skip
        self._store_tokens(response.json())

    def _process_one_recording(self) -> str:
        data = silent_wav()
        created = self._call(
            "POST", "/recordings", "setup: create recording", expect=(201,),
            json={
                "project_id": self.project_id,
                "title": "Load recording",
                "filename": "load.wav",
                "content_type": "audio/wav",
                "size_bytes": len(data),
            },
        ).json()  # fmt: skip
        recording_id = created["recording"]["id"]
        self._call("PUT", created["upload_url"], "setup: upload to MinIO", data=data)
        self._call(
            "POST", f"/recordings/{recording_id}/complete-upload",
            "setup: complete-upload", expect=(202,),
        )  # fmt: skip
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status = self._call(
                "GET", f"/recordings/{recording_id}", "setup: poll recording"
            ).json()["status"]
            if status == "completed":
                return str(recording_id)
            if status in ("failed", "cancelled"):
                raise RuntimeError(f"recording {recording_id} ended as {status}")
            time.sleep(1)
        raise RuntimeError(f"recording {recording_id} did not finish in time")

    # ── steady-state tasks (what the SLO is judged on) ───────────────────────

    @task(5)
    def me(self) -> None:
        self._call("GET", "/users/me", "GET /users/me")

    @task(5)
    def list_projects(self) -> None:
        self._call("GET", "/projects", "GET /projects")

    @task(2)
    def create_project(self) -> None:
        self._call(
            "POST", "/projects", "POST /projects", expect=(201,),
            json={"name": f"load-{uuid.uuid4().hex[:10]}"},
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
    def create_recording(self) -> None:
        self._call(
            "POST", "/recordings", "POST /recordings", expect=(201,),
            json={
                "project_id": self.project_id,
                "title": "Never uploaded",
                "filename": "x.wav",
                "content_type": "audio/wav",
                "size_bytes": 1024,
            },
        )  # fmt: skip

    @task(1)
    def export(self) -> None:
        base = f"/recordings/{self.recording_id}/exports"
        self._call("POST", base, "export: create", expect=(202,), json={"format": "srt"})
        self._call("GET", f"{base}/srt", "export: link")


@events.quitting.add_listener
def judge(environment: Environment, **_: Any) -> None:
    print(f"\n{'request':<44}{'n':>7}{'fail':>7}{'p95 ms':>9}{'p99 ms':>9}  verdict")
    judged = failed = 0
    problems: list[str] = []
    for (name, method), entry in sorted(environment.stats.entries.items()):
        p95 = float(entry.get_response_time_percentile(0.95) or 0)
        p99 = float(entry.get_response_time_percentile(0.99) or 0)
        if name.startswith(NOT_JUDGED):
            verdict = "not judged"
        elif entry.num_requests < MIN_SAMPLES:
            verdict = "too few requests"
            judged += entry.num_requests
            failed += entry.num_failures
        else:
            judged += entry.num_requests
            failed += entry.num_failures
            verdict = "ok" if p95 <= SLO_P95_MS else f"SLOW (> {SLO_P95_MS:.0f} ms)"
            if p95 > SLO_P95_MS:
                problems.append(f"{method} {name}: p95 {p95:.0f} ms")
        print(
            f"{method + ' ' + name:<44.44}{entry.num_requests:>7}{entry.num_failures:>7}"
            f"{p95:>9.0f}{p99:>9.0f}  {verdict}"
        )

    if judged == 0:
        problems.append("no steady-state request was made (did the users fail to start?)")
    elif failed / judged > MAX_FAIL_RATIO:
        problems.append(f"failure ratio {failed / judged:.2%} exceeds {MAX_FAIL_RATIO:.2%}")

    print(f"\nverdict: {'FAILED' if problems else 'passed'}")
    for problem in problems:
        print(f"  - {problem}")
    environment.process_exit_code = 1 if problems else 0
