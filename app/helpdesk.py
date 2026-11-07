"""Client for the helpdesk reply API."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({408, 409, 425, 429})


@dataclass(frozen=True)
class SendOutcome:
    delivered: bool
    retryable: bool = False
    retry_after: float | None = None
    error: str | None = None
    status_code: int | None = None


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


async def send_reply(
    client: httpx.AsyncClient,
    *,
    reply_url: str,
    ticket_id: str,
    message: str,
    idempotency_key: str,
    timeout_seconds: float,
) -> SendOutcome:
    """POST the reply. The same `idempotency_key` must be used for every retry of a reply.

    A timeout is ambiguous: the helpdesk may have stored the reply and only the response
    was lost. That's why it's retried with the same key, never with a new one.
    """
    try:
        response = await client.post(
            reply_url,
            json={"ticket_id": ticket_id, "message": message},
            headers={"Idempotency-Key": idempotency_key},
            timeout=timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        return SendOutcome(
            delivered=False,
            retryable=True,
            error=f"timeout, delivery state unknown ({exc!r})",
        )
    except httpx.TransportError as exc:
        return SendOutcome(
            delivered=False, retryable=True, error=f"transport error: {exc!r}"
        )

    code = response.status_code
    if 200 <= code < 300:
        return SendOutcome(delivered=True, status_code=code)

    body = response.text[:300]
    retryable = code >= 500 or code in RETRYABLE_STATUSES
    return SendOutcome(
        delivered=False,
        retryable=retryable,
        retry_after=_retry_after(response) if retryable else None,
        error=f"helpdesk returned HTTP {code}: {body}",
        status_code=code,
    )
