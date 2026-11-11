from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

from scribe.application.use_cases.auth import TokenPair
from scribe.domain.users import User


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email_length(cls, v: str) -> str:
        if len(v) > 254:
            raise ValueError("email must be at most 254 characters")
        return v

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        if not any(c.isalpha() for c in v) or not any(c.isdigit() for c in v):
            raise ValueError("password must contain at least one letter and one digit")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int

    @classmethod
    def from_domain(cls, pair: TokenPair) -> Self:
        return cls(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            expires_in=pair.expires_in,
        )


class UserResponse(BaseModel):
    id: UUID
    email: str
    plan: str
    created_at: datetime

    @classmethod
    def from_domain(cls, user: User) -> Self:
        return cls(id=user.id, email=user.email, plan=user.plan.name, created_at=user.created_at)


class MeResponse(BaseModel):
    id: UUID
    email: str
    plan: str
    created_at: datetime
    monthly_minutes: int
    max_file_mb: int
    minutes_used: int

    @classmethod
    def build(cls, user: User, minutes_used: int) -> Self:
        return cls(
            id=user.id,
            email=user.email,
            plan=user.plan.name,
            created_at=user.created_at,
            monthly_minutes=user.plan.monthly_minutes,
            max_file_mb=user.plan.max_file_mb,
            minutes_used=minutes_used,
        )
