"""Minimal server-rendered operator UI."""

import math
from collections.abc import Awaitable
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import tickets
from app.models import TicketStatus
from app.web.deps import SessionDep, SettingsDep

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

PAGE_SIZE = 50

STATUS_LABELS = {
    TicketStatus.NEW: "Генерується чернетка",
    TicketStatus.DRAFT_READY: "Чекає на модерацію",
    TicketStatus.APPROVED: "Надсилається",
    TicketStatus.SENT: "Надіслано",
    TicketStatus.REJECTED: "Відхилено",
    TicketStatus.GENERATION_FAILED: "Помилка генерації",
    TicketStatus.SEND_FAILED: "Помилка надсилання",
}

NOTICES = {
    "approve": "Відповідь підтверджено й поставлено в чергу на надсилання.",
    "reject": "Чернетку відхилено.",
    "regenerate": "Чернетку поставлено в чергу на повторну генерацію.",
    "retry_send": "Надсилання поставлено в чергу повторно.",
}


def _parse_status(raw: str | None) -> TicketStatus | None:
    try:
        return TicketStatus(raw) if raw else None
    except ValueError:  # an unknown filter shows all tickets
        return None


def _parse_page(raw: int | str | None) -> int:
    try:
        return max(int(raw or 1), 1)
    except ValueError:
        return 1


def list_url(
    status: str | None, page: int | str | None = None, **params: str
) -> str:
    """URL of the ticket list that keeps the status filter and the page."""
    page_number = _parse_page(page)
    query = {
        "status": status or "",
        "page": str(page_number) if page_number > 1 else "",
        **params,
    }
    query = {k: v for k, v in query.items() if v}
    return "/ui/tickets" + (f"?{urlencode(query)}" if query else "")


def _back(
    status: str | None, page: str | None, **params: str
) -> RedirectResponse:
    return RedirectResponse(list_url(status, page, **params), status_code=303)


async def _run(
    action_name: str,
    coro: Awaitable[None],
    status: str | None,
    page: str | None,
) -> RedirectResponse:
    try:
        await coro
    except tickets.TicketNotFoundError:
        return _back(status, page, error="Звернення не знайдено.")
    except tickets.StaleDraftError:
        return _back(
            status,
            page,
            error=(
                "Чернетку змінено, поки сторінка була відкрита: згенеровано нову версію. "
                "Оновіть сторінку й перевірте текст перед підтвердженням."
            ),
        )
    except tickets.InvalidTransitionError as exc:
        label = STATUS_LABELS.get(exc.status, exc.status)
        return _back(
            status,
            page,
            error=f"Дію не виконано: звернення вже має статус «{label}».",
        )
    except ValueError as exc:
        return _back(status, page, error=str(exc))

    return _back(status, page, notice=NOTICES[action_name])


@router.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse("/ui/tickets", status_code=307)


@router.get("/ui/tickets", response_class=HTMLResponse)
async def tickets_page(
    request: Request,
    session: SessionDep,
    status: str | None = None,
    page: str | None = None,
    notice: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    selected = _parse_status(status)
    counts = await tickets.count_by_status(session)
    total = sum(counts.values())
    matching = counts[selected] if selected else total
    pages = max(math.ceil(matching / PAGE_SIZE), 1)
    # Clamp, e.g. when an action emptied the last page.
    current = min(_parse_page(page), pages)
    items = await tickets.list_tickets(
        session, selected, limit=PAGE_SIZE, offset=(current - 1) * PAGE_SIZE
    )
    return templates.TemplateResponse(
        request,
        "tickets.html",
        {
            "tickets": items,
            "counts": counts,
            "total": total,
            "matching": matching,
            "page": current,
            "pages": pages,
            "prev_url": list_url(selected, current - 1)
            if current > 1
            else None,
            "next_url": list_url(selected, current + 1)
            if current < pages
            else None,
            "selected": selected,
            "labels": STATUS_LABELS,
            "statuses": TicketStatus,
            "notice": notice,
            "error": error,
        },
    )


@router.post("/ui/tickets/{ticket_id}/approve")
async def approve(
    ticket_id: int,
    session: SessionDep,
    settings: SettingsDep,
    text: str = Form(...),
    draft_version: int = Form(...),
    status: str | None = Form(None),
    page: str | None = Form(None),
) -> RedirectResponse:
    return await _run(
        "approve",
        tickets.approve(session, ticket_id, settings, text, draft_version),
        status,
        page,
    )


@router.post("/ui/tickets/{ticket_id}/reject")
async def reject(
    ticket_id: int,
    session: SessionDep,
    reason: str = Form(""),
    status: str | None = Form(None),
    page: str | None = Form(None),
) -> RedirectResponse:
    return await _run(
        "reject", tickets.reject(session, ticket_id, reason), status, page
    )


@router.post("/ui/tickets/{ticket_id}/regenerate")
async def regenerate(
    ticket_id: int,
    session: SessionDep,
    settings: SettingsDep,
    status: str | None = Form(None),
    page: str | None = Form(None),
) -> RedirectResponse:
    return await _run(
        "regenerate",
        tickets.regenerate(session, ticket_id, settings),
        status,
        page,
    )


@router.post("/ui/tickets/{ticket_id}/retry-send")
async def retry_send(
    ticket_id: int,
    session: SessionDep,
    settings: SettingsDep,
    status: str | None = Form(None),
    page: str | None = Form(None),
) -> RedirectResponse:
    return await _run(
        "retry_send",
        tickets.retry_send(session, ticket_id, settings),
        status,
        page,
    )
