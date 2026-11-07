"""Job handlers. Each one reads state, does slow I/O without holding a DB connection,
then writes the result and finishes the job in one transaction."""

from __future__ import annotations

import logging
import typing as t
from dataclasses import dataclass

from sqlalchemy import func, update

from app import queue
from app.drafts import (
    DRAFT_SCHEMA,
    STATIC_DRAFT_RESPONSE,
    SYSTEM_PROMPT,
    DraftParseError,
    build_user_prompt,
    parse_draft,
)
from app.helpdesk import send_reply
from app.kb.repository import search_articles
from app.llm.client import LLMError
from app.models import Ticket, TicketStatus

if t.TYPE_CHECKING:
    import httpx

    from app.config import Settings
    from app.db import SessionMaker
    from app.kb.repository import FoundArticle
    from app.llm.client import LLMClient
    from app.queue import ClaimedJob

log = logging.getLogger(__name__)


@dataclass
class WorkerContext:
    settings: Settings
    sessionmaker: SessionMaker
    llm: LLMClient
    http: httpx.AsyncClient


async def _finish_noop(
    ctx: WorkerContext, job: ClaimedJob, reason: str
) -> None:
    log.info(
        "job %s (%s) for ticket %s: nothing to do (%s)",
        job.id,
        job.kind,
        job.ticket_id,
        reason,
    )
    async with ctx.sessionmaker.begin() as session:
        await queue.mark_done(session, job)


async def _retry_or_fail(
    ctx: WorkerContext,
    job: ClaimedJob,
    *,
    error: str,
    retryable: bool,
    retry_after: float | None,
    expected_status: TicketStatus,
    failed_status: TicketStatus,
    backoff_base: float,
    backoff_cap: float,
) -> None:
    async with ctx.sessionmaker.begin() as session:
        if retryable and not job.is_last_attempt:
            delay = max(
                retry_after or 0.0,
                queue.backoff_delay(job.attempts, backoff_base, backoff_cap),
            )
            if await queue.mark_retry(session, job, error, delay):
                await session.execute(
                    update(Ticket)
                    .where(Ticket.id == job.ticket_id)
                    .values(last_error=error, updated_at=func.now())
                )
                log.warning(
                    "job %s (%s) attempt %d/%d failed, retry in %.1fs: %s",
                    job.id,
                    job.kind,
                    job.attempts,
                    job.max_attempts,
                    delay,
                    error,
                )

            return

        if await queue.mark_failed(session, job, error):
            await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == job.ticket_id,
                    Ticket.status == expected_status,
                )
                .values(
                    status=failed_status,
                    last_error=error,
                    updated_at=func.now(),
                )
            )
            log.error(
                "job %s (%s) failed permanently after %d attempt(s): %s",
                job.id,
                job.kind,
                job.attempts,
                error,
            )


