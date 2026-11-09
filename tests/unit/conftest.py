import pytest

from scribe.application.interfaces import UnitOfWork, UnitOfWorkFactory
from tests.unit.fakes import FakeDb, FakeUnitOfWork


@pytest.fixture
def db() -> FakeDb:
    return FakeDb()


@pytest.fixture
def uow_factory(db: FakeDb) -> UnitOfWorkFactory:
    def factory() -> UnitOfWork:
        return FakeUnitOfWork(db)

    return factory
