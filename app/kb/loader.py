"""Reads KB articles from Markdown files with a minimal `key: value` front matter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArticleSource:
    slug: str
    title: str
    body: str


def parse_article(text: str, default_slug: str) -> ArticleSource:
    meta: dict[str, str] = {}
    body = text
    lines = text.lstrip("\ufeff").splitlines()  # tolerate a UTF-8 BOM
    if lines and lines[0].strip() == "---":
        try:
            end = next(
                i
                for i, line in enumerate(lines[1:], start=1)
                if line.strip() == "---"
            )
        except StopIteration as exc:
            raise ValueError(
                f"unterminated front matter in {default_slug}"
            ) from exc

        for line in lines[1:end]:
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip()] = value.strip()

        body = "\n".join(lines[end + 1 :])

    title = meta.get("title")
    if not title:
        raise ValueError(f"article {default_slug} has no title")

    return ArticleSource(
        slug=meta.get("slug") or default_slug, title=title, body=body.strip()
    )


def load_articles(directory: str | Path) -> list[ArticleSource]:
    paths = sorted(Path(directory).glob("*.md"))
    articles = [
        parse_article(p.read_text(encoding="utf-8"), default_slug=p.stem)
        for p in paths
    ]
    slugs = [a.slug for a in articles]
    if len(slugs) != len(set(slugs)):
        raise ValueError("duplicate article slugs in knowledge base")

    return articles
