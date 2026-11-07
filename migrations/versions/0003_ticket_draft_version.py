"""Draft version on tickets (optimistic check when the operator approves).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column(
            "draft_version", sa.Integer, nullable=False, server_default="0"
        ),
    )
    # Existing drafts become version 1, so forms rendered after the upgrade can approve them.
    op.execute(
        "UPDATE tickets SET draft_version = 1 WHERE draft_text IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("tickets", "draft_version")
