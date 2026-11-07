"""Initial schema: tickets, jobs, kb_articles, llm_request_log.

Revision ID: 0001
Revises:
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TICKET_STATUSES = (
    "new",
    "draft_ready",
    "approved",
    "sent",
    "rejected",
    "generation_failed",
    "send_failed",
)
JOB_KINDS = ("generate_draft", "send_reply")
JOB_STATUSES = ("queued", "running", "done", "failed")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(
    name: str, *, nullable: bool = True, default: str | None = None
) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.text(default) if default else None,
    )


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("event_id", sa.String(255), nullable=False, unique=True),
        sa.Column("external_ticket_id", sa.String(255), nullable=False),
        sa.Column("customer_email", sa.String(320), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("reply_url", sa.Text, nullable=False),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default="new"
        ),
        sa.Column("draft_text", sa.Text),
        sa.Column(
            "draft_sources",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("draft_model", sa.String(100)),
        _ts("draft_generated_at"),
        sa.Column("reply_text", sa.Text),
        sa.Column("reply_idempotency_key", postgresql.UUID(as_uuid=True)),
        sa.Column("rejection_reason", sa.Text),
        sa.Column("last_error", sa.Text),
        _ts("approved_at"),
        _ts("rejected_at"),
        _ts("sent_at"),
        _ts("created_at", nullable=False, default="now()"),
        _ts("updated_at", nullable=False, default="now()"),
        sa.CheckConstraint(
            _one_of("status", TICKET_STATUSES), name="tickets_status_check"
        ),
    )
    op.create_index(
        "ix_tickets_external_ticket_id", "tickets", ["external_ticket_id"]
    )
    op.create_index("ix_tickets_status", "tickets", ["status"])
    op.create_index("ix_tickets_created_at", "tickets", ["created_at"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "ticket_id",
            sa.BigInteger,
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="queued"
        ),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False),
        _ts("available_at", nullable=False, default="now()"),
        sa.Column("last_error", sa.Text),
        _ts("created_at", nullable=False, default="now()"),
        _ts("updated_at", nullable=False, default="now()"),
        sa.UniqueConstraint("ticket_id", "kind", name="jobs_ticket_kind_key"),
        sa.CheckConstraint(_one_of("kind", JOB_KINDS), name="jobs_kind_check"),
        sa.CheckConstraint(
            _one_of("status", JOB_STATUSES), name="jobs_status_check"
        ),
    )
    op.create_index(
        "jobs_claim_idx",
        "jobs",
        ["kind", "available_at"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "kb_articles",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("slug", sa.String(200), nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("title_terms", sa.Text, nullable=False, server_default=""),
        sa.Column("body_terms", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR,
            sa.Computed(
                "setweight(to_tsvector('simple'::regconfig, title_terms), 'A') || "
                "setweight(to_tsvector('simple'::regconfig, body_terms), 'B')",
                persisted=True,
            ),
        ),
        _ts("updated_at", nullable=False, default="now()"),
    )
    op.create_index(
        "kb_articles_search_idx",
        "kb_articles",
        ["search_vector"],
        postgresql_using="gin",
    )

    op.create_table(
        "llm_request_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        _ts("created_at", nullable=False, default="clock_timestamp()"),
    )
    op.create_index(
        "ix_llm_request_log_created_at", "llm_request_log", ["created_at"]
    )


def downgrade() -> None:
    op.drop_table("llm_request_log")
    op.drop_table("kb_articles")
    op.drop_table("jobs")
    op.drop_table("tickets")
