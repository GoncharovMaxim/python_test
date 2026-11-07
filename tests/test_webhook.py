import asyncio
import time

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import make_sessionmaker
from app.main import create_app
from app.models import Job, JobKind, JobStatus, Ticket, TicketStatus
from tests.conftest import assert_logged, make_event


async def _counts(sessionmaker) -> tuple[int, int]:
    async with sessionmaker() as session:
        tickets = await session.scalar(
            select(func.count()).select_from(Ticket)
        )
        jobs = await session.scalar(select(func.count()).select_from(Job))

    return tickets, jobs


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.text == "OK"


async def test_new_event_is_stored_with_generation_job(client, sessionmaker):
    response = await client.post("/webhooks/helpdesk", json=make_event())
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"

    async with sessionmaker() as session:
        ticket = (await session.scalars(select(Ticket))).one()
        job = (await session.scalars(select(Job))).one()

    assert ticket.event_id == "evt_0001"
    assert ticket.external_ticket_id == "T-1001"
    assert ticket.status == TicketStatus.NEW
    assert (job.kind, job.status, job.ticket_id) == (
        JobKind.GENERATE_DRAFT,
        JobStatus.QUEUED,
        ticket.id,
    )


async def test_repeated_delivery_is_acknowledged_but_not_stored_twice(
    client, sessionmaker
):
    first = await client.post("/webhooks/helpdesk", json=make_event())
    second = await client.post("/webhooks/helpdesk", json=make_event())
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert await _counts(sessionmaker) == (1, 1)


async def test_concurrent_duplicates_create_exactly_one_ticket(
    client, sessionmaker
):
    responses = await asyncio.gather(
        *(
            client.post("/webhooks/helpdesk", json=make_event())
            for _ in range(25)
        )
    )
    codes = sorted(r.status_code for r in responses)
    assert codes.count(202) == 1
    assert codes.count(200) == 24
    assert await _counts(sessionmaker) == (1, 1)


async def test_burst_of_distinct_events_is_fast_and_lossless(
    client, sessionmaker
):
    started = time.monotonic()
    responses = await asyncio.gather(
        *(
            client.post("/webhooks/helpdesk", json=make_event(n))
            for n in range(1, 101)
        )
    )
    elapsed = time.monotonic() - started
    assert all(r.status_code == 202 for r in responses)
    assert await _counts(sessionmaker) == (100, 100)
    assert elapsed < 10, f"100 concurrent webhooks took {elapsed:.1f}s"


def _app_client(settings, sessionmaker, budget: float) -> httpx.AsyncClient:
    app = create_app(
        settings=settings.model_copy(
            update={"webhook_timeout_seconds": budget}
        ),
        sessionmaker=sessionmaker,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_webhook_answers_503_in_time_when_no_db_connection_is_free(
    database_url, settings, sessionmaker, caplog
):
    """Waiting for a pooled connection counts against the helpdesk's 2 s budget."""
    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    try:
        async with _app_client(
            settings, make_sessionmaker(engine), budget=0.3
        ) as client:
            async with engine.connect() as held:  # take the only connection
                await held.execute(text("SELECT 1"))
                started = time.monotonic()
                response = await client.post(
                    "/webhooks/helpdesk", json=make_event()
                )
                elapsed = time.monotonic() - started

            assert response.status_code == 503
            assert elapsed < 1.0
            assert_logged(caplog, "WARNING", "not stored within 0.3s")
            # The helpdesk retries once the pool is free again: nothing was lost.
            retry = await client.post("/webhooks/helpdesk", json=make_event())
            assert retry.status_code == 202
    finally:
        await engine.dispose()

    assert await _counts(sessionmaker) == (1, 1)


async def test_webhook_answers_503_in_time_when_insert_is_blocked(
    engine, settings, sessionmaker, caplog
):
    """A statement stuck on a lock is cancelled at the budget; the connection stays usable."""
    async with _app_client(settings, sessionmaker, budget=0.3) as client:
        async with engine.connect() as blocker:
            tx = await blocker.begin()
            await blocker.execute(
                text(
                    "INSERT INTO tickets (event_id, external_ticket_id, customer_email, message,"
                    " reply_url) VALUES ('evt_0001', 'T-1', 'a@b.c', 'm', 'https://x')"
                )
            )
            started = time.monotonic()
            response = await client.post(
                "/webhooks/helpdesk", json=make_event()
            )
            elapsed = time.monotonic() - started
            await tx.rollback()

        assert response.status_code == 503
        assert elapsed < 1.0
        assert_logged(caplog, "WARNING", "not stored within 0.3s")
        results = [
            await client.post("/webhooks/helpdesk", json=make_event())
            for _ in range(3)
        ]
        assert [r.status_code for r in results] == [202, 200, 200]

    assert await _counts(sessionmaker) == (1, 1)


async def test_invalid_payload_is_rejected(client, sessionmaker):
    missing = make_event()
    del missing["message"]
    assert (
        await client.post("/webhooks/helpdesk", json=missing)
    ).status_code == 422
    bad_email = make_event(customer_email="not-an-email")
    assert (
        await client.post("/webhooks/helpdesk", json=bad_email)
    ).status_code == 422
    assert await _counts(sessionmaker) == (0, 0)


async def test_reply_url_host_must_be_allowed(client, sessionmaker):
    event = make_event(reply_url="http://169.254.169.254/latest/meta-data")
    response = await client.post("/webhooks/helpdesk", json=event)
    assert response.status_code == 422
    assert await _counts(sessionmaker) == (0, 0)
