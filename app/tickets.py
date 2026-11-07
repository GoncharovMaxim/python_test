"""Ticket use-cases shared by the webhook, the operator API and the UI.

Every status transition is a conditional UPDATE (`WHERE status = <expected>`), so double
clicks, concurrent operators and late workers can't move a ticket through a step twice.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import queue
from app.models import JobKind, Ticket, TicketStatus

if t.TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import Settings


class TicketNotFoundError(LookupError):
    pass


class InvalidTransitionError(RuntimeError):
    def __init__(self, ticket_id: int, action: str, status: str) -> None:
        super().__init__(
            f"cannot {action} ticket {ticket_id} in status '{status}'"
        )
        self.ticket_id = ticket_id
        self.action = action
        self.status = status


class StaleDraftError(InvalidTransitionError):
    """The operator acted on a draft that has been replaced since they loaded it."""

    def __init__(
        self, ticket_id: int, action: str, status: str, seen: int, current: int
    ) -> None:
        super().__init__(ticket_id, action, status)
        self.args = (
            f"cannot {action} ticket {ticket_id}: draft version {seen} is outdated "
            f"(current version is {current}), reload the ticket",
        )
        self.seen_version = seen
        self.current_version = current


@dataclass(frozen=True)
class IncomingEvent:
    event_id: str
    ticket_id: str
    customer_email: str
    message: str
    reply_url: str


async def ingest_event(
    session: AsyncSession, event: IncomingEvent, settings: Settings
) -> bool:
    """Store the ticket and its generation job atomically. Returns False for duplicates.

    Concurrent deliveries of the same event serialize on the unique index: the second one
    waits for the first to commit and then inserts nothing.
    """
    # The caller enforces the total deadline client-side (incl. waiting for a connection);
    # this makes Postgres abandon the work too, instead of finishing it for nobody.
    timeout_ms = max(int(settings.webhook_timeout_seconds * 1000), 1)
    await session.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
    stmt = (
        pg_insert(Ticket)
        .values(
            event_id=event.event_id,
            external_ticket_id=event.ticket_id,
            customer_email=event.customer_email,
            message=event.message,
            reply_url=event.reply_url,
            status=TicketStatus.NEW,
        )
        .on_conflict_do_nothing(index_elements=[Ticket.event_id])
        .returning(Ticket.id)
    )
    ticket_id = (await session.execute(stmt)).scalar_one_or_none()
    if ticket_id is not None:
        await queue.enqueue(
            session,
            kind=JobKind.GENERATE_DRAFT,
            ticket_id=ticket_id,
            max_attempts=settings.generation_max_attempts,
        )

    await session.commit()
    return ticket_id is not None


async def _transition(
    session: AsyncSession,
    ticket_id: int,
    action: str,
    expected: TicketStatus | tuple[TicketStatus, ...],
    values: dict,
    draft_version: int | None = None,
) -> None:
    """Conditional UPDATE. With `draft_version`, it also requires the draft the caller saw."""
    allowed = expected if isinstance(expected, tuple) else (expected,)
    conditions = [Ticket.id == ticket_id, Ticket.status.in_(allowed)]
    if draft_version is not None:
        conditions.append(Ticket.draft_version == draft_version)

    result = await session.execute(
        update(Ticket)
        .where(*conditions)
        .values(updated_at=func.now(), **values)
        .returning(Ticket.id)
    )
    if result.scalar_one_or_none() is None:
        await session.rollback()
        row = (
            await session.execute(
                select(Ticket.status, Ticket.draft_version).where(
                    Ticket.id == ticket_id
                )
            )
        ).first()
        if row is None:
            raise TicketNotFoundError(ticket_id)

        if row.status in allowed and draft_version is not None:
            raise StaleDraftError(
                ticket_id, action, row.status, draft_version, row.draft_version
            )

        raise InvalidTransitionError(ticket_id, action, row.status)


async def approve(
    session: AsyncSession,
    ticket_id: int,
    settings: Settings,
    text_override: str | None = None,
    draft_version: int | None = None,
) -> None:
    """Approve the draft (optionally edited by the operator) and queue it for sending.

    `draft_version` is the version the operator was looking at. If the draft was regenerated
    since, approval fails with `StaleDraftError` instead of sending text based on the old draft.
    """
    reply_text = text_override.strip() if text_override is not None else None
    if text_override is not None and not reply_text:
        raise ValueError("reply text must not be empty")

    await _transition(
        session,
        ticket_id,
        "approve",
        TicketStatus.DRAFT_READY,
        {
            "status": TicketStatus.APPROVED,
            "reply_text": reply_text
            if reply_text is not None
            else Ticket.draft_text,
            # One key per reply, kept for every retry: the helpdesk dedups on it.
            "reply_idempotency_key": func.coalesce(
                Ticket.reply_idempotency_key, func.gen_random_uuid()
            ),
            "approved_at": func.now(),
            "last_error": None,
        },
        draft_version=draft_version,
    )
    await queue.enqueue(
        session,
        kind=JobKind.SEND_REPLY,
        ticket_id=ticket_id,
        max_attempts=settings.send_max_attempts,
    )
    await session.commit()


async def reject(
    session: AsyncSession, ticket_id: int, reason: str | None = None
) -> None:
    await _transition(
        session,
        ticket_id,
        "reject",
        TicketStatus.DRAFT_READY,
        {
            "status": TicketStatus.REJECTED,
            "rejection_reason": (reason or "").strip() or None,
            "rejected_at": func.now(),
        },
    )
    await session.commit()


async def regenerate(
    session: AsyncSession, ticket_id: int, settings: Settings
) -> None:
    """Ask for a fresh draft. Not allowed once a reply was approved (it may be delivered)."""
    await _transition(
        session,
        ticket_id,
        "regenerate",
        (
            TicketStatus.DRAFT_READY,
            TicketStatus.REJECTED,
            TicketStatus.GENERATION_FAILED,
        ),
        {
            "status": TicketStatus.NEW,
            "draft_text": None,
            "draft_sources": [],
            "draft_model": None,
            "draft_generated_at": None,
            "rejection_reason": None,
            "rejected_at": None,
            "last_error": None,
        },
    )
    await queue.enqueue(
        session,
        kind=JobKind.GENERATE_DRAFT,
        ticket_id=ticket_id,
        max_attempts=settings.generation_max_attempts,
    )
    await session.commit()


async def retry_send(
    session: AsyncSession, ticket_id: int, settings: Settings
) -> None:
    """Re-queue a failed delivery. The idempotency key stays the same, so a reply that
    actually reached the helpdesk earlier will not be duplicated."""
    await _transition(
        session,
        ticket_id,
        "retry sending",
        TicketStatus.SEND_FAILED,
        {"status": TicketStatus.APPROVED, "last_error": None},
    )
    await queue.enqueue(
        session,
        kind=JobKind.SEND_REPLY,
        ticket_id=ticket_id,
        max_attempts=settings.send_max_attempts,
    )
    await session.commit()


async def get_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        raise TicketNotFoundError(ticket_id)

    return ticket


async def list_tickets(
    session: AsyncSession,
    status: TicketStatus | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Ticket]:
    """Newest first; `id` breaks ties so pages never overlap or skip rows."""
    stmt = (
        select(Ticket)
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        stmt = stmt.where(Ticket.status == status)

    return list((await session.scalars(stmt)).all())


async def count_by_status(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(Ticket.status, func.count()).group_by(Ticket.status)
    )
    counts = {s.value: 0 for s in TicketStatus}
    counts.update(dict(rows.all()))
    return counts
