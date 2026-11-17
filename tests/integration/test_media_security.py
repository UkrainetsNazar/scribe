import httpx
import pytest

from tests.integration.helpers import API, auth_headers
from tests.integration.test_recordings_api import _recording_with_upload, requires_ffprobe
from tests.integration.test_webhooks import Receiver, receiver  # noqa: F401

pytestmark = pytest.mark.integration


@requires_ffprobe
async def test_a_playlist_cannot_make_the_server_fetch_urls(
    api_client: httpx.AsyncClient,
    receiver: Receiver,  # noqa: F811
) -> None:
    target = receiver.url.replace("/hook", "/private.ts")
    playlist = (
        f"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\n{target}\n"
        "#EXT-X-ENDLIST\n"
    ).encode()
    headers = await auth_headers(api_client)
    recording_id = await _recording_with_upload(api_client, headers, playlist)  # declared audio/wav

    resp = await api_client.post(
        f"{API}/recordings/{recording_id}/complete-upload", headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "INVALID_MEDIA"
    assert receiver.requests == []  # nothing was fetched from the URL inside the file
