"""Global (cross-process) sliding-window rate limiter backed by Postgres.

The LLM limit (60 requests/minute) applies to the whole service, not to one process,
so the counter must be shared. Each acquired slot is a row in `llm_request_log`. A
transaction-scoped advisory lock serialises "count + insert" so concurrent workers can't
both take the last slot.
"""

from __future__ import annotations

import asyncio
import logging
import random
import typing as t

from sqlalchemy import text

if t.TYPE_CHECKING:
    from app.db import SessionMaker

log = logging.getLogger(__name__)

_LOCK_KEY = (
    0x11A_5107  # arbitrary constant shared by every process using the limiter
)

_COUNT_SQL = text(
    """
    SELECT count(*) AS used,
           EXTRACT(EPOCH FROM (min(created_at) + make_interval(secs => :window)
                               - clock_timestamp())) AS wait_seconds
    FROM llm_request_log
    WHERE created_at > clock_timestamp() - make_interval(secs => :window)
    """
)


class PostgresRateLimiter:
    def __init__(
        self,
        sessionmaker: SessionMaker,
        limit: int,
        window_seconds: float = 60.0,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")

        self._sessionmaker = sessionmaker
        self._limit = limit
        self._window = float(window_seconds)

    async def acquire(self) -> None:
        """Wait until a slot is free in the current window, then take it."""
        while True:
            wait = await self._try_acquire()
            if wait is None:
                return

            # Small jitter so waiters don't stampede the lock at the same instant.
            await asyncio.sleep(wait + random.uniform(0.01, 0.1))

    async def _try_acquire(self) -> float | None:
        async with self._sessionmaker.begin() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY}
            )
            row = (
                await session.execute(_COUNT_SQL, {"window": self._window})
            ).one()
            if row.used < self._limit:
                await session.execute(
                    text("INSERT INTO llm_request_log DEFAULT VALUES")
                )
                await session.execute(
                    text(
                        "DELETE FROM llm_request_log "
                        "WHERE created_at < clock_timestamp() - make_interval(secs => :keep)"
                    ),
                    {"keep": self._window * 10},
                )
                return None

            wait = max(float(row.wait_seconds or 0.0), 0.0)
            log.debug(
                "LLM rate limit reached (%d/%d), waiting %.2fs",
                row.used,
                self._limit,
                wait,
            )
            return wait
