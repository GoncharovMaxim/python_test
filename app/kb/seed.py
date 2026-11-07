"""Load KB articles from disk into Postgres: `python -m app.kb.seed`."""

import asyncio
import logging

from app.config import get_settings
from app.db import make_engine, make_sessionmaker
from app.kb.loader import load_articles
from app.kb.repository import sync_articles

log = logging.getLogger("kb.seed")


async def main() -> None:
    settings = get_settings()
    articles = load_articles(settings.kb_dir)
    engine = make_engine(settings.database_url, pool_size=1)
    try:
        async with make_sessionmaker(engine).begin() as session:
            await sync_articles(session, articles, settings.kb_public_base_url)
    finally:
        await engine.dispose()

    log.info("knowledge base synced: %d articles", len(articles))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s"
    )
    asyncio.run(main())
