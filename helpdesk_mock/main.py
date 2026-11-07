"""Fake helpdesk for local runs and demos (not part of the service itself).

1. Reply API, `POST /tickets/{ticket_id}/replies`, which misbehaves like a real one:
   - `MOCK_REPLY_FAIL_RATE`: answers 5xx without storing the reply;
   - `MOCK_REPLY_LOST_RESPONSE_RATE`: stores the reply, then hangs longer than the
     client's timeout (the customer got the message, the caller doesn't know it).
   It honours `Idempotency-Key`: a repeated key returns the stored reply instead of
   posting it again. `duplicate_messages` counts identical replies posted twice to one
   ticket, which must stay 0.
2. Webhook sender with real helpdesk semantics: 2 s timeout, retries with backoff, and
   deliberate duplicate deliveries (concurrent and late).

State lives in memory; restart the container to reset it.
"""

import asyncio
import itertools
import logging
import os
import random
import typing as t
import uuid
from collections import Counter
from collections.abc import Coroutine, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [mock] %(message)s"
)

WEBHOOK_URL = os.getenv(
    "MOCK_WEBHOOK_URL", "http://localhost:8000/webhooks/helpdesk"
)
PUBLIC_BASE_URL = os.getenv(
    "MOCK_PUBLIC_BASE_URL", "http://localhost:8001"
).rstrip("/")
WEBHOOK_TIMEOUT_SECONDS = 2.0
WEBHOOK_MAX_ATTEMPTS = 8

SAMPLE_MESSAGES = (
    "Не можу експортувати звіт у PDF",
    "PDF-файл не завантажується, кнопка Завантажити нічого не робить",
    "Як вивантажити таблицю в Excel? У CSV замість кирилиці якісь знаки",
    "Забув пароль, а лист для відновлення не приходить",
    "Загубив телефон з двофакторною автентифікацією, як увійти?",
    "Скільки коштує тариф Команда і чи можна оплатити за рахунком як ТОВ?",
    "Хочу скасувати підписку і повернути гроші за цей місяць",
    "Як запросити колегу і дати йому лише перегляд?",
    "Не підключається база PostgreSQL, пише timeout",
    "Звіти не приходять на пошту за розкладом з минулого тижня",
    "Чи можна змінити мову інтерфейсу на англійську?",
    "Дані в дашборді не оновлюються вже два дні",
)


@dataclass
class Config:
    reply_fail_rate: float = float(os.getenv("MOCK_REPLY_FAIL_RATE", "0.2"))
    reply_lost_response_rate: float = float(
        os.getenv("MOCK_REPLY_LOST_RESPONSE_RATE", "0.1")
    )
    reply_hang_seconds: float = float(
        os.getenv("MOCK_REPLY_HANG_SECONDS", "15")
    )


@dataclass
class StoredReply:
    id: str
    ticket_id: str
    message: str
    idempotency_key: str | None
    received_at: str


@dataclass
class State:
    config: Config = field(default_factory=Config)
    replies: list[StoredReply] = field(default_factory=list)
    by_key: dict[str, StoredReply] = field(default_factory=dict)
    events: list[dict[str, str]] = field(default_factory=list)
    stats: Counter[str] = field(default_factory=Counter)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)


state = State()
_ticket_numbers = itertools.count(1001)
app = FastAPI(title="Helpdesk mock")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- Reply API ------------------------------------------------------------------------


class ReplyIn(BaseModel):
    ticket_id: str
    message: str = Field(min_length=1)


