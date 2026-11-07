"""End-to-end: webhook -> draft -> operator -> delivery, against real Postgres.

The helpdesk reply API is replaced by an httpx MockTransport scripted per test.
"""

import asyncio
import json
from collections.abc import Callable
from urllib.parse import unquote_plus

import httpx
import pytest
from sqlalchemy import select, update

from app import queue
from app.llm.client import LLMClient, PermanentLLMError
from app.llm.rate_limiter import PostgresRateLimiter
from app.models import Job, JobKind, JobStatus, Ticket, TicketStatus
from app.worker.handlers import WorkerContext, handle_generate_draft, handle_send_reply
from app.worker.runner import Worker
from tests.conftest import make_event

HANDLERS = {JobKind.GENERATE_DRAFT: handle_generate_draft, JobKind.SEND_REPLY: handle_send_reply}


class FakeHelpdesk:
    """Scripted reply API; records every request it receives."""

    def __init__(self, *script: Callable[[httpx.Request], httpx.Response] | int):
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if callable(step):
            return step(request)
        return httpx.Response(step, json={"status": step})

    @property
    def keys(self) -> list[str]:
        return [r.headers["Idempotency-Key"] for r in self.requests]


def timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("helpdesk did not answer in time", request=request)


@pytest.fixture
async def make_ctx(settings, sessionmaker):
    clients: list[httpx.AsyncClient] = []

    def factory(helpdesk: FakeHelpdesk | None = None, llm=None, **overrides) -> WorkerContext:
        cfg = settings.model_copy(update=overrides)
        http = httpx.AsyncClient(transport=httpx.MockTransport(helpdesk or FakeHelpdesk(201)))
        clients.append(http)
        llm = llm or LLMClient(cfg, PostgresRateLimiter(sessionmaker, limit=1000))
        return WorkerContext(cfg, sessionmaker, llm, http)

    yield factory
    for http in clients:
        await http.aclose()


async def run_jobs(ctx: WorkerContext, kind: JobKind, max_rounds: int = 50) -> int:
    """Claim and handle jobs of one kind until none is due (retries use tiny backoffs)."""
    handled = 0
    for _ in range(max_rounds):
        job = await queue.claim(ctx.sessionmaker, kind, lease_seconds=60)
        if job is None:
            async with ctx.sessionmaker() as session:
                pending = await session.scalar(
                    select(Job.id).where(Job.kind == kind, Job.status == JobStatus.QUEUED)
                )
            if pending is None:
                return handled
            await asyncio.sleep(0.05)
            continue
        await HANDLERS[kind](ctx, job)
        handled += 1
    raise AssertionError("jobs did not settle")


async def get_ticket(sessionmaker, ticket_id: int = 1) -> Ticket:
    async with sessionmaker() as session:
        return await session.get(Ticket, ticket_id)


async def draft_ready_ticket(client, ctx) -> Ticket:
    assert (await client.post("/webhooks/helpdesk", json=make_event())).status_code == 202
    await run_jobs(ctx, JobKind.GENERATE_DRAFT)
    ticket = await get_ticket(ctx.sessionmaker)
    assert ticket.status == TicketStatus.DRAFT_READY
    return ticket


async def test_full_happy_path(client, sessionmaker, make_ctx):
    helpdesk = FakeHelpdesk(201)
    ctx = make_ctx(helpdesk)

    ticket = await draft_ready_ticket(client, ctx)
    assert ticket.draft_model == "static-stub"
    assert ticket.draft_sources[0]["slug"] == "export-report-pdf"
    assert "https://kb.test/articles/export-report-pdf" in ticket.draft_text

    listed = (await client.get("/api/tickets", params={"status": "draft_ready"})).json()
    assert [t["id"] for t in listed] == [ticket.id]

    edited = ticket.draft_text + "\n\nP.S. Відредаговано оператором."
    response = await client.post(f"/api/tickets/{ticket.id}/approve", json={"text": edited})
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    await run_jobs(ctx, JobKind.SEND_REPLY)
    ticket = await get_ticket(sessionmaker)
    assert ticket.status == TicketStatus.SENT
    assert ticket.sent_at is not None

    assert len(helpdesk.requests) == 1
    request = helpdesk.requests[0]
    assert str(request.url) == "https://helpdesk.test/tickets/T-1001/replies"
    assert json.loads(request.content) == {"ticket_id": "T-1001", "message": edited}
    assert request.headers["Idempotency-Key"] == str(ticket.reply_idempotency_key)


