import asyncio

from sqlalchemy import select

from app import queue
from app.models import Job, JobKind, JobStatus, Ticket


async def _make_ticket(sessionmaker, n: int = 1) -> int:
    async with sessionmaker.begin() as session:
        ticket = Ticket(
            event_id=f"evt_{n}",
            external_ticket_id=f"T-{n}",
            customer_email="u@example.com",
            message="hi",
            reply_url="https://helpdesk.test/r",
        )
        session.add(ticket)
        await session.flush()
        await queue.enqueue(
            session, kind=JobKind.GENERATE_DRAFT, ticket_id=ticket.id, max_attempts=3
        )
        return ticket.id


async def _job(sessionmaker) -> Job:
    async with sessionmaker() as session:
        return (await session.scalars(select(Job))).one()


async def test_claim_returns_none_when_queue_is_empty(sessionmaker):
    assert await queue.claim(sessionmaker, JobKind.GENERATE_DRAFT, 60) is None


async def test_concurrent_claims_never_return_the_same_job(sessionmaker):
    for n in range(5):
        await _make_ticket(sessionmaker, n)
    claims = await asyncio.gather(
        *(queue.claim(sessionmaker, JobKind.GENERATE_DRAFT, 60) for _ in range(20))
    )
    claimed = [c.id for c in claims if c is not None]
    assert len(claimed) == 5
    assert len(set(claimed)) == 5


async def test_claim_respects_kind(sessionmaker):
    await _make_ticket(sessionmaker)
    assert await queue.claim(sessionmaker, JobKind.SEND_REPLY, 60) is None
    assert await queue.claim(sessionmaker, JobKind.GENERATE_DRAFT, 60) is not None


async def test_expired_lease_is_reclaimed_and_stale_owner_is_fenced(sessionmaker):
    await _make_ticket(sessionmaker)
    first = await queue.claim(sessionmaker, JobKind.GENERATE_DRAFT, lease_seconds=0)
    assert first is not None and first.attempts == 1
    # Lease of 0 s has already expired: another worker can take the job over.
    second = await queue.claim(sessionmaker, JobKind.GENERATE_DRAFT, lease_seconds=60)
    assert second is not None and second.id == first.id and second.attempts == 2

    async with sessionmaker.begin() as session:
        assert await queue.mark_done(session, first) is False  # stale owner
        assert await queue.mark_done(session, second) is True
    assert (await _job(sessionmaker)).status == JobStatus.DONE


async def test_running_job_is_not_claimed_twice(sessionmaker):
    await _make_ticket(sessionmaker)
    assert await queue.claim(sessionmaker, JobKind.GENERATE_DRAFT, 60) is not None
    assert await queue.claim(sessionmaker, JobKind.GENERATE_DRAFT, 60) is None


async def test_retry_postpones_job(sessionmaker):
    await _make_ticket(sessionmaker)
    job = await queue.claim(sessionmaker, JobKind.GENERATE_DRAFT, 60)
    async with sessionmaker.begin() as session:
        assert await queue.mark_retry(session, job, "boom", delay_seconds=3600)
    assert await queue.claim(sessionmaker, JobKind.GENERATE_DRAFT, 60) is None
    stored = await _job(sessionmaker)
    assert (stored.status, stored.last_error, stored.attempts) == (JobStatus.QUEUED, "boom", 1)


async def test_enqueue_resets_finished_job_but_not_active_one(sessionmaker):
    ticket_id = await _make_ticket(sessionmaker)
    job = await queue.claim(sessionmaker, JobKind.GENERATE_DRAFT, 60)

    async with sessionmaker.begin() as session:  # active: no-op
        await queue.enqueue(
            session, kind=JobKind.GENERATE_DRAFT, ticket_id=ticket_id, max_attempts=3
        )
    assert (await _job(sessionmaker)).status == JobStatus.RUNNING

    async with sessionmaker.begin() as session:
        await queue.mark_failed(session, job, "nope")
        await queue.enqueue(
            session, kind=JobKind.GENERATE_DRAFT, ticket_id=ticket_id, max_attempts=3
        )
    stored = await _job(sessionmaker)
    assert (stored.status, stored.attempts, stored.last_error) == (JobStatus.QUEUED, 0, None)


def test_backoff_grows_and_is_capped():
    delays = [queue.backoff_delay(a, base=2, cap=60) for a in range(1, 10)]
    assert 1.6 <= delays[0] <= 2.4
    assert 3.2 <= delays[1] <= 4.8
    assert all(d <= 72 for d in delays)
