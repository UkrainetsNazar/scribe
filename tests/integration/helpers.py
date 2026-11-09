import uuid
from pathlib import Path

import httpx
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]
API = "/api/v1"
PASSWORD = "correct-horse-1"


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


async def auth_headers(client: httpx.AsyncClient) -> dict[str, str]:
    email = f"user-{uuid.uuid4().hex[:10]}@example.com"
    await client.post(f"{API}/auth/register", json={"email": email, "password": PASSWORD})
    resp = await client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}
