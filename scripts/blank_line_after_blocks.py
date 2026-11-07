"""Pre-commit hook: put a blank line after a compound block.

A block (if/for/while/with/try/match) that is followed by another statement
at the same indentation level must be separated from it by a blank line:

    if condition:
        do_something()

    next_statement()

`elif`/`else`/`except`/`finally` belong to the block, so they are not
affected. Files are fixed in place; the exit code is 1 when anything changed
(the pre-commit convention for fixers). `--check` only reports.
"""

from __future__ import annotations

import argparse
import ast
import itertools
import sys
import typing as t
from pathlib import Path

if t.TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

BLOCKS = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.TryStar,
    ast.Match,
)
BODY_FIELDS = ("body", "orelse", "finalbody")


def _statement_lists(tree: ast.AST) -> Iterator[list[ast.stmt]]:
    for node in ast.walk(tree):
        for field in BODY_FIELDS:
            value = getattr(node, field, None)
            if isinstance(value, list) and len(value) > 1:
                yield value


def _first_line(stmt: ast.stmt) -> int:
    decorators = getattr(stmt, "decorator_list", ())
    return min((stmt.lineno, *(d.lineno for d in decorators)))


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def missing_blank_lines(source: str) -> list[int]:
    """1-based line numbers after which a blank line has to be inserted."""
    lines = source.splitlines()
    result: set[int] = set()
    for body in _statement_lists(ast.parse(source)):
        for block, following in itertools.pairwise(body):
            if not isinstance(block, BLOCKS) or block.end_lineno is None:
                continue

            start = _first_line(following)
            between = lines[block.end_lineno : start - 1]
            if any(not line.strip() for line in between):
                continue

            # Comments indented deeper than the next statement still belong
            # to the block: the blank line goes after them.
            insert_after = block.end_lineno
            for offset, line in enumerate(between, start=1):
                if _indent(line) > following.col_offset:
                    insert_after = block.end_lineno + offset
                else:
                    break

            result.add(insert_after)

    return sorted(result)


def fix_source(source: str) -> str:
    lines = source.splitlines(keepends=True)
    for line_number in reversed(missing_blank_lines(source)):
        lines.insert(line_number, "\n")

    return "".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument(
        "--check", action="store_true", help="report only, do not fix"
    )
    args = parser.parse_args(argv)

    status = 0
    for path in args.files:
        source = path.read_text(encoding="utf-8")
        missing = missing_blank_lines(source)
        if not missing:
            continue

        status = 1
        for line_number in missing:
            sys.stdout.write(
                f"{path}:{line_number}: missing blank line after block\n"
            )

        if not args.check:
            path.write_text(fix_source(source), encoding="utf-8", newline="")

    return status


if __name__ == "__main__":
    sys.exit(main())