@app.post("/tickets/{ticket_id}/replies", status_code=201)
async def create_reply(
    ticket_id: str,
    body: ReplyIn,
    idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    state.stats["reply_requests"] += 1
    if body.ticket_id != ticket_id:
        raise HTTPException(422, "ticket_id in body does not match the URL")

    if idempotency_key and idempotency_key in state.by_key:
        state.stats["idempotent_replays"] += 1
        stored = state.by_key[idempotency_key]
        log.info("reply %s replayed for key %s", stored.id, idempotency_key)
        return JSONResponse(
            asdict(stored),
            status_code=201,
            headers={"Idempotent-Replayed": "true"},
        )

    roll = random.random()
    cfg = state.config
    if roll < cfg.reply_fail_rate:
        state.stats["failed_5xx"] += 1
        return JSONResponse(
            {"error": "temporary failure"},
            status_code=random.choice([500, 502, 503]),
        )

    if any(
        r.ticket_id == ticket_id and r.message == body.message
        for r in state.replies
    ):
        state.stats["duplicate_messages"] += 1
        log.error("DUPLICATE reply to ticket %s", ticket_id)

    reply = StoredReply(
        id=f"rep_{uuid.uuid4().hex[:8]}",
        ticket_id=ticket_id,
        message=body.message,
        idempotency_key=idempotency_key,
        received_at=_now(),
    )
    state.replies.append(reply)
    if idempotency_key:
        state.by_key[idempotency_key] = reply

    state.stats["replies_stored"] += 1

    if roll < cfg.reply_fail_rate + cfg.reply_lost_response_rate:
        state.stats["lost_responses"] += 1
        log.info(
            "reply %s stored, response will be lost (hang %ss)",
            reply.id,
            cfg.reply_hang_seconds,
        )
        await asyncio.sleep(cfg.reply_hang_seconds)

    return JSONResponse(asdict(reply), status_code=201)


@app.get("/tickets/{ticket_id}/replies")
async def list_ticket_replies(ticket_id: str) -> list[dict[str, t.Any]]:
    return [asdict(r) for r in state.replies if r.ticket_id == ticket_id]


# --- Webhook sender -------------------------------------------------------------------


def _spawn(coro: Coroutine[t.Any, t.Any, None]) -> None:
    task = asyncio.create_task(coro)
    state.tasks.add(task)
    task.add_done_callback(state.tasks.discard)


async def _deliver(
    client: httpx.AsyncClient, event: Mapping[str, str]
) -> None:
    """Deliver like a real helpdesk: wait <= 2 s, retry on anything but 2xx."""
    for attempt in range(1, WEBHOOK_MAX_ATTEMPTS + 1):
        try:
            response = await client.post(
                WEBHOOK_URL, json=event, timeout=WEBHOOK_TIMEOUT_SECONDS
            )
            if response.is_success:
                state.stats["webhook_delivered"] += 1
                return

            state.stats["webhook_non_2xx"] += 1
            log.warning(
                "webhook %s -> HTTP %s",
                event["event_id"],
                response.status_code,
            )
        except httpx.HTTPError as exc:
            state.stats["webhook_timeouts_or_errors"] += 1
            log.warning(
                "webhook %s attempt %d failed: %r",
                event["event_id"],
                attempt,
                exc,
            )

        state.stats["webhook_retries"] += 1
        await asyncio.sleep(min(30.0, 2.0**attempt) * random.uniform(0.5, 1.0))

    state.stats["webhook_gave_up"] += 1


async def _deliver_with_duplicates(
    event: Mapping[str, str], duplicate_rate: float, delay: float = 0.0
) -> None:
    if delay:
        await asyncio.sleep(delay)

    async with httpx.AsyncClient() as client:
        deliveries = [_deliver(client, event)]
        # The same event, concurrently.
        if random.random() < duplicate_rate:
            state.stats["webhook_concurrent_duplicates"] += 1
            deliveries.append(_deliver(client, event))

        # The same event again, a bit later.
        if random.random() < duplicate_rate:
            state.stats["webhook_late_duplicates"] += 1

            async def late() -> None:
                await asyncio.sleep(random.uniform(1.0, 5.0))
                await _deliver(client, event)

            deliveries.append(late())

        await asyncio.gather(*deliveries)


def _new_event(
    message: str | None = None, email: str | None = None
) -> dict[str, str]:
    ticket_id = f"T-{next(_ticket_numbers)}"
    event = {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "ticket_id": ticket_id,
        "customer_email": email or f"user{random.randint(1, 999)}@example.com",
        "message": message or random.choice(SAMPLE_MESSAGES),
        "reply_url": f"{PUBLIC_BASE_URL}/tickets/{ticket_id}/replies",
    }
    state.events.append({**event, "created_at": _now()})
    return event


class TicketIn(BaseModel):
    message: str | None = None
    customer_email: str | None = None
    duplicate_rate: float = Field(0.0, ge=0, le=1)


class BurstIn(BaseModel):
    count: int = Field(50, ge=1, le=5000)
    spread_seconds: float = Field(0.0, ge=0, le=3600)
    duplicate_rate: float = Field(0.3, ge=0, le=1)


@app.post("/simulate/ticket")
async def simulate_ticket(body: TicketIn) -> dict[str, str]:
    event = _new_event(body.message, body.customer_email)
    _spawn(_deliver_with_duplicates(event, body.duplicate_rate))
    return event


@app.post("/simulate/burst")
async def simulate_burst(body: BurstIn) -> dict[str, float]:
    for _ in range(body.count):
        event = _new_event()
        delay = (
            random.uniform(0, body.spread_seconds)
            if body.spread_seconds
            else 0.0
        )
        _spawn(_deliver_with_duplicates(event, body.duplicate_rate, delay))

    return {"scheduled": body.count, "spread_seconds": body.spread_seconds}


class ConfigIn(BaseModel):
    reply_fail_rate: float | None = Field(None, ge=0, le=1)
    reply_lost_response_rate: float | None = Field(None, ge=0, le=1)
    reply_hang_seconds: float | None = Field(None, ge=0, le=120)


@app.post("/config")
async def update_config(body: ConfigIn) -> dict[str, float]:
    for name, value in body.model_dump(exclude_none=True).items():
        setattr(state.config, name, value)

    return asdict(state.config)


@app.get("/api/state")
async def get_state() -> dict[str, t.Any]:
    return {
        "config": asdict(state.config),
        "stats": state.stats,
        "replies": [asdict(r) for r in state.replies],
        "events": state.events,
    }


# --- HTML -----------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "config": state.config,
            "stats": dict(sorted(state.stats.items())),
            "replies": reversed(state.replies[-50:]),
            "events": reversed(state.events[-50:]),
            "samples": SAMPLE_MESSAGES,
            "webhook_url": WEBHOOK_URL,
        },
    )


@app.post("/ui/ticket")
async def ui_ticket(
    message: str = Form(""), duplicate_rate: float = Form(0.0)
) -> RedirectResponse:
    event = _new_event(message.strip() or None)
    _spawn(_deliver_with_duplicates(event, duplicate_rate))
    return RedirectResponse("/", status_code=303)


@app.post("/ui/burst")
async def ui_burst(
    count: int = Form(50),
    spread_seconds: float = Form(0.0),
    duplicate_rate: float = Form(0.3),
) -> RedirectResponse:
    await simulate_burst(
        BurstIn(
            count=count,
            spread_seconds=spread_seconds,
            duplicate_rate=duplicate_rate,
        )
    )
    return RedirectResponse("/", status_code=303)


@app.post("/ui/config")
async def ui_config(
    reply_fail_rate: float = Form(...),
    reply_lost_response_rate: float = Form(...),
    reply_hang_seconds: float = Form(...),
) -> RedirectResponse:
    await update_config(
        ConfigIn(
            reply_fail_rate=reply_fail_rate,
            reply_lost_response_rate=reply_lost_response_rate,
            reply_hang_seconds=reply_hang_seconds,
        )
    )
    return RedirectResponse("/", status_code=303)
