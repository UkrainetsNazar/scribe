from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scribe.application.interfaces import PlanRepository, UserRepository
from scribe.domain.errors import EmailTakenError
from scribe.domain.users import Plan, User
from scribe.infrastructure.db.models import PlanModel, UserModel


def _plan_to_domain(m: PlanModel) -> Plan:
    return Plan(
        id=m.id,
        name=m.name,
        monthly_minutes=m.monthly_minutes,
        max_file_mb=m.max_file_mb,
    )


def _user_to_domain(m: UserModel) -> User:
    return User(
        id=m.id,
        email=m.email,
        password_hash=m.password_hash,
        plan=_plan_to_domain(m.plan),
        is_active=m.is_active,
        created_at=m.created_at,
    )


class SqlAlchemyPlanRepository(PlanRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_name(self, name: str) -> Plan | None:
        model = (
            await self._session.execute(select(PlanModel).where(PlanModel.name == name))
        ).scalar_one_or_none()
        return _plan_to_domain(model) if model else None


class SqlAlchemyUserRepository(UserRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, user: User) -> None:
        self._session.add(
            UserModel(
                id=user.id,
                email=user.email,
                password_hash=user.password_hash,
                plan_id=user.plan.id,
                is_active=user.is_active,
                created_at=user.created_at,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise EmailTakenError from exc

    async def get_by_id(self, user_id: UUID) -> User | None:
        model = (
            await self._session.execute(select(UserModel).where(UserModel.id == user_id))
        ).scalar_one_or_none()
        return _user_to_domain(model) if model else None

    async def get_by_email(self, email: str) -> User | None:
        model = (
            await self._session.execute(select(UserModel).where(UserModel.email == email))
        ).scalar_one_or_none()
        return _user_to_domain(model) if model else None
