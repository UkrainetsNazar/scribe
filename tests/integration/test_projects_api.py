import httpx
import pytest

from tests.integration.helpers import API, auth_headers

pytestmark = pytest.mark.integration


async def _create(
    client: httpx.AsyncClient, headers: dict[str, str], name: str, description: str | None = None
) -> httpx.Response:
    return await client.post(
        f"{API}/projects", json={"name": name, "description": description}, headers=headers
    )


async def test_project_crud(api_client: httpx.AsyncClient) -> None:
    headers = await auth_headers(api_client)

    resp = await _create(api_client, headers, "  Podcasts ", "weekly shows")
    assert resp.status_code == 201
    project = resp.json()
    assert project["name"] == "Podcasts"
    project_id = project["id"]

    resp = await api_client.get(f"{API}/projects/{project_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["description"] == "weekly shows"

    resp = await api_client.patch(
        f"{API}/projects/{project_id}", json={"name": "Lectures"}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Lectures"
    assert resp.json()["description"] == "weekly shows"

    resp = await api_client.patch(
        f"{API}/projects/{project_id}", json={"description": None}, headers=headers
    )
    assert resp.json()["description"] is None

    assert (
        await api_client.delete(f"{API}/projects/{project_id}", headers=headers)
    ).status_code == 204
    assert (
        await api_client.get(f"{API}/projects/{project_id}", headers=headers)
    ).status_code == 404


async def test_duplicate_name_conflicts_but_is_reusable_after_delete(
    api_client: httpx.AsyncClient,
) -> None:
    headers = await auth_headers(api_client)
    first = (await _create(api_client, headers, "Same")).json()

    resp = await _create(api_client, headers, "Same")
    assert resp.status_code == 409
    assert resp.json()["code"] == "PROJECT_NAME_TAKEN"

    await api_client.delete(f"{API}/projects/{first['id']}", headers=headers)
    assert (await _create(api_client, headers, "Same")).status_code == 201

    other = await auth_headers(api_client)
    assert (await _create(api_client, other, "Same")).status_code == 201


async def test_projects_are_private(api_client: httpx.AsyncClient) -> None:
    owner = await auth_headers(api_client)
    stranger = await auth_headers(api_client)
    project = (await _create(api_client, owner, "Mine")).json()

    for method in ("get", "delete"):
        resp = await getattr(api_client, method)(
            f"{API}/projects/{project['id']}", headers=stranger
        )
        assert resp.status_code == 404
    resp = await api_client.get(f"{API}/projects", headers=stranger)
    assert resp.json()["items"] == []


async def test_validation_and_auth(api_client: httpx.AsyncClient) -> None:
    headers = await auth_headers(api_client)
    resp = await _create(api_client, headers, "   ")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"
    assert (await api_client.get(f"{API}/projects")).status_code == 401


async def test_pagination(api_client: httpx.AsyncClient) -> None:
    headers = await auth_headers(api_client)
    for name in ("p1", "p2", "p3"):
        assert (await _create(api_client, headers, name)).status_code == 201

    first = (await api_client.get(f"{API}/projects?limit=2", headers=headers)).json()
    assert [p["name"] for p in first["items"]] == ["p3", "p2"]  # newest first
    assert first["next_cursor"]

    second = (
        await api_client.get(
            f"{API}/projects?limit=2&cursor={first['next_cursor']}", headers=headers
        )
    ).json()
    assert [p["name"] for p in second["items"]] == ["p1"]
    assert second["next_cursor"] is None

    resp = await api_client.get(f"{API}/projects?cursor=garbage", headers=headers)
    assert resp.status_code == 400
    assert (await api_client.get(f"{API}/projects?limit=0", headers=headers)).status_code == 422