async def test_approve_twice_sends_once(client, sessionmaker, make_ctx):
    helpdesk = FakeHelpdesk(201)
    ctx = make_ctx(helpdesk)
    ticket = await draft_ready_ticket(client, ctx)

    results = await asyncio.gather(
        *(client.post(f"/api/tickets/{ticket.id}/approve", json={}) for _ in range(5))
    )
    assert sorted(r.status_code for r in results) == [200, 409, 409, 409, 409]
    await run_jobs(ctx, JobKind.SEND_REPLY)
    assert len(helpdesk.requests) == 1
    # Approved without edits: the draft is sent as is.
    assert json.loads(helpdesk.requests[0].content)["message"] == ticket.draft_text


async def test_5xx_and_timeouts_are_retried_with_the_same_idempotency_key(
    client, sessionmaker, make_ctx
):
    helpdesk = FakeHelpdesk(503, timeout, 502, 201)
    ctx = make_ctx(helpdesk)
    ticket = await draft_ready_ticket(client, ctx)
    await client.post(f"/api/tickets/{ticket.id}/approve", json={})

    await run_jobs(ctx, JobKind.SEND_REPLY)
    ticket = await get_ticket(sessionmaker)
    assert ticket.status == TicketStatus.SENT
    assert len(helpdesk.requests) == 4
    assert set(helpdesk.keys) == {str(ticket.reply_idempotency_key)}
    assert len({r.content for r in helpdesk.requests}) == 1


async def test_permanent_helpdesk_error_and_manual_retry_keeps_the_key(
    client, sessionmaker, make_ctx
):
    helpdesk = FakeHelpdesk(400, 201)
    ctx = make_ctx(helpdesk)
    ticket = await draft_ready_ticket(client, ctx)
    await client.post(f"/api/tickets/{ticket.id}/approve", json={})

    await run_jobs(ctx, JobKind.SEND_REPLY)
    failed = await get_ticket(sessionmaker)
    assert failed.status == TicketStatus.SEND_FAILED
    assert "HTTP 400" in failed.last_error
    assert len(helpdesk.requests) == 1  # 4xx is not retried automatically

    response = await client.post(f"/api/tickets/{ticket.id}/retry-send")
    assert response.json()["status"] == "approved"
    await run_jobs(ctx, JobKind.SEND_REPLY)
    sent = await get_ticket(sessionmaker)
    assert sent.status == TicketStatus.SENT
    assert helpdesk.keys[0] == helpdesk.keys[1] == str(sent.reply_idempotency_key)


async def test_send_gives_up_after_max_attempts(client, sessionmaker, make_ctx):
    helpdesk = FakeHelpdesk(503)
    ctx = make_ctx(helpdesk)
    ticket = await draft_ready_ticket(client, ctx)
    await client.post(f"/api/tickets/{ticket.id}/approve", json={})
    async with sessionmaker.begin() as session:  # the limit is fixed on the job at approval
        await session.execute(
            update(Job).where(Job.kind == JobKind.SEND_REPLY).values(max_attempts=3)
        )

    await run_jobs(ctx, JobKind.SEND_REPLY)
    assert (await get_ticket(sessionmaker)).status == TicketStatus.SEND_FAILED
    assert len(helpdesk.requests) == 3


async def test_ticket_approved_by_stale_worker_is_not_sent_twice(client, sessionmaker, make_ctx):
    """A send job whose lease expired mid-flight is re-claimed; both attempts use one key."""
    helpdesk = FakeHelpdesk(201)
    ctx = make_ctx(helpdesk)
    ticket = await draft_ready_ticket(client, ctx)
    await client.post(f"/api/tickets/{ticket.id}/approve", json={})

    stale = await queue.claim(sessionmaker, JobKind.SEND_REPLY, lease_seconds=0)
    fresh = await queue.claim(sessionmaker, JobKind.SEND_REPLY, lease_seconds=60)
    await asyncio.gather(handle_send_reply(ctx, stale), handle_send_reply(ctx, fresh))

    assert (await get_ticket(sessionmaker)).status == TicketStatus.SENT
    assert len(set(helpdesk.keys)) == 1  # the helpdesk can dedup on it


