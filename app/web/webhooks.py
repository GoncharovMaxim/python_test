import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy.exc import SQLAlchemyError

from app.tickets import IncomingEvent, ingest_event
from app.web.deps import SessionDep, SettingsDep

log = logging.getLogger(__name__)

router = APIRouter()


def _unavailable(detail: str) -> JSONResponse:
    return JSONResponse(
        {"status": "error", "detail": detail}, status_code=503, headers={"Retry-After": "1"}
    )


class HelpdeskEvent(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    event_id: str = Field(min_length=1, max_length=255)
    ticket_id: str = Field(min_length=1, max_length=255)
    customer_email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    message: str = Field(min_length=1, max_length=20_000)
    reply_url: HttpUrl


@router.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    return "OK"


@router.post(
    "/webhooks/helpdesk",
    status_code=202,
    responses={200: {"description": "Duplicate delivery, already accepted"}},
)
async def helpdesk_webhook(
    event: HelpdeskEvent, session: SessionDep, settings: SettingsDep
) -> JSONResponse:
    """Accept a new ticket. Only persists it; the draft is generated in the background.

    202 = new event stored, 200 = duplicate (already stored). Both tell the helpdesk to stop
    retrying. 503 = could not store it within the time budget: the helpdesk delivers it
    again (if the commit did land just before the deadline, the retry gets 200).
    """
    reply_url = str(event.reply_url)
    if not settings.is_reply_url_allowed(reply_url):
        raise HTTPException(status_code=422, detail="reply_url host is not allowed")
    try:
        # One deadline for everything: pool checkout, connect, statements, commit.
        async with asyncio.timeout(settings.webhook_timeout_seconds):
            created = await ingest_event(
                session,
                IncomingEvent(
                    event_id=event.event_id,
                    ticket_id=event.ticket_id,
                    customer_email=event.customer_email,
                    message=event.message,
                    reply_url=reply_url,
                ),
                settings,
            )
    except TimeoutError:
        log.warning(
            "event %s not stored within %.1fs, asking the helpdesk to retry",
            event.event_id,
            settings.webhook_timeout_seconds,
        )
        return _unavailable("timeout")
    except (SQLAlchemyError, OSError) as exc:
        log.error("failed to store event %s: %s", event.event_id, exc)
        return _unavailable("temporarily unavailable")

    if created:
        log.info("accepted event %s (ticket %s)", event.event_id, event.ticket_id)
        return JSONResponse({"status": "accepted", "event_id": event.event_id}, status_code=202)
    log.info("duplicate event %s ignored", event.event_id)
    return JSONResponse({"status": "duplicate", "event_id": event.event_id}, status_code=200)