async def handle_generate_draft(ctx: WorkerContext, job: ClaimedJob) -> None:
    retry_kwargs = {
        "expected_status": TicketStatus.NEW,
        "failed_status": TicketStatus.GENERATION_FAILED,
        "backoff_base": ctx.settings.generation_backoff_base_seconds,
        "backoff_cap": ctx.settings.generation_backoff_cap_seconds,
    }
    message = ""
    articles: list[FoundArticle] = []
    async with ctx.sessionmaker() as session:
        ticket = await session.get(Ticket, job.ticket_id)
        if ticket and ticket.status == TicketStatus.NEW:
            message = ticket.message
            articles = await search_articles(
                session, message, ctx.settings.kb_top_k
            )
    # The DB connection is released here: the LLM call can take up to 30 s.

    if not message:
        status = ticket.status if ticket else "missing"
        await _finish_noop(ctx, job, f"ticket status is {status}")
        return

    if (
        job.attempts > job.max_attempts
    ):  # a worker crashed mid-way on the last attempt
        await _retry_or_fail(
            ctx,
            job,
            error="attempts exhausted",
            retryable=False,
            retry_after=None,
            **retry_kwargs,
        )
        return

    try:
        result = await ctx.llm.generate_json(
            system=SYSTEM_PROMPT,
            user=build_user_prompt(message, articles),
            schema=DRAFT_SCHEMA,
            stub_response=STATIC_DRAFT_RESPONSE,
        )
        draft = parse_draft(result.text, articles)
    except LLMError as exc:
        await _retry_or_fail(
            ctx,
            job,
            error=f"LLM: {exc}",
            retryable=exc.retryable,
            retry_after=exc.retry_after,
            **retry_kwargs,
        )
        return
    except DraftParseError as exc:
        await _retry_or_fail(
            ctx,
            job,
            error=f"bad LLM output: {exc}",
            retryable=True,
            retry_after=None,
            **retry_kwargs,
        )
        return

    async with ctx.sessionmaker.begin() as session:
        if not await queue.mark_done(session, job):
            log.warning(
                "job %s lost its lease; discarding generated draft", job.id
            )
            return

        await session.execute(
            update(Ticket)
            .where(
                Ticket.id == job.ticket_id, Ticket.status == TicketStatus.NEW
            )
            .values(
                status=TicketStatus.DRAFT_READY,
                draft_text=draft.text,
                draft_sources=[s.as_dict() for s in draft.sources],
                draft_model=result.model,
                draft_version=Ticket.draft_version + 1,
                draft_generated_at=func.now(),
                last_error=None,
                updated_at=func.now(),
            )
        )

    log.info(
        "draft ready for ticket %s (model=%s, sources=%s)",
        job.ticket_id,
        result.model,
        [s.slug for s in draft.sources],
    )


async def handle_send_reply(ctx: WorkerContext, job: ClaimedJob) -> None:
    async with ctx.sessionmaker() as session:
        ticket = await session.get(Ticket, job.ticket_id)

    if ticket is None or ticket.status != TicketStatus.APPROVED:
        await _finish_noop(
            ctx,
            job,
            f"ticket status is {ticket.status if ticket else 'missing'}",
        )
        return

    retry_kwargs = {
        "expected_status": TicketStatus.APPROVED,
        "failed_status": TicketStatus.SEND_FAILED,
        "backoff_base": ctx.settings.send_backoff_base_seconds,
        "backoff_cap": ctx.settings.send_backoff_cap_seconds,
    }
    if job.attempts > job.max_attempts:
        await _retry_or_fail(
            ctx,
            job,
            error="attempts exhausted",
            retryable=False,
            retry_after=None,
            **retry_kwargs,
        )
        return

    if not ctx.settings.is_reply_url_allowed(ticket.reply_url):
        await _retry_or_fail(
            ctx,
            job,
            error=f"reply_url host is not allowed: {ticket.reply_url}",
            retryable=False,
            retry_after=None,
            **retry_kwargs,
        )
        return

    if not ticket.reply_text or ticket.reply_idempotency_key is None:
        await _retry_or_fail(
            ctx,
            job,
            error="approved ticket has no reply text or idempotency key",
            retryable=False,
            retry_after=None,
            **retry_kwargs,
        )
        return

    outcome = await send_reply(
        ctx.http,
        reply_url=ticket.reply_url,
        ticket_id=ticket.external_ticket_id,
        message=ticket.reply_text,
        idempotency_key=str(ticket.reply_idempotency_key),
        timeout_seconds=ctx.settings.helpdesk_timeout_seconds,
    )
    if not outcome.delivered:
        await _retry_or_fail(
            ctx,
            job,
            error=outcome.error or "delivery failed",
            retryable=outcome.retryable,
            retry_after=outcome.retry_after,
            **retry_kwargs,
        )
        return

    async with ctx.sessionmaker.begin() as session:
        # Even if the lease was lost, the helpdesk accepted the reply: record it. A
        # concurrent re-send uses the same idempotency key, so no duplicate is possible.
        await queue.mark_done(session, job)
        await session.execute(
            update(Ticket)
            .where(
                Ticket.id == job.ticket_id,
                Ticket.status == TicketStatus.APPROVED,
            )
            .values(
                status=TicketStatus.SENT,
                sent_at=func.now(),
                last_error=None,
                updated_at=func.now(),
            )
        )

    log.info(
        "reply for ticket %s delivered (HTTP %s)",
        job.ticket_id,
        outcome.status_code,
    )