class FailingLLM:
    stub_mode = False

    async def generate_json(self, **_):
        raise PermanentLLMError("provider returned HTTP 400: bad request")


async def test_generation_failure_and_regenerate(client, sessionmaker, make_ctx):
    await client.post("/webhooks/helpdesk", json=make_event())
    await run_jobs(make_ctx(llm=FailingLLM()), JobKind.GENERATE_DRAFT)
    ticket = await get_ticket(sessionmaker)
    assert ticket.status == TicketStatus.GENERATION_FAILED
    assert "HTTP 400" in ticket.last_error

    assert (await client.post(f"/api/tickets/{ticket.id}/approve", json={})).status_code == 409
    response = await client.post(f"/api/tickets/{ticket.id}/regenerate")
    assert response.json()["status"] == "new"
    await run_jobs(make_ctx(), JobKind.GENERATE_DRAFT)
    assert (await get_ticket(sessionmaker)).status == TicketStatus.DRAFT_READY


async def test_reject_then_regenerate(client, sessionmaker, make_ctx):
    ctx = make_ctx()
    ticket = await draft_ready_ticket(client, ctx)
    response = await client.post(f"/api/tickets/{ticket.id}/reject", json={"reason": "не те"})
    assert response.json()["status"] == "rejected"
    assert response.json()["rejection_reason"] == "не те"
    assert (await client.post(f"/api/tickets/{ticket.id}/reject", json={})).status_code == 409
    assert (await client.post(f"/api/tickets/{ticket.id}/retry-send")).status_code == 409
    assert (await client.post("/api/tickets/999/approve", json={})).status_code == 404

    await client.post(f"/api/tickets/{ticket.id}/regenerate")
    await run_jobs(ctx, JobKind.GENERATE_DRAFT)
    assert (await get_ticket(sessionmaker)).status == TicketStatus.DRAFT_READY


async def test_ui_flow(client, sessionmaker, make_ctx):
    ctx = make_ctx()
    ticket = await draft_ready_ticket(client, ctx)

    page = await client.get("/ui/tickets")
    assert page.status_code == 200
    assert "Не можу експортувати звіт у PDF" in page.text
    assert f"/ui/tickets/{ticket.id}/approve" in page.text
    assert '<input type="hidden" name="draft_version" value="1">' in page.text

    form = {"text": "Готова відповідь", "draft_version": "1", "status": ""}
    response = await client.post(f"/ui/tickets/{ticket.id}/approve", data=form)
    assert response.status_code == 303
    assert "notice=" in response.headers["location"]
    again = await client.post(f"/ui/tickets/{ticket.id}/approve", data={**form, "text": "x"})
    assert "error=" in again.headers["location"]
    assert (await get_ticket(sessionmaker)).reply_text == "Готова відповідь"


async def test_approving_an_outdated_draft_is_refused(client, sessionmaker, make_ctx):
    """Operator A loads draft v1; operator B regenerates it; A's approval must not send
    text based on v1."""
    ctx = make_ctx()
    ticket = await draft_ready_ticket(client, ctx)
    assert ticket.draft_version == 1
    await client.post(f"/api/tickets/{ticket.id}/regenerate")
    await run_jobs(ctx, JobKind.GENERATE_DRAFT)
    assert (await get_ticket(sessionmaker)).draft_version == 2

    # UI: stale form -> asked to reload, nothing approved
    stale_form = {"text": "Текст на основі v1", "draft_version": "1", "status": ""}
    response = await client.post(f"/ui/tickets/{ticket.id}/approve", data=stale_form)
    assert response.status_code == 303
    location = unquote_plus(response.headers["location"])
    assert "Оновіть сторінку" in location
    # API: same check, 409
    stale = await client.post(
        f"/api/tickets/{ticket.id}/approve", json={"text": "v1", "draft_version": 1}
    )
    assert stale.status_code == 409
    assert "outdated" in stale.json()["detail"]
    assert (await get_ticket(sessionmaker)).status == TicketStatus.DRAFT_READY

    current = await client.post(
        f"/api/tickets/{ticket.id}/approve", json={"text": "v2", "draft_version": 2}
    )
    assert current.status_code == 200
    assert current.json()["reply_text"] == "v2"


