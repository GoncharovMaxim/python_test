from __future__ import annotations

import typing as t
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import make_engine, make_sessionmaker
from app.log_config import configure_logging
from app.web import api, ui, webhooks

if t.TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from app.config import Settings
    from app.db import SessionMaker


def create_app(
    settings: Settings | None = None, sessionmaker: SessionMaker | None = None
) -> FastAPI:
    """App factory (`uvicorn app.main:create_app --factory`). Tests inject their own DB."""
    settings = settings or get_settings()
    engine = None
    if sessionmaker is None:
        configure_logging()
        engine = make_engine(
            settings.database_url, pool_size=settings.db_pool_size
        )
        sessionmaker = make_sessionmaker(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="Support drafts", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.sessionmaker = sessionmaker
    app.include_router(webhooks.router)
    app.include_router(api.router)
    app.include_router(ui.router)
    return app
