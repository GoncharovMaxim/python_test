import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TicketStatus(StrEnum):
    NEW = "new"  # waiting for a draft (queued or generating)
    DRAFT_READY = "draft_ready"  # waiting for the operator
    APPROVED = "approved"  # operator approved; reply is queued / being sent
    SENT = "sent"  # helpdesk accepted the reply (2xx)
    REJECTED = "rejected"  # operator rejected the draft
    GENERATION_FAILED = "generation_failed"  # LLM failed permanently / retries exhausted
    SEND_FAILED = "send_failed"  # helpdesk failed permanently / retries exhausted


class JobKind(StrEnum):
    GENERATE_DRAFT = "generate_draft"
    SEND_REPLY = "send_reply"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _one_of(column: str, values: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


class Ticket(Base):
    """One incoming helpdesk event (deduplicated by `event_id`) and its reply lifecycle."""

    __tablename__ = "tickets"
    __table_args__ = (CheckConstraint(_one_of("status", TicketStatus), name="tickets_status_check"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(255), unique=True)
    external_ticket_id: Mapped[str] = mapped_column(String(255), index=True)
    customer_email: Mapped[str] = mapped_column(String(320))
    message: Mapped[str] = mapped_column(Text)
    reply_url: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(32), default=TicketStatus.NEW, index=True)

    draft_text: Mapped[str | None] = mapped_column(Text)
    # [{"slug": ..., "title": ..., "url": ...}] - articles the draft links to
    draft_sources: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    draft_model: Mapped[str | None] = mapped_column(String(100))
    draft_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    reply_text: Mapped[str | None] = mapped_column(Text)
    # Created once at approval and reused for every delivery attempt.
    reply_idempotency_key: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)

    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Job(Base):
    """Durable work queue. One job per (ticket, kind); re-enqueueing resets a finished job.

    `available_at` means "not before" for queued jobs and "lease expires" for running ones,
    so a job held by a crashed worker becomes claimable again automatically.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("ticket_id", "kind", name="jobs_ticket_kind_key"),
        CheckConstraint(_one_of("kind", JobKind), name="jobs_kind_check"),
        CheckConstraint(_one_of("status", JobStatus), name="jobs_status_check"),
        Index(
            "jobs_claim_idx",
            "kind",
            "available_at",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    ticket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tickets.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class KbArticle(Base):
    __tablename__ = "kb_articles"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    slug: Mapped[str] = mapped_column(String(200), unique=True)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    # Normalised terms (see app/kb/text.py); Postgres has no Ukrainian stemmer.
    title_terms: Mapped[str] = mapped_column(Text, default="", server_default="")
    body_terms: Mapped[str] = mapped_column(Text, default="", server_default="")
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('simple'::regconfig, title_terms), 'A') || "
            "setweight(to_tsvector('simple'::regconfig, body_terms), 'B')",
            persisted=True,
        ),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("kb_articles_search_idx", "search_vector", postgresql_using="gin"),)


class LlmRequestLog(Base):
    """One row per LLM request slot; the rate limiter counts rows in a sliding window."""

    __tablename__ = "llm_request_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), index=True
    )
