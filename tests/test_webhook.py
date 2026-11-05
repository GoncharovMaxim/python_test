import asyncio
import time

from sqlalchemy import func, select

from app.models import Job, JobKind, JobStatus, Ticket, TicketStatus
from tests.conftest import make_event


async def _counts(sessionmaker) -> tuple[int, int]:
    async with sessionmaker() as session:
        tickets = await session.scalar(select(func.count()).select_from(Ticket))
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


async def test_repeated_delivery_is_acknowledged_but_not_stored_twice(client, sessionmaker):
    first = await client.post("/webhooks/helpdesk", json=make_event())
    second = await client.post("/webhooks/helpdesk", json=make_event())
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert await _counts(sessionmaker) == (1, 1)


async def test_concurrent_duplicates_create_exactly_one_ticket(client, sessionmaker):
    responses = await asyncio.gather(
        *(client.post("/webhooks/helpdesk", json=make_event()) for _ in range(25))
    )
    codes = sorted(r.status_code for r in responses)
    assert codes.count(202) == 1
    assert codes.count(200) == 24
    assert await _counts(sessionmaker) == (1, 1)


async def test_burst_of_distinct_events_is_fast_and_lossless(client, sessionmaker):
    started = time.monotonic()
    responses = await asyncio.gather(
        *(client.post("/webhooks/helpdesk", json=make_event(n)) for n in range(1, 101))
    )
    elapsed = time.monotonic() - started
    assert all(r.status_code == 202 for r in responses)
    assert await _counts(sessionmaker) == (100, 100)
    assert elapsed < 10, f"100 concurrent webhooks took {elapsed:.1f}s"


async def test_invalid_payload_is_rejected(client, sessionmaker):
    missing = make_event()
    del missing["message"]
    assert (await client.post("/webhooks/helpdesk", json=missing)).status_code == 422
    bad_email = make_event(customer_email="not-an-email")
    assert (await client.post("/webhooks/helpdesk", json=bad_email)).status_code == 422
    assert await _counts(sessionmaker) == (0, 0)


async def test_reply_url_host_must_be_allowed(client, sessionmaker):
    event = make_event(reply_url="http://169.254.169.254/latest/meta-data")
    response = await client.post("/webhooks/helpdesk", json=event)
    assert response.status_code == 422
    assert await _counts(sessionmaker) == (0, 0)
