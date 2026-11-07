"""Per-claim lease token on jobs (fencing that survives re-enqueueing).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs", sa.Column("lease_token", postgresql.UUID(as_uuid=True))
    )


def downgrade() -> None:
    op.drop_column("jobs", "lease_token")
