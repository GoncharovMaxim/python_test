"""Operator JSON API (the HTML UI uses the same use-cases)."""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app import tickets
from app.models import TicketStatus
from app.web.deps import SessionDep, SettingsDep

router = APIRouter(prefix="/api", tags=["operator"])


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_id: str
    external_ticket_id: str
    customer_email: str
    message: str
    status: TicketStatus
    draft_text: str | None
    draft_sources: list[dict[str, Any]]
    draft_model: str | None
    reply_text: str | None
    reply_idempotency_key: uuid.UUID | None
    rejection_reason: str | None
    last_error: str | None
    created_at: datetime
    draft_generated_at: datetime | None
    approved_at: datetime | None
    sent_at: datetime | None


class ApproveIn(BaseModel):
    text: str | None = None  # None = send the draft as is


class RejectIn(BaseModel):
    reason: str | None = None


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, tickets.TicketNotFound):
        return HTTPException(404, "ticket not found")
    if isinstance(exc, tickets.InvalidTransition):
        return HTTPException(409, str(exc))
    return HTTPException(422, str(exc))


_ERRORS = (tickets.TicketNotFound, tickets.InvalidTransition, ValueError)


@router.get("/tickets", response_model=list[TicketOut])
async def list_tickets(
    session: SessionDep,
    status: TicketStatus | None = None,
    limit: int = Query(100, ge=1, le=500),
):
    return await tickets.list_tickets(session, status, limit)


@router.get("/tickets/{ticket_id}", response_model=TicketOut)
async def get_ticket(ticket_id: int, session: SessionDep):
    try:
        return await tickets.get_ticket(session, ticket_id)
    except _ERRORS as exc:
        raise _http_error(exc) from exc


@router.get("/stats")
async def stats(session: SessionDep) -> dict[str, int]:
    return await tickets.count_by_status(session)


async def _act_and_return(session, ticket_id: int, action) -> Any:
    try:
        await action
        return await tickets.get_ticket(session, ticket_id)
    except _ERRORS as exc:
        raise _http_error(exc) from exc


@router.post("/tickets/{ticket_id}/approve", response_model=TicketOut)
async def approve(ticket_id: int, body: ApproveIn, session: SessionDep, settings: SettingsDep):
    return await _act_and_return(
        session, ticket_id, tickets.approve(session, ticket_id, settings, body.text)
    )


@router.post("/tickets/{ticket_id}/reject", response_model=TicketOut)
async def reject(ticket_id: int, body: RejectIn, session: SessionDep):
    return await _act_and_return(
        session, ticket_id, tickets.reject(session, ticket_id, body.reason)
    )


@router.post("/tickets/{ticket_id}/regenerate", response_model=TicketOut)
async def regenerate(ticket_id: int, session: SessionDep, settings: SettingsDep):
    return await _act_and_return(
        session, ticket_id, tickets.regenerate(session, ticket_id, settings)
    )


@router.post("/tickets/{ticket_id}/retry-send", response_model=TicketOut)
async def retry_send(ticket_id: int, session: SessionDep, settings: SettingsDep):
    return await _act_and_return(
        session, ticket_id, tickets.retry_send(session, ticket_id, settings)
    )
