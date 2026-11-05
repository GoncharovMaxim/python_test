"""Background worker: `python -m app.worker`."""

import asyncio
import contextlib
import signal

import httpx

from app.config import get_settings
from app.db import make_engine, make_sessionmaker
from app.llm.client import LLMClient
from app.llm.rate_limiter import PostgresRateLimiter
from app.log_config import configure_logging
from app.worker.handlers import WorkerContext
from app.worker.runner import Worker


async def main() -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url, pool_size=settings.db_pool_size)
    sessionmaker = make_sessionmaker(engine)
    limiter = PostgresRateLimiter(sessionmaker, settings.llm_rate_limit_per_minute)
    llm = LLMClient(settings, limiter)
    try:
        async with httpx.AsyncClient(follow_redirects=False) as http:
            worker = Worker(WorkerContext(settings, sessionmaker, llm, http))
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError):  # Windows
                    loop.add_signal_handler(sig, worker.stop)
            await worker.run()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    configure_logging()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
