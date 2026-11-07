import asyncio
import time

from app.llm.rate_limiter import PostgresRateLimiter


async def test_limit_is_enforced_within_window(sessionmaker):
    limiter = PostgresRateLimiter(sessionmaker, limit=3, window_seconds=1.0)
    started = time.monotonic()
    for _ in range(3):
        await limiter.acquire()

    assert time.monotonic() - started < 0.8

    # The 4th has to wait for the oldest slot to leave the window.
    await limiter.acquire()
    assert time.monotonic() - started >= 0.9


async def test_limit_is_shared_between_limiter_instances(sessionmaker):
    """Two instances stand in for two worker processes: the budget is global."""
    a = PostgresRateLimiter(sessionmaker, limit=4, window_seconds=1.0)
    b = PostgresRateLimiter(sessionmaker, limit=4, window_seconds=1.0)
    started = time.monotonic()
    await asyncio.gather(a.acquire(), a.acquire(), b.acquire(), b.acquire())
    assert time.monotonic() - started < 0.8

    await asyncio.gather(a.acquire(), b.acquire())
    assert time.monotonic() - started >= 0.9


async def test_concurrent_acquires_never_exceed_limit(sessionmaker):
    limiter = PostgresRateLimiter(sessionmaker, limit=5, window_seconds=1.0)
    stamps: list[float] = []

    async def take() -> None:
        await limiter.acquire()
        stamps.append(time.monotonic())

    await asyncio.gather(*(take() for _ in range(12)))
    stamps.sort()
    # In any 1-second window at most 5 acquisitions happened.
    for i in range(len(stamps) - 5):
        assert stamps[i + 5] - stamps[i] >= 0.95
