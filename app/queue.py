"""Durable job queue on Postgres.

- claim: `FOR UPDATE SKIP LOCKED`, so concurrent workers never get the same job. Claiming
  sets a lease (`available_at = now() + lease`); if the worker dies, the job becomes
  claimable again when the lease expires.
- every finishing transition is fenced by the `attempts` value seen at claim time, so a
  worker whose lease expired (and whose job was re-claimed) can't overwrite the result.
- callers run finishing transitions in the same transaction as their ticket update.
"""

import random
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionMaker
from app.models import Job, JobKind, JobStatus


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: str
    ticket_id: int
    attempts: int  # including the current one
    max_attempts: int

    @property
    def is_last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


async def enqueue(
    session: AsyncSession, *, kind: JobKind, ticket_id: int, max_attempts: int
) -> None:
    """Create the job, or reset a finished (done/failed) one. Active jobs are left alone."""
    stmt = pg_insert(Job).values(
        kind=kind,
        ticket_id=ticket_id,
        status=JobStatus.QUEUED,
        attempts=0,
        max_attempts=max_attempts,
        available_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="jobs_ticket_kind_key",
        set_={
            "status": JobStatus.QUEUED,
            "attempts": 0,
            "max_attempts": stmt.excluded.max_attempts,
            "available_at": func.now(),
            "last_error": None,
            "updated_at": func.now(),
        },
        where=Job.status.in_([JobStatus.DONE, JobStatus.FAILED]),
    )
    await session.execute(stmt)


_CLAIM_SQL = text(
    """
    UPDATE jobs
    SET status = 'running',
        attempts = attempts + 1,
        available_at = now() + make_interval(secs => :lease),
        updated_at = now()
    WHERE id = (
        SELECT id FROM jobs
        WHERE kind = :kind
          AND status IN ('queued', 'running')
          AND available_at <= now()
        ORDER BY available_at, id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, kind, ticket_id, attempts, max_attempts
    """
)


async def claim(
    sessionmaker: SessionMaker, kind: JobKind, lease_seconds: float
) -> ClaimedJob | None:
    async with sessionmaker.begin() as session:
        row = (
            await session.execute(_CLAIM_SQL, {"kind": str(kind), "lease": float(lease_seconds)})
        ).first()
    return ClaimedJob(**row._mapping) if row else None


def _owned(job: ClaimedJob):
    return (Job.id == job.id) & (Job.status == JobStatus.RUNNING) & (Job.attempts == job.attempts)


async def _finish(session: AsyncSession, job: ClaimedJob, **values) -> bool:
    result = await session.execute(
        update(Job).where(_owned(job)).values(updated_at=func.now(), **values).returning(Job.id)
    )
    return result.scalar_one_or_none() is not None


async def mark_done(session: AsyncSession, job: ClaimedJob) -> bool:
    """Returns False if the lease was lost (the job belongs to someone else now)."""
    return await _finish(session, job, status=JobStatus.DONE, last_error=None)


async def mark_retry(
    session: AsyncSession, job: ClaimedJob, error: str, delay_seconds: float
) -> bool:
    return await _finish(
        session,
        job,
        status=JobStatus.QUEUED,
        last_error=error,
        available_at=func.now() + timedelta(seconds=delay_seconds),
    )


async def mark_failed(session: AsyncSession, job: ClaimedJob, error: str) -> bool:
    return await _finish(session, job, status=JobStatus.FAILED, last_error=error)


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with +-20% jitter: base, 2*base, 4*base, ... capped at `cap`."""
    delay = min(cap, base * 2 ** max(attempt - 1, 0))
    return delay * random.uniform(0.8, 1.2)
