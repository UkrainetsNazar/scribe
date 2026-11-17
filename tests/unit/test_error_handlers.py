import httpx


async def test_malformed_json_is_a_bad_request(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login", content="{", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "BAD_REQUEST"
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_valid_json_with_invalid_fields_is_still_a_validation_error(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post("/api/v1/auth/login", json={"email": "not-an-email"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"
