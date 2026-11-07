"""Test fixtures. Tests need a real Postgres: set TEST_DATABASE_URL (the database is
dropped and re-created). In Docker: `docker compose --profile test run --rm tests`."""

import asyncio
import logging
import os
from pathlib import Path

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.db import make_engine, make_sessionmaker
from app.kb.loader import load_articles
from app.kb.repository import sync_articles
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DB = (
    "postgresql+asyncpg://support:support@localhost:5432/support_test"
)
ALLOWED_HOST = "helpdesk.test"


@pytest.fixture(autouse=True)
def no_unasserted_logs(caplog):
    """Warnings and errors a test triggers must be asserted (codestyle: "No Log
    Noise In Tests"), so an unexpected error can't hide behind an expected one.
    Use `assert_logged()`, which also marks the record as expected."""
    yield
    unexpected = [
        f"{r.levelname} {r.name}: {r.getMessage()}"
        for r in caplog.get_records("call")
        if r.levelno >= logging.WARNING
    ]
    assert not unexpected, f"unasserted log records: {unexpected}"


def assert_logged(caplog, level, fragment):
    """Assert a `level` record containing `fragment` was logged and mark it
    as expected. Returns how many records matched."""
    matching = [
        r
        for r in caplog.records
        if r.levelname == level and fragment in r.getMessage()
    ]
    assert matching, f"no {level} log containing {fragment!r}"
    for record in matching:
        caplog.records.remove(record)

    return len(matching)


def _run_migrations(url: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
async def database_url() -> str:
    url = make_url(os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DB))
    admin = url.set(drivername="postgresql", database="postgres")
    conn = await asyncpg.connect(admin.render_as_string(hide_password=False))
    try:
        await conn.execute(
            f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'
        )
        await conn.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await conn.close()

    rendered = url.render_as_string(hide_password=False)
    await asyncio.to_thread(_run_migrations, rendered)
    return rendered


@pytest.fixture(scope="session")
async def engine(database_url):
    engine = make_engine(database_url, pool_size=20)
    async with make_sessionmaker(engine).begin() as session:
        await sync_articles(
            session, load_articles(ROOT / "kb"), "https://kb.test/articles"
        )

    yield engine
    await engine.dispose()


@pytest.fixture
async def sessionmaker(engine):
    yield make_sessionmaker(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE tickets, jobs, llm_request_log RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def settings(database_url) -> Settings:
    return Settings(
        _env_file=None,
        database_url=database_url,
        anthropic_api_key="",
        llm_stub_min_delay_seconds=0.01,
        llm_stub_max_delay_seconds=0.03,
        kb_dir=str(ROOT / "kb"),
        kb_public_base_url="https://kb.test/articles",
        reply_url_allowed_hosts=ALLOWED_HOST,
        generation_backoff_base_seconds=0.01,
        generation_backoff_cap_seconds=0.05,
        send_backoff_base_seconds=0.01,
        send_backoff_cap_seconds=0.05,
        worker_poll_interval_seconds=0.05,
    )


@pytest.fixture
async def client(settings, sessionmaker):
    app = create_app(settings=settings, sessionmaker=sessionmaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client


def make_event(n: int = 1, **overrides) -> dict:
    event = {
        "event_id": f"evt_{n:04d}",
        "ticket_id": f"T-{1000 + n}",
        "customer_email": "user@example.com",
        "message": "Не можу експортувати звіт у PDF",
        "reply_url": f"https://{ALLOWED_HOST}/tickets/T-{1000 + n}/replies",
    }
    event.update(overrides)
    return event
