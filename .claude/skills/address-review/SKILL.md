---
name: address-review
description: Work through code review feedback on this repository end to end. Verify each remark against the code, reproduce real bugs with a failing test before fixing, fix them, keep the docs in sync, make one commit per remark, and finish with a report. Use whenever the user pastes review comments or a numbered list of remarks, links to lines on GitHub (github.com/.../blob/SHA/path#L42), PR review threads, or asks to "fix the review", "address feedback", "исправь замечания", "поправь по ревью", even when they don't use the word "review".
---

# Address review feedback

A reviewer has pointed at places in this codebase and said what's wrong or missing. Your job is
to turn each remark into a verified fix the reviewer can check commit by commit, without
breaking the invariants in `CLAUDE.md` and without touching anything the user didn't ask about.

Work through all remarks without stopping for approval, then report. Stop and ask only when a
remark doesn't hold up (see step 2) or when two remarks contradict each other.

## 0. Before touching anything

- Run `git status`. The user may have their own uncommitted or untracked files (notes, rules,
  agent configs). They are not yours: never stage them, never commit them, never "clean them
  up". This is why you'll stage files by path later, not with `git add -A`.
- Note the current branch and how far it is ahead of `origin`. Do not push at any point in this
  workflow. Publishing is the user's call, and you'll ask at the end.

## 1. Read every remark against the code it points at

Review links usually pin a commit (`/blob/<sha>/app/queue.py#L94`). The code may have moved
since then. Look at both versions:

```bash
git show <sha>:app/queue.py | sed -n '80,110p'   # what the reviewer saw
```

Then find the same code at `HEAD`. If a remark is already resolved at `HEAD`, say so in the
report instead of changing anything.

For each remark, write down (for yourself) the concrete failure: *which inputs or interleaving of
events lead to which wrong outcome*. "Fencing by attempts is weak" isn't a failure. "Regenerate
resets `attempts` to 0, so a stalled worker's attempt 1 matches the fresh claim's attempt 1, and
the stale draft overwrites the new one" is. If you can't state it this concretely, you don't
understand the remark yet. Keep reading.

## 2. Prove it before you fix it

For a remark about a bug, write a test that reproduces it **before** changing production code,
run it, and watch it fail *for the reason the reviewer described*. A test that fails for some other
reason proves nothing.

- It fails as expected: good. Mention "the test failed before the fix" in the commit message.
  This is the strongest evidence a reviewer can get.
- It can't be reproduced: don't "fix" it anyway. Explain in the report what you tried and why it
  doesn't fail, and ask the user. Reviewers are sometimes wrong, and a speculative change
  in concurrency or queue code can introduce the bug it claims to prevent.
- The remark is a feature or UX request (pagination, a setting, config wiring): there is
  nothing to fail. Write tests that describe the new behaviour instead.

Tests in this repo run against real Postgres (see `CLAUDE.md` → Commands). Concurrency remarks
need a test that forces the interleaving: claim with `lease_seconds=0` to make a lease expire,
hold a pool connection or an uncommitted row lock to make the DB slow, and use `asyncio.gather`
for simultaneous requests. A test that triggers a warning or error must assert it with
`assert_logged(caplog, level, fragment)`; the autouse fixture `no_unasserted_logs` fails it otherwise.

## 3. Fix the problem, not just the sentence

Make the smallest change that removes the failure you stated in step 1. Keep every
invariant in `CLAUDE.md` and follow `.claude/rules/codestyle.md`. Any production function you
touch ends up fully typed.

The reviewer's suggested fix is a strong hint, not a spec. If the underlying problem is wider than
the suggestion (e.g. "add `env_file` to the worker", but the API needs the same settings), fix the
real scope **and call out the difference explicitly in the report**. That way the reviewer isn't
surprised. If it's narrower, do the narrower thing and explain why.

Schema changes get a new hand-written Alembic revision per remark. Backfill existing rows so
running deployments stay consistent (e.g. existing drafts become `draft_version = 1`). Check
`alembic upgrade head`, then `downgrade` one step and `upgrade` again, on a database that has data.

## 4. Keep the docs true

Behaviour changes ripple into documents agents and reviewers rely on. Update whichever apply in
the same commit as the code:

- `CLAUDE.md`: the invariants list. If a remark exposed a wrong invariant, fix the wording.
- `docs/design.md`: the decisions table, state machine, queue section, data model.
- `README.md`: endpoints, configuration table, requirements, test count.
- `.env.example`: new settings.

## 5. One self-contained commit per remark

The reviewer will read the history remark by remark, so each commit must stand on its own:

- Tests pass **at every commit**, not only at the end. Don't let remark 1's test assert a field
  that remark 3 introduces. Add that assertion in remark 3's commit instead.
- If a new behaviour makes an existing test wrong, update that test in the same commit.
- Before each commit, run `pre-commit run --files <changed files>` (typos, ruff, format,
  blank-line-after-blocks, no-suppressions, ty) with the dev virtualenv active, then the full test
  suite. Run the suite twice if you touched timing- or concurrency-sensitive code, to catch flakiness.
- Stage only what you changed: `git add path/one path/two`.
- Keep LF line endings (`.gitattributes` enforces `eol=lf`). If you write files from a script on
  Windows, pass `newline="\n"`.

Commit message: an imperative subject naming the component and the change, then a body with
**what was wrong** (the concrete failure from step 1), **what changed**, and **how it's tested**,
and end with the attribution trailer for the model you are running as:

```
Queue: fence job completion with a per-claim lease token

Completion was fenced by 'attempts', but re-enqueueing (regenerate) resets
attempts to 0. A worker stalled past its lease could match a fresh claim
with the same attempt number and write its stale draft (ABA).

Every claim now sets lease_token = gen_random_uuid(); done/retry/failed
require the token of that claim. Migration 0002 adds the column. The ABA
test failed before the fix.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

## 6. Report

Write the report in the language the user wrote to you. One block per remark, in the reviewer's
order:

```
N. <short name of the remark> (<commit sha>)
   Problem: what was actually broken. Say whether a test reproduced it before the fix,
            or why this is a feature with no failure to reproduce.
   Change:  what changed, and where you deviated from the reviewer's suggestion, with the reason.
   Tests:   which scenarios are now covered.
```

After the remarks:

- **Not verified:** anything you couldn't run here (e.g. Docker or `docker compose`, a
  real LLM key) and how the user can check it.
- **Not fixed:** remarks that didn't reproduce or that you disagree with, with your reasoning.
- The total test count and lint status.
- How many commits are ahead of `origin`, and a question whether to push. Don't push until
  the user says so.
