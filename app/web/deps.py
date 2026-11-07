import typing as t
from collections.abc import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings


async def _session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def _settings(request: Request) -> Settings:
    return request.app.state.settings


SessionDep = t.Annotated[AsyncSession, Depends(_session)]
SettingsDep = t.Annotated[Settings, Depends(_settings)]
