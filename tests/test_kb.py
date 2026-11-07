import pytest

from app.kb.loader import load_articles, parse_article
from app.kb.repository import search_articles
from tests.conftest import ROOT


@pytest.mark.parametrize(
    ("query", "expected_slug"),
    (
        ("Не можу експортувати звіт у PDF", "export-report-pdf"),
        ("Як вивантажити таблицю в Excel?", "export-data-excel-csv"),
        ("Забув пароль, лист не приходить", "login-password-2fa"),
        ("Скільки коштує тариф Команда?", "plans-and-billing"),
        (
            "Хочу скасувати підписку і повернути гроші",
            "cancel-subscription-refund",
        ),
        ("Як запросити колегу?", "team-invite-roles"),
        ("Не підключається база PostgreSQL, timeout", "connect-data-sources"),
        ("Не могу экспортировать отчет в PDF", "export-report-pdf"),
    ),
)
async def test_search_finds_relevant_article(
    sessionmaker, query, expected_slug
):
    async with sessionmaker() as session:
        found = await search_articles(session, query, limit=3)

    assert found, query
    assert found[0].slug == expected_slug
    assert found[0].url == f"https://kb.test/articles/{expected_slug}"


async def test_search_without_meaningful_terms_returns_nothing(sessionmaker):
    async with sessionmaker() as session:
        assert await search_articles(session, "!!! ??? і у", limit=3) == []


def test_kb_has_between_5_and_10_articles():
    articles = load_articles(ROOT / "kb")
    assert 5 <= len(articles) <= 10
    assert all(a.title and len(a.body) > 100 for a in articles)


def test_parse_article_front_matter():
    article = parse_article(
        "---\nslug: s1\ntitle: Заголовок\n---\nТекст\n", default_slug="x"
    )
    assert (article.slug, article.title, article.body) == (
        "s1",
        "Заголовок",
        "Текст",
    )
    with pytest.raises(ValueError):
        parse_article("---\nslug: s1\n---\nТекст", default_slug="x")
