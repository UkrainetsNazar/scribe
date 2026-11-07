import uuid

import httpx
import pytest

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-1"
API = "/api/v1"


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:10]}@example.com"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(
    client: httpx.AsyncClient, email: str, password: str = PASSWORD
) -> httpx.Response:
    return await client.post(f"{API}/auth/register", json={"email": email, "password": password})


async def _login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    return await client.post(f"{API}/auth/login", json={"email": email, "password": password})


async def _registered_tokens(client: httpx.AsyncClient) -> tuple[str, dict[str, str]]:
    email = _email()
    assert (await _register(client, email)).status_code == 201
    resp = await _login(client, email)
    assert resp.status_code == 200
    return email, resp.json()


async def test_register_login_me(api_client: httpx.AsyncClient) -> None:
    email = _email()
    resp = await _register(api_client, email)
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == email
    assert body["plan"] == "free"
    assert "password" not in body
    assert "password_hash" not in body

    resp = await _login(api_client, email)
    assert resp.status_code == 200
    tokens = resp.json()
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == 900

    me = await api_client.get(f"{API}/users/me", headers=_auth(tokens["access_token"]))
    assert me.status_code == 200
    assert me.json()["email"] == email
    assert me.json()["monthly_minutes"] == 300


async def test_email_is_case_insensitive(api_client: httpx.AsyncClient) -> None:
    email = _email()
    await _register(api_client, email.upper())
    assert (await _login(api_client, email)).status_code == 200


async def test_duplicate_email_returns_problem_json(api_client: httpx.AsyncClient) -> None:
    email = _email()
    assert (await _register(api_client, email)).status_code == 201
    resp = await _register(api_client, email.upper())
    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["code"] == "EMAIL_TAKEN"
    assert body["status"] == 409
    assert body["trace_id"] == resp.headers["x-trace-id"]


@pytest.mark.parametrize("password", ["short1", "onlyletterspassword", "1234567890123"])
async def test_weak_password_is_rejected(api_client: httpx.AsyncClient, password: str) -> None:
    resp = await _register(api_client, _email(), password)
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["errors"]
    assert "password" in body["errors"][0]["loc"]


async def test_login_with_wrong_credentials(api_client: httpx.AsyncClient) -> None:
    email = _email()
    await _register(api_client, email)
    for resp in (
        await _login(api_client, email, "wrong-password-1"),
        await _login(api_client, _email()),
    ):
        assert resp.status_code == 401
        assert resp.json()["code"] == "UNAUTHORIZED"


async def test_me_requires_valid_access_token(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.get(f"{API}/users/me")).status_code == 401
    assert (await api_client.get(f"{API}/users/me", headers=_auth("garbage"))).status_code == 401

    _, tokens = await _registered_tokens(api_client)
    resp = await api_client.get(f"{API}/users/me", headers=_auth(tokens["refresh_token"]))
    assert resp.status_code == 401


async def test_refresh_rotates_and_rejects_reuse(api_client: httpx.AsyncClient) -> None:
    _, first = await _registered_tokens(api_client)

    resp = await api_client.post(
        f"{API}/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert resp.status_code == 200
    second = resp.json()
    assert second["refresh_token"] != first["refresh_token"]

    reuse = await api_client.post(
        f"{API}/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert reuse.status_code == 401

    me = await api_client.get(f"{API}/users/me", headers=_auth(second["access_token"]))
    assert me.status_code == 200


async def test_logout_revokes_refresh_token(api_client: httpx.AsyncClient) -> None:
    _, tokens = await _registered_tokens(api_client)

    resp = await api_client.post(
        f"{API}/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert resp.status_code == 204

    resp = await api_client.post(
        f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert resp.status_code == 401


async def test_unknown_route_is_problem_json(api_client: httpx.AsyncClient) -> None:
    resp = await api_client.get("/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == "NOT_FOUND"
