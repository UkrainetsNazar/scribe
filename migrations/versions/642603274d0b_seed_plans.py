"""seed plans

Revision ID: 642603274d0b
Revises: a2208c9dc35d
Create Date: 2026-11-14 11:21:56.185634

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "642603274d0b"
down_revision: str | Sequence[str] | None = "a2208c9dc35d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Reference data the app cannot work without: the default plan is used on registration.
    # Idempotent, so it is safe on a database that already has the rows.
    op.execute(
        """
        INSERT INTO plans (name, monthly_minutes, max_file_mb) VALUES
            ('free', 300, 500),
            ('pro', 3000, 2048),
            ('business', 12000, 2048)
        ON CONFLICT (name) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM plans WHERE name IN ('free', 'pro', 'business')")
