from __future__ import annotations

import typing as t
from dataclasses import dataclass

from sqlalchemy import delete, func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.kb.text import terms
from app.models import KbArticle

if t.TYPE_CHECKING:
    from collections.abc import Collection

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.kb.loader import ArticleSource


@dataclass(frozen=True)
class FoundArticle:
    slug: str
    title: str
    body: str
    url: str
    rank: float


def article_url(base_url: str, slug: str) -> str:
    return f"{base_url.rstrip('/')}/{slug}"


async def sync_articles(
    session: AsyncSession, articles: Collection[ArticleSource], base_url: str
) -> None:
    """Upsert articles by slug and remove ones that no longer exist on disk."""
    for article in articles:
        values = {
            "slug": article.slug,
            "title": article.title,
            "body": article.body,
            "url": article_url(base_url, article.slug),
            "title_terms": " ".join(terms(article.title)),
            "body_terms": " ".join(terms(article.body)),
        }
        stmt = pg_insert(KbArticle).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[KbArticle.slug],
            set_={
                **{k: stmt.excluded[k] for k in values if k != "slug"},
                "updated_at": func.now(),
            },
        )
        await session.execute(stmt)

    await session.execute(
        delete(KbArticle).where(
            KbArticle.slug.not_in([a.slug for a in articles])
        )
    )


_SEARCH_SQL = text(
    """
    SELECT slug, title, body, url, ts_rank_cd(search_vector, q, 1) AS rank
    FROM kb_articles, to_tsquery('simple', :query) AS q
    WHERE search_vector @@ q
    ORDER BY rank DESC, id
    LIMIT :limit
    """
)


async def search_articles(
    session: AsyncSession, query: str, limit: int
) -> list[FoundArticle]:
    """OR-match of normalised query terms, ranked with title terms weighted higher."""
    query_terms = sorted(set(terms(query)))
    if not query_terms:
        return []

    # terms() yields only letters/digits, so joining with `|` is a safe tsquery.
    result = await session.execute(
        _SEARCH_SQL, {"query": " | ".join(query_terms), "limit": limit}
    )
    return [FoundArticle(**row._mapping) for row in result]
