import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.helpers import alembic_config

pytestmark = pytest.mark.integration


async def test_upgrade_check_downgrade_upgrade(postgres_url: str) -> None:
    admin = create_async_engine(postgres_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text("CREATE DATABASE migration_check"))
    await admin.dispose()

    url = (
        make_url(postgres_url).set(database="migration_check").render_as_string(hide_password=False)
    )
    cfg = alembic_config(url)

    await asyncio.to_thread(command.upgrade, cfg, "head")
    await asyncio.to_thread(command.check, cfg)

    engine = create_async_engine(url)
    async with engine.connect() as conn:
        names = (await conn.execute(text("SELECT name FROM plans ORDER BY id"))).scalars().all()
    await engine.dispose()
    assert names == ["free", "pro", "business"]

    await asyncio.to_thread(command.downgrade, cfg, "base")
    await asyncio.to_thread(command.upgrade, cfg, "head")
