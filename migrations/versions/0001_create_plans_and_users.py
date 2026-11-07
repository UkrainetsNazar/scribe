"""create plans and users

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    plans = op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("monthly_minutes", sa.Integer(), nullable=False),
        sa.Column("max_file_mb", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plans")),
        sa.UniqueConstraint("name", name=op.f("uq_plans_name")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], name=op.f("fk_users_plan_id_plans")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.bulk_insert(
        plans,
        [
            {"name": "free", "monthly_minutes": 300, "max_file_mb": 500},
            {"name": "pro", "monthly_minutes": 3000, "max_file_mb": 2048},
            {"name": "business", "monthly_minutes": 12000, "max_file_mb": 2048},
        ],
    )


def downgrade() -> None:
    op.drop_table("users")
    op.drop_table("plans")
