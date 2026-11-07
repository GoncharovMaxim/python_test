---
description: Python code style, ruff linting, ty type checking, typos, pre-commit hooks, formatting rules
paths:
  - "**/*.py"
  - "pyproject.toml"
  - ".pre-commit-config.yaml"
---

# Code Style & Linting

## Python (ruff)

- Line length: **79** characters, as configured in `pyproject.toml` (ruff handles formatting; `E501` is ignored in the linter, so long strings may exceed it).
- Target: Python 3.12 (`py312`). The runtime image is `python:3.12-slim`; don't use syntax newer than 3.12.
- Quote style: double quotes, space indentation, LF line endings.
- Imports: absolute only; isort with `known-first-party = ["app", "helpdesk_mock", "scripts", "tests"]`; `combine-as-imports = true`.
- Enabled rules: `E`/`W`/`F`, `S` (bandit), `B` (bugbear), `A` (builtins), `C4` (comprehensions), `DTZ` (datetimez), `T20` (print), `RET` (return), `SIM` (simplify), `UP` (pyupgrade), `FURB` (refurb), `I` (isort), `N` (naming), `PTH` (pathlib), `ANN` (annotations), `TC` (type-checking imports, `strict = true`), `ICN` (`import typing as t`, `from typing import ...` is banned), `ASYNC` (blocking calls in async code).
- Not enabled: `C901` (complexity), `RUF`, `TRY`.
- `ANN` is enabled except `ANN002`/`ANN003`, so pass-through `*args`/`**kwargs` stay unannotated; `ANN401` forbids bare `Any`.
- Per-file ignores: `tests/**` relax `ANN`, `B017`, `F601`, `S101` (pytest asserts), `S105`/`S106` (fake secrets). `S311` is off in `helpdesk_mock/**`, `app/queue.py`, `app/llm/*.py`, where `random` only drives simulated failures, jitter, and simulated latency.
- Logging: one module-level `log = logging.getLogger(__name__)` per module; never `print`.

## Python Typing

**Production code is fully typed.** Every new function, method, and class attribute outside tests is fully typed: all parameters (except pass-through `*args` / `**kwargs`) and the return type.

**Tests are not annotated.** Don't add type annotations anywhere under `tests/`: not on test functions (`-> None`), fixtures, helpers, or fakes. Tests are excluded from ty and from ruff's `ANN` rules, so annotations there are never checked and only add noise.

**Touching a production function or class means typing it.** Any code change inside a function, method, or class body outside tests includes adding or completing the annotations of that function or class if it isn't fully typed yet. This covers a bug fix, a one-line tweak, or a rename. Don't leave a partially typed signature behind just because the task was about something else. Ruff's `ANN` rules enforce this in the hook chain.

- Import generic collection ABCs such as `Iterable`, `Sequence`, `Mapping`, `Callable`, and `Set` from `collections.abc`, not from `typing`.
- Use the repo convention `import typing as t` (`t.Any`, `t.Protocol`, `t.Annotated`, `t.Self`). `from typing import ...` is rejected by ruff (`ICN003`).
- Put type-only imports under `if t.TYPE_CHECKING:` when they are not needed at runtime.
- Start every module that has an import used only in annotations with `from __future__ import annotations` (first statement after the module docstring). With it, those imports move under `if t.TYPE_CHECKING:` and forward references need no quotes. Ruff's TC001–TC003 only fire once the future import is present, so add it before running the hooks. Ruff marks these fixes unsafe, so apply them with `ruff check --fix --unsafe-fixes --select TC` and review the result.
- **Exception: modules whose annotations are evaluated at runtime.** Don't add the future import there:
  - FastAPI routers and dependencies (`app/web/*`, `helpdesk_mock/main.py`). FastAPI reads endpoint signatures to inject dependencies and validate requests.
  - pydantic models and settings (`app/config.py`, request/response models).
  - SQLAlchemy models (`app/models.py`, `Mapped[...]`).

  Their annotation imports must stay real imports. For pydantic/SQLAlchemy classes, ruff knows this from `runtime-evaluated-base-classes`.
- `flake8-type-checking.strict = true` is on, so a typing-only name is flagged even when the same module is also imported for runtime names (`from sqlalchemy import ColumnElement` goes under `t.TYPE_CHECKING` even though `from sqlalchemy import func` stays).
- Guard type-only imports with `if t.TYPE_CHECKING:`, never with `from typing import TYPE_CHECKING` / `if TYPE_CHECKING:`. Ruff's auto-fix writes the latter when the file has no guard yet: rewrite it to the `t.` form.
- Methods that return their own instance (builders, fluent helpers) are annotated `-> t.Self`.
- Prefer PEP 695 generic syntax (Python 3.12) instead of introducing one-off `TypeVar`s only for a single declaration.

```python
# WRONG
from typing import Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import Ticket


# CORRECT
from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:
    from collections.abc import Iterable

    from app.models import Ticket
```

