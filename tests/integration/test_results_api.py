import json
from dataclasses import dataclass

import httpx
import pytest

from tests.integration.helpers import API, auth_headers
from tests.integration.test_pipeline import _owner_id, _run_pipeline, requires_ffmpeg
from tests.integration.test_recordings_api import _recording_with_upload, make_wav

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Finished:
    headers: dict[str, str]
    recording_id: str
    project_id: str


@pytest.fixture
async def finished(api_client: httpx.AsyncClient, pipeline_env: None) -> Finished:
    """A recording that went through the whole pipeline (stub transcriber, real everything else)."""
    headers = await auth_headers(api_client)
    owner_id = await _owner_id(api_client, headers)
    recording_id = await _recording_with_upload(api_client, headers, make_wav(seconds=6))
    resp = await api_client.post(
        f"{API}/recordings/{recording_id}/complete-upload", headers=headers
    )
    assert resp.status_code == 202
    await _run_pipeline(recording_id, owner_id, run=1)

    detail = (await api_client.get(f"{API}/recordings/{recording_id}", headers=headers)).json()
    assert detail["status"] == "completed"
    return Finished(headers, recording_id, detail["project_id"])


async def _transcript(
    client: httpx.AsyncClient, done: Finished, query: str = ""
) -> dict[str, object]:
    resp = await client.get(
        f"{API}/recordings/{done.recording_id}/transcript{query}", headers=done.headers
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


@requires_ffmpeg
async def test_transcript_pagination_and_start_filter(
    api_client: httpx.AsyncClient, finished: Finished
) -> None:
    everything = await _transcript(api_client, finished)
    items = everything["items"]
    assert everything["language"] == "en"
    assert everything["next_cursor"] is None
    assert len(items) >= 2  # type: ignore[arg-type]
    assert [i["idx"] for i in items] == list(range(len(items)))  # type: ignore[arg-type,index]
    assert items[0]["text"] == "Stub transcript segment 1."  # type: ignore[index]

    first = await _transcript(api_client, finished, "?limit=1")
    assert [i["idx"] for i in first["items"]] == [0]  # type: ignore[attr-defined]
    assert first["next_cursor"]
    second = await _transcript(api_client, finished, f"?limit=1&cursor={first['next_cursor']}")
    assert [i["idx"] for i in second["items"]] == [1]  # type: ignore[attr-defined]

    last = items[-1]  # type: ignore[index]
    tail = await _transcript(api_client, finished, f"?from={last['start'] + 0.01}")
    assert [i["idx"] for i in tail["items"]] == [last["idx"]]  # type: ignore[attr-defined]

    bad = await api_client.get(
        f"{API}/recordings/{finished.recording_id}/transcript?cursor=garbage",
        headers=finished.headers,
    )
    assert bad.status_code == 400
    assert (
        await api_client.get(
            f"{API}/recordings/{finished.recording_id}/transcript?from=-1", headers=finished.headers
        )
    ).status_code == 422


@requires_ffmpeg
async def test_summary(api_client: httpx.AsyncClient, finished: Finished) -> None:
    resp = await api_client.get(
        f"{API}/recordings/{finished.recording_id}/summary", headers=finished.headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "extractive-v1"
    assert body["summary"]
    assert "transcript" in body["topics"]


async def test_results_are_not_available_before_completion(api_client: httpx.AsyncClient) -> None:
    from tests.integration.test_recordings_api import _create_recording, _project

    headers = await auth_headers(api_client)
    project_id = await _project(api_client, headers)
    created = (await _create_recording(api_client, headers, project_id, 1024)).json()
    base = f"{API}/recordings/{created['recording']['id']}"

    for resp in (
        await api_client.get(f"{base}/transcript", headers=headers),
        await api_client.get(f"{base}/summary", headers=headers),
        await api_client.post(f"{base}/exports", json={"format": "txt"}, headers=headers),
    ):
        assert resp.status_code == 409
        assert resp.json()["code"] == "INVALID_STATE"


@requires_ffmpeg
async def test_results_are_private(api_client: httpx.AsyncClient, finished: Finished) -> None:
    stranger = await auth_headers(api_client)
    base = f"{API}/recordings/{finished.recording_id}"
    for resp in (
        await api_client.get(f"{base}/transcript", headers=stranger),
        await api_client.get(f"{base}/summary", headers=stranger),
        await api_client.post(f"{base}/exports", json={"format": "txt"}, headers=stranger),
        await api_client.get(f"{base}/exports/txt", headers=stranger),
    ):
        assert resp.status_code == 404


@requires_ffmpeg
async def test_search(api_client: httpx.AsyncClient, finished: Finished) -> None:
    total = len((await _transcript(api_client, finished))["items"])  # type: ignore[arg-type]

    resp = await api_client.get(f"{API}/search?q=segment", headers=finished.headers)
    assert resp.status_code == 200
    hits = resp.json()["items"]
    assert len(hits) == total
    for hit in hits:
        assert hit["recording_id"] == finished.recording_id
        assert "segment" in hit["snippet"]
        assert hit["segment"]["end"] > hit["segment"]["start"]  # timecodes are present
    assert sorted(h["segment"]["idx"] for h in hits) == list(range(total))

    in_project = await api_client.get(
        f"{API}/search?q=segment&project_id={finished.project_id}", headers=finished.headers
    )
    assert len(in_project.json()["items"]) == total
    assert (
        len(
            (
                await api_client.get(f"{API}/search?q=segment&limit=1", headers=finished.headers)
            ).json()["items"]
        )
        == 1
    )
    nothing = await api_client.get(f"{API}/search?q=zzzzzz", headers=finished.headers)
    assert nothing.json()["items"] == []

    other_project = await api_client.post(
        f"{API}/projects", json={"name": "Other"}, headers=finished.headers
    )
    empty = await api_client.get(
        f"{API}/search?q=segment&project_id={other_project.json()['id']}", headers=finished.headers
    )
    assert empty.json()["items"] == []

    assert (await api_client.get(f"{API}/search?q=a", headers=finished.headers)).status_code == 422
    stranger = await auth_headers(api_client)
    assert (await api_client.get(f"{API}/search?q=segment", headers=stranger)).json()["items"] == []
    foreign = await api_client.get(
        f"{API}/search?q=segment&project_id={finished.project_id}", headers=stranger
    )
    assert foreign.status_code == 404

    deleted = await api_client.delete(
        f"{API}/recordings/{finished.recording_id}", headers=finished.headers
    )
    assert deleted.status_code == 204
    after = await api_client.get(f"{API}/search?q=segment", headers=finished.headers)
    assert after.json()["items"] == []  # the Mongo documents stay, the search ignores them


@requires_ffmpeg
@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        ("txt", "[00:00:00] Stub transcript segment 1."),
        ("srt", " --> "),
        ("vtt", "WEBVTT"),
        ("json", '"segments"'),
    ],
)
async def test_export_download(
    api_client: httpx.AsyncClient, finished: Finished, fmt: str, expected: str
) -> None:
    base = f"{API}/recordings/{finished.recording_id}/exports"
    assert (await api_client.get(f"{base}/{fmt}", headers=finished.headers)).status_code == 404

    for _ in range(2):  # idempotent
        resp = await api_client.post(base, json={"format": fmt}, headers=finished.headers)
        assert resp.status_code == 202
        assert resp.json() == {"format": fmt, "status": "ready"}

    link = await api_client.get(f"{base}/{fmt}", headers=finished.headers)
    assert link.status_code == 200
    assert link.json()["format"] == fmt

    async with httpx.AsyncClient() as http:  # straight from MinIO: the API does not proxy files
        download = await http.get(link.json()["url"])
    assert download.status_code == 200
    assert expected in download.text
    assert f"Standup.{fmt}" in download.headers["content-disposition"]


@requires_ffmpeg
async def test_json_export_content(api_client: httpx.AsyncClient, finished: Finished) -> None:
    base = f"{API}/recordings/{finished.recording_id}/exports"
    await api_client.post(base, json={"format": "json"}, headers=finished.headers)
    link = (await api_client.get(f"{base}/json", headers=finished.headers)).json()
    async with httpx.AsyncClient() as http:
        data = json.loads((await http.get(link["url"])).text)
    assert data["recording_id"] == finished.recording_id
    assert (data["title"], data["language"]) == ("Standup", "en")
    assert data["segments"][0]["text"] == "Stub transcript segment 1."


async def test_unknown_export_format_is_rejected(api_client: httpx.AsyncClient) -> None:
    headers = await auth_headers(api_client)
    base = f"{API}/recordings/00000000-0000-0000-0000-000000000000/exports"
    assert (await api_client.post(base, json={"format": "pdf"}, headers=headers)).status_code == 422
    assert (await api_client.get(f"{base}/pdf", headers=headers)).status_code == 422
