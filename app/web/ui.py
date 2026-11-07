"""Minimal server-rendered operator UI."""

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


def _back(status: str | None, **params: str) -> RedirectResponse:
    query = {k: v for k, v in {"status": status or "", **params}.items() if v}
    url = "/ui/tickets" + (f"?{urlencode(query)}" if query else "")
    return RedirectResponse(url, status_code=303)


async def _run(action_name: str, coro, status: str | None) -> RedirectResponse:
    try:
        await coro
    except tickets.TicketNotFound:
        return _back(status, error="Звернення не знайдено.")
    except tickets.StaleDraft:
        return _back(
            status,
            error=(
                "Чернетку змінено, поки сторінка була відкрита: згенеровано нову версію. "
                "Оновіть сторінку й перевірте текст перед підтвердженням."
            ),
        )
    except tickets.InvalidTransition as exc:
        label = STATUS_LABELS.get(TicketStatus(exc.status), exc.status)
        return _back(status, error=f"Дію не виконано: звернення вже має статус «{label}».")
    except ValueError as exc:
        return _back(status, error=str(exc))
    return _back(status, notice=NOTICES[action_name])


@router.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse("/ui/tickets", status_code=307)


@router.get("/ui/tickets", response_class=HTMLResponse)
async def tickets_page(
    request: Request,
    session: SessionDep,
    status: str | None = None,
    notice: str | None = None,
    error: str | None = None,
):
    selected = TicketStatus(status) if status in TicketStatus._value2member_map_ else None
    items = await tickets.list_tickets(session, selected, limit=100)
    counts = await tickets.count_by_status(session)
    return templates.TemplateResponse(
        request,
        "tickets.html",
        {
            "tickets": items,
            "counts": counts,
            "total": sum(counts.values()),
            "selected": selected,
            "labels": STATUS_LABELS,
            "statuses": list(TicketStatus),
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
):
    return await _run(
        "approve", tickets.approve(session, ticket_id, settings, text, draft_version), status
    )


@router.post("/ui/tickets/{ticket_id}/reject")
async def reject(
    ticket_id: int,
    session: SessionDep,
    reason: str = Form(""),
    status: str | None = Form(None),
):
    return await _run("reject", tickets.reject(session, ticket_id, reason), status)


@router.post("/ui/tickets/{ticket_id}/regenerate")
async def regenerate(
    ticket_id: int, session: SessionDep, settings: SettingsDep, status: str | None = Form(None)
):
    return await _run("regenerate", tickets.regenerate(session, ticket_id, settings), status)


@router.post("/ui/tickets/{ticket_id}/retry-send")
async def retry_send(
    ticket_id: int, session: SessionDep, settings: SettingsDep, status: str | None = Form(None)
):
    return await _run("retry_send", tickets.retry_send(session, ticket_id, settings), status)
