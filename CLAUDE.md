# CLAUDE.md

Instructions for AI coding agents (Claude Code, etc.) working in this repository.

## What this is

A support-draft service: the helpdesk posts a ticket to our webhook → we find relevant
knowledge-base (KB) articles and ask an LLM for a draft reply that links to them → an
operator approves/rejects the draft in a web UI → the approved reply is posted back to
the helpdesk API.

- `docs/task.md`: the original assignment (source of truth for requirements).
- `docs/design.md`: architecture, state machines, and the reasoning behind decisions.
  **Keep it in sync** when you change behaviour.

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2 (async, asyncpg) · Alembic · PostgreSQL 16 ·
Jinja2 (server-rendered UI) · httpx · Anthropic Python SDK · docker compose.

## Layout

```
app/
  config.py          settings (env vars, see .env.example)
  db.py, models.py   engine/session factory, ORM models (tickets, jobs, kb_articles, llm_request_log)
  queue.py           durable Postgres job queue (SKIP LOCKED + leases + fencing)
  tickets.py         ticket use-cases: ingest, approve, reject, regenerate, retry-send
  drafts.py          prompt, JSON schema, parsing, rendering of KB links
  helpdesk.py        client for the helpdesk reply API (idempotency key, error classification)
  kb/                KB loader/seed, Ukrainian-friendly tokenizer, full-text search
  llm/               Anthropic client wrapper (+ no-key stub mode), global rate limiter
  web/               FastAPI routers: webhook, /health, operator JSON API, HTML UI
  worker/            background worker: job handlers + concurrent runner
helpdesk_mock/       fake helpdesk: reply API with failures + webhook sender with retries/dupes
kb/                  KB articles (Markdown + front matter), seeded into Postgres
migrations/          Alembic migrations (hand-written)
tests/               pytest suite against a real Postgres
scripts/             repo tooling (blank-line-after-blocks pre-commit hook)
.claude/agents/      subagents: backend-engineer, test-engineer, software-architect
.claude/rules/       code style, typing and lint rules (codestyle.md)
```

## Invariants: do not break these

1. **Webhook < 2 s.** The handler only validates and runs one short INSERT transaction
   under a single deadline (`WEBHOOK_TIMEOUT_SECONDS`, 1.5 s). The deadline covers waiting for
   a pooled connection, connecting, statements, and commit. On timeout it answers 503 so the
   helpdesk retries. Never call the LLM, KB search, or any HTTP from the request path.
2. **Idempotent intake.** Dedup by `event_id` (unique index + `ON CONFLICT DO NOTHING`).
   Concurrent duplicates must create exactly one ticket and one job.
3. **Nothing in memory.** All background work goes through the `jobs` table. Claim with
   `FOR UPDATE SKIP LOCKED` and a lease. Every claim issues a fresh `lease_token`, and
   finishing a job is fenced by it, so a worker whose lease expired cannot overwrite the new
   owner's result. (`attempts` is not a valid fence: re-enqueueing resets it, which is the ABA problem.)
4. **Global LLM rate limit.** Every LLM call (including stub mode) goes through
   `LLMClient`, which acquires the Postgres-backed sliding-window limiter
   (60 req/min across all worker processes).
5. **No-key mode.** Without `ANTHROPIC_API_KEY` the client still sends the real request
   to the provider, ignores the result, and returns the static response after a
   random 5–30 s delay. The rest of the pipeline parses it exactly like a real response.
6. **No duplicate or stale replies.** One `Idempotency-Key` per ticket, created at approval
   and reused for every retry. All status transitions are conditional
   `UPDATE ... WHERE status = <expected>`. Each generated draft bumps `draft_version`. The UI
   approves only the version the operator saw; a stale version gets "reload the page".
7. **Untrusted input.** Customer text stays delimited in the prompt. Links in drafts come
   only from KB rows (the model returns article numbers, never URLs).

## Commands

- Run everything: `docker compose up --build` (UI: http://localhost:8000, mock helpdesk: http://localhost:8001)
- Tests (in Docker): `docker compose --profile test run --rm tests`
- Tests (local): `TEST_DATABASE_URL=postgresql+asyncpg://user:pass@host:port/support_test pytest`
- Lint & types (host, dev venv active): `pre-commit run --all-files`
  (typos, ruff check --fix, ruff format, blank-line-after-blocks, no-suppressions, ty).
  Single tools: `ruff check .`, `ruff format --check .`, `ty check`.
- Coverage: `pytest --cov=app --cov=helpdesk_mock --cov=scripts --cov-branch --cov-report=term-missing`
- New migration: `alembic revision -m "..."`, then hand-write `upgrade`/`downgrade`.

## Conventions

- Code style, typing and lint rules: `.claude/rules/codestyle.md` (79 columns, `import typing as t`,
  type-only imports under `t.TYPE_CHECKING`, no `# noqa`/`# type: ignore`). Async all the way
  down (no blocking I/O in the event loop).
- Ticket statuses and job kinds live in `app/models.py`. Update the state diagram in
  `docs/design.md` when you add one.
- User-facing UI text and KB are Ukrainian; code, comments, and logs are English.
- Tests run against real Postgres (no SQLite: we rely on `SKIP LOCKED`, `ON CONFLICT`,
  advisory locks, and tsvector). External HTTP is faked with `httpx.MockTransport`.
- Keep the webhook, queue, and sender logic covered by tests. Concurrency bugs are the main risk.