```python
# WORSE: one-off TypeVar only for a single declaration
_T = TypeVar("_T")


def first_or_none(values: Sequence[_T]) -> _T | None:
    return values[0] if values else None


# BETTER: a good fit for PEP 695
def first_or_none[_T](values: Sequence[_T]) -> _T | None:
    return values[0] if values else None
```

## Function Typing

Follow the [typing best practices](https://typing.python.org/en/latest/reference/best_practices.html):

- prefer narrower, more concrete return types;
- prefer wider argument types based on actual usage.

For arguments, use the narrowest abstract type from `collections.abc` that covers what the function (and any function it passes the value to) actually does with it, not what the caller happens to pass:

- `Container` when only `x in value` is used
- `Sized` when only `len()` or truthiness is used
- `Iterable` for a single pass (one `for` loop, comprehension, `set()`, `join`, `in_()` filter)
- `Collection` for a combination of those or more than one pass
- `Reversible` when only `reversed()` and iteration are used
- `Sequence` only when the value is indexed or sliced
- `Mapping` / `MutableMapping` when reading/updating mapping-like input

FastAPI endpoint parameters and pydantic fields are the exception: FastAPI and pydantic validate them, so they need concrete types (`list[int]`, `dict[str, str]`), not ABCs.

Don't widen return types just to mirror argument flexibility.

```python
# WRONG: return type is needlessly wide
def build_payload() -> Mapping[str, str]:
    return {"id": "1"}


# CORRECT: concrete return type
def build_payload() -> dict[str, str]:
    return {"id": "1"}
```

```python
# WRONG: argument is stricter than the implementation needs
def collect_ids(values: list[int]) -> list[int]:
    return [value for value in values if value > 0]


# CORRECT: argument type matches actual usage
def collect_ids(values: Iterable[int]) -> list[int]:
    return [value for value in values if value > 0]


def is_allowed(code: str, allowed_codes: Container[str]) -> bool:
    return code in allowed_codes
```

## Imports And Runtime Dependencies

Don't use function-level imports unless there is a verified circular dependency or a measured startup/runtime reason. Default to module-top imports that follow isort ordering.

```python
# WRONG
async def stats(session: AsyncSession) -> dict[str, int]:
    from app.tickets import count_by_status

    return await count_by_status(session)


# CORRECT
from app.tickets import count_by_status


async def stats(session: AsyncSession) -> dict[str, int]:
    return await count_by_status(session)
```

## Primitive Enums (StrEnum / IntEnum)

Don't call `.value` on a `StrEnum` or `IntEnum` member (`TicketStatus`, `JobKind`, `JobStatus`). The member *is* its primitive: `StrEnum` subclasses `str`, so comparison, formatting, `json.dumps`, SQLAlchemy column assignment, and Jinja rendering all treat the member and its value identically. `.value` only adds noise, and mixing the two styles in one expression makes it look as though the branches produce different types.

Use `.value` only where an API rejects or renders incorrectly a subclass of `str`/`int`, e.g. `repr()` when building SQL text (`repr(TicketStatus.NEW)` is `<TicketStatus.NEW: 'new'>`). That case deserves a nearby comment.

```python
# WRONG
counts = {status.value: 0 for status in TicketStatus}

# CORRECT
counts = {status: 0 for status in TicketStatus}
```

This holds for plain `enum.Enum` too, except that there `.value` *is* required, because a plain `Enum` member is not its value.

## Truthiness And None Checks

Default to a plain truthiness check: `if value:` / `if not value:`. This holds for values that may be `None` exactly as it does for booleans. Nobody writes `if flag is True:`, and an optional value needs no more ceremony than a flag.

Where the `is None` habit comes from: PEP 8 forbids `== None` and says to compare with singletons using `is`. That rule chooses between `==` and `is`; it never says to prefer `is None` over truthiness. `== None` / `== True` / `== False` stay wrong.

`X | None` in the annotation is not a reason for `is None`. ty narrows `if value:` to `X` just like `if value is not None:`.

Use `is None` / `is not None` / `is True` / `is False` only when **both** of these hold (which is rare):

1. `None` means something different from another falsy value (`False`, `0`, `""`, empty collection), and
2. that other falsy value is a real, valid case for the contract.

Typical cases for an exact check:

- Tri-state boolean: `None` = not decided yet, `False` = decided "no".
- Numbers where `0` is valid: limits, offsets, attempts, delays (`retry_after=0` is a real value).
- Empty string or collection with its own meaning: `ids=None` means "no filter", `ids=()` means "match nothing".
- Cache/sentinel slots whose computed value may be falsy (`0`, `[]`).
- `dict.get()` when a stored value may itself be falsy.

When unsure, ask whether `0`, `""`, `False`, or `[]` would ever reach this line and need different handling from `None`. If not, use truthiness.

```python
# WRONG: None is the only falsy value a model instance can have
if ticket is not None:
    status = ticket.status

# CORRECT
if ticket:
    status = ticket.status
```

```python
# CORRECT: 0 is a valid offset, so only None falls back to the default
if offset is None:
    offset = DEFAULT_OFFSET

# WRONG: offset=0 is silently replaced by the default
offset = offset or DEFAULT_OFFSET
```

## Fixed Sequences And Literals

- Prefer tuples over lists for fixed/immutable sequence declarations and expectations.
- Prefer single-line literals and returns when they fit comfortably within the 79-character line length.
- Prefer `...` for ellipsis over `Ellipsis` or `object()` placeholders.

## Materialization And Copies

Avoid materializing query results or iterables with `.all()` / `list()` / `dict()` unless the copy is required for correctness, serialization, or repeated consumption. Ask the database for what you need (`exists()`, `count()`, `LIMIT`) instead of loading rows and checking them in Python.

A copy is justified when:

- the iterator will be consumed more than once;
- the downstream API requires a concrete container;
- the data must be detached from the session before slow I/O (e.g. read what you need, close the session, then call the LLM).

```python
# WRONG: loads every id to answer a yes/no question
ids = (await session.scalars(select(Ticket.id))).all()
return any(ids)

# CORRECT
return bool(await session.scalar(select(exists().where(Ticket.id > 0))))
```

## Blank Line After Blocks (custom pre-commit hook)

An `ast`-based hook (`scripts/blank_line_after_blocks.py`) enforces a blank line after `if`/`for`/`while`/`with`/`try`/`match` blocks when they are followed by another statement at the same indentation level. It fixes files in place.

This is **not** a ruff rule; it runs as a separate pre-commit hook.

```python
# WRONG
if condition:
    do_something()
next_statement()

# CORRECT
if condition:
    do_something()

next_statement()
```

## Type Checking (ty)

- Configured under `[tool.ty]` in `pyproject.toml`: checks `app`, `helpdesk_mock`, `scripts`; excludes `migrations` and `tests`.
- Run it via the hook (`pre-commit run ty --all-files`) or `ty check` with the dev virtualenv active. ty uses that virtualenv, so third-party types (FastAPI, SQLAlchemy, pydantic, anthropic) are resolved and really checked. Keep `requirements-dev.txt` installed in it.
- **Suppressions are banned.** `# ty: ignore`, `# type: ignore`, and `# noqa` are all rejected by the `no-suppressions` hook. Fix the type, widen the signature, narrow explicitly (`if ticket and ticket.status == ...`), or introduce a `t.Protocol`.
- Convention: see "Python Typing" above. All new production code is fully typed, and any production function, method, or class you touch gets its missing annotations added in the same change (enforced by ruff's `ANN` rules). Tests are never annotated.

### Patterns that keep ty quiet without lying

- Narrowing through an intermediate variable (`status = ticket.status if ticket else None`, then `if status == ...: ticket.message`) doesn't narrow `ticket`. Test the object itself.
- `@asynccontextmanager` functions return `AsyncGenerator[T]`, not `AsyncIterator[T]`.
- A collaborator that is only called (a rate limiter, a clock): type it with a `t.Protocol` instead of the concrete class (see `RateLimiter` in `app/llm/client.py`).
- An attribute populated after `__init__`: declare it without assigning (`self.foo: Bar`) rather than initialising it to `None`.

## Spell Checking (typos)

- Configured under `[tool.typos]` in `pyproject.toml`. Excluded: `kb/*.md` and `docs/task.md` (Ukrainian prose).
- File names are checked too, so a misspelled file name fails the hook.

## Pre-commit Hook Chain (runs on the host, outside Docker)

Install once: `pip install -r requirements-dev.txt && pre-commit install`. Run: `pre-commit run --files <files>` or `pre-commit run --all-files`.

1. `typos`: spell check (fixes unambiguous typos in place)
2. `ruff-check --fix`: lint with auto-fix
3. `ruff-format`: format
4. `blank-line-after-blocks`: custom style rule (stdlib `ast`)
5. `no-suppressions`: rejects `# noqa`, `# type: ignore`, `# ty: ignore`
6. `ty`: type check

Dependencies are pinned in `requirements*.txt` (no uv lock file to validate).

## No Log Noise In Tests

pytest captures log output, but a test that deliberately triggers a warning/error path must still **assert** the log, so an unexpected error can't hide behind an expected one.

- Preferred: pytest's `caplog` fixture. Assert the level and the message.
- Also acceptable: patch the module's `log` object and assert the call.
- New, unasserted error logs in a test run are a defect to fix in the change that introduced them.

```python
# WRONG: the expected error is logged but never checked
async def test_send_gives_up_after_max_attempts(client, make_ctx):
    ...
    assert ticket.status == TicketStatus.SEND_FAILED


# CORRECT
async def test_send_gives_up_after_max_attempts(client, make_ctx, caplog):
    ...
    assert ticket.status == TicketStatus.SEND_FAILED
    assert any(
        r.levelname == "ERROR" and "failed permanently" in r.getMessage()
        for r in caplog.records
    )
```
