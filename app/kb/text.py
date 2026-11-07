"""Language-light text normalisation for knowledge-base search.

PostgreSQL ships no Ukrainian stemmer, so we normalise words in Python and store the
result in a `simple`-config tsvector. The same `terms()` function is applied to articles
(at seed time) and to customer messages (at query time).

The stemmer is deliberately crude: strip one common inflectional ending, then cut the
word to a fixed-length prefix. Prefix truncation is a known strong baseline for highly
inflected languages, and for short support questions over a small KB it gives good recall.
Precision comes from ranking (title terms weigh more) and from the LLM, which only cites
the articles it actually used.
"""

import re

STEM_LENGTH = 5
MIN_STEM_LENGTH = 3

_NORMALIZE = str.maketrans(
    {
        "'": None,
        "’": None,
        "ʼ": None,
        "`": None,
        # Russian letters -> closest Ukrainian spelling, so mixed-language messages still match
        "ё": "е",
        "э": "е",
        "ы": "и",
        "ъ": None,
    }
)
_TOKEN_RE = re.compile(r"[^\W_]+")
_CYRILLIC_RE = re.compile(r"[а-яіїєґ]")

# fmt: off
_UK_SUFFIXES = tuple(sorted({
    # verbs (no bare "ти"/"ть": they would also strip nouns, "звіти" -> "зві")
    "ювання", "ування", "увати", "ювати", "ватися", "атися", "итися",
    "ання", "ення", "іння", "ати", "яти", "ити", "іти", "ути",
    "ться", "тися", "тись", "ся", "сь",
    # nouns / adjectives
    "ами", "ями", "ові", "еві", "ого", "ому", "ими", "іми", "ою", "ею", "єю",
    "ях", "ах", "ів", "їв", "ам", "ям", "ом", "ем", "им", "ім", "их", "іх",
    "ий", "ій", "ія", "ії", "ію", "ей",
    "а", "я", "у", "ю", "і", "и", "о", "е", "ь", "й", "є", "ї",
}, key=len, reverse=True))
# fmt: on
_EN_SUFFIXES = ("ing", "ed", "es", "s")

STOPWORDS = frozenset(
    """
    а або аби але б би бо був була були було буде будь в вам вас ваш ваша ваше ваші весь
    від вже все всі ви він вона вони воно де для до добрий день дякую є ж же з за зараз
    зі і із й його її їх коли ласка лише мене мені ми мій мною моє мої моя може можна
    можу мого на нам нас наш не немає неї ні ніж ну о от по повинен при привіт про раз
    сам свій своє свої свою себе так також там тим тільки то тобто треба тут у усі хочу
    це цей ці цим цього цю ця чи щоб що як який яка яке які якщо я ще вітаю
    підкажіть скажіть будьласка допоможіть потрібно
    и в не на я что как мне у с по это но мы вы он она они меня нас вас
    the a an and or to of in on for is are be with my i we you it this that not can
    please hello hi how
    """.split()
)


def stem(word: str) -> str:
    """Reduce a single lower-cased token to its search term."""
    w = word.lower().translate(_NORMALIZE)
    suffixes = _UK_SUFFIXES if _CYRILLIC_RE.search(w) else _EN_SUFFIXES
    for suffix in suffixes:
        if w.endswith(suffix) and len(w) - len(suffix) >= MIN_STEM_LENGTH:
            w = w[: -len(suffix)]
            break

    return w[:STEM_LENGTH]


def terms(text: str) -> list[str]:
    """Tokenise, drop stop words and stem. Output tokens contain only letters/digits."""
    normalized = text.lower().translate(_NORMALIZE)
    return [
        stem(token)
        for token in _TOKEN_RE.findall(normalized)
        if len(token) >= 2 and token not in STOPWORDS
    ]