async def test_approving_while_regeneration_is_running_is_refused(client, make_ctx):
    ctx = make_ctx()
    ticket = await draft_ready_ticket(client, ctx)
    await client.post(f"/api/tickets/{ticket.id}/regenerate")  # status: new, no draft yet
    response = await client.post(
        f"/ui/tickets/{ticket.id}/approve", data={"text": "x", "draft_version": "1"}
    )
    assert "Генерується чернетка" in unquote_plus(response.headers["location"])


async def test_worker_runner_processes_queue(client, sessionmaker, make_ctx):
    helpdesk = FakeHelpdesk(201)
    ctx = make_ctx(helpdesk)
    for n in range(1, 6):
        await client.post("/webhooks/helpdesk", json=make_event(n))

    worker = Worker(ctx)
    run = asyncio.create_task(worker.run(shutdown_grace_seconds=5))

    async def wait_for(status: TicketStatus, count: int) -> None:
        for _ in range(200):
            async with sessionmaker() as session:
                n = len(
                    (await session.scalars(select(Ticket.id).where(Ticket.status == status))).all()
                )
            if n == count:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"tickets did not reach {status}")

    try:
        await wait_for(TicketStatus.DRAFT_READY, 5)
        async with sessionmaker.begin() as session:
            ids = (await session.scalars(select(Ticket.id))).all()
        for ticket_id in ids:
            await client.post(f"/api/tickets/{ticket_id}/approve", json={})
        await wait_for(TicketStatus.SENT, 5)
    finally:
        worker.stop()
        await asyncio.wait_for(run, timeout=10)
    assert len(helpdesk.requests) == 5


async def test_stale_generation_cannot_overwrite_regenerated_ticket(client, sessionmaker, make_ctx):
    """Worker A stalls past its lease; B finishes; the operator asks for a new draft and C
    claims it. When A finally finishes, its result must be discarded."""
    ctx = make_ctx()
    await client.post("/webhooks/helpdesk", json=make_event())
    stale = await queue.claim(sessionmaker, JobKind.GENERATE_DRAFT, lease_seconds=0)
    takeover = await queue.claim(sessionmaker, JobKind.GENERATE_DRAFT, lease_seconds=60)
    await handle_generate_draft(ctx, takeover)
    ticket = await get_ticket(sessionmaker)
    assert (ticket.status, ticket.draft_version) == (TicketStatus.DRAFT_READY, 1)

    await client.post(f"/api/tickets/{ticket.id}/regenerate")
    fresh = await queue.claim(sessionmaker, JobKind.GENERATE_DRAFT, lease_seconds=60)
    assert fresh.attempts == stale.attempts  # same attempt number, different claim

    await handle_generate_draft(ctx, stale)
    ticket = await get_ticket(sessionmaker)
    assert ticket.status == TicketStatus.NEW  # stale result discarded
    assert ticket.draft_text is None

    await handle_generate_draft(ctx, fresh)
    ticket = await get_ticket(sessionmaker)
    assert (ticket.status, ticket.draft_version) == (TicketStatus.DRAFT_READY, 2)


async def test_stale_draft_is_not_written_after_regenerate(client, sessionmaker, make_ctx):
    """A generation job whose ticket left `new` (e.g. operator action) is a no-op."""
    await client.post("/webhooks/helpdesk", json=make_event())
    async with sessionmaker.begin() as session:
        await session.execute(update(Ticket).values(status=TicketStatus.REJECTED))
    await run_jobs(make_ctx(), JobKind.GENERATE_DRAFT)
    ticket = await get_ticket(sessionmaker)
    assert ticket.status == TicketStatus.REJECTED
    assert ticket.draft_text is None
