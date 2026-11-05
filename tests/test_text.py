import pytest

from app.kb.text import stem, terms


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("звіт", "звіти"),
        ("звіту", "звітів"),
        ("експорт", "експортувати"),
        ("пароль", "паролю"),
        ("підписка", "підписку"),
        ("оплата", "оплатити"),
        ("рахунок", "рахунку"),
        ("export", "exporting"),
        ("экспорт", "експорт"),  # Russian spelling normalised to Ukrainian
    ],
)
def test_word_forms_share_a_stem(a, b):
    assert stem(a) == stem(b)


def test_stop_words_are_dropped():
    assert terms("Не можу, і у нас це не так") == []


def test_apostrophes_are_ignored():
    assert terms("м'який") == terms("мякий") == terms("м’який")


@pytest.mark.parametrize(
    "text", ["a | b & !c", "(звіт):* <-> 'x'", "user@example.com", "😀 PDF!!!"]
)
def test_terms_are_safe_for_tsquery(text):
    assert all(t.isalnum() for t in terms(text))
