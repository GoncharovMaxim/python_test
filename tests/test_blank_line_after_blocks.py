import textwrap

import pytest

from scripts.blank_line_after_blocks import fix_source, main


def _fix(source):
    return fix_source(textwrap.dedent(source))


def test_adds_blank_line_after_block():
    assert _fix(
        """\
        if condition:
            do_something()
        next_statement()
        """
    ) == textwrap.dedent(
        """\
        if condition:
            do_something()

        next_statement()
        """
    )


@pytest.mark.parametrize(
    "source",
    (
        # already separated
        "if a:\n    b()\n\nc()\n",
        # elif / else / except / finally belong to the block
        "if a:\n    b()\nelif c:\n    d()\nelse:\n    e()\n",
        "try:\n    a()\nexcept ValueError:\n    b()\nfinally:\n    c()\n",
        # the block is the last statement of its body
        "def f():\n    for x in y:\n        g(x)\n",
        # a plain statement followed by a block is not this rule's business
        "a = 1\nif a:\n    b()\n",
    ),
)
def test_leaves_valid_code_alone(source):
    assert fix_source(source) == source


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        (
            "def f():\n    with a:\n        b()\n    return c\n",
            "def f():\n    with a:\n        b()\n\n    return c\n",
        ),
        (
            "try:\n    a()\nexcept E:\n    b()\nc()\n",
            "try:\n    a()\nexcept E:\n    b()\n\nc()\n",
        ),
        (
            "while a:\n    b()\n@dec\ndef f():\n    pass\n",
            "while a:\n    b()\n\n@dec\ndef f():\n    pass\n",
        ),
    ),
)
def test_handles_nesting_handlers_and_decorators(source, expected):
    assert fix_source(source) == expected


def test_comment_inside_the_block_stays_with_the_block():
    source = "if a:\n    b()\n    # still the block\n# about c\nc()\n"
    expected = "if a:\n    b()\n    # still the block\n\n# about c\nc()\n"
    assert fix_source(source) == expected


def test_fix_is_idempotent():
    once = fix_source("for x in y:\n    f(x)\ng()\nif a:\n    b()\nc()\n")
    assert fix_source(once) == once


def test_main_fixes_files_and_reports(tmp_path, capsys):
    bad = tmp_path / "bad.py"
    bad.write_text("if a:\n    b()\nc()\n", encoding="utf-8")
    good = tmp_path / "good.py"
    good.write_text("if a:\n    b()\n\nc()\n", encoding="utf-8")

    assert main(["--check", str(bad), str(good)]) == 1
    assert bad.read_text(encoding="utf-8") == "if a:\n    b()\nc()\n"

    assert main([str(bad), str(good)]) == 1
    assert bad.read_text(encoding="utf-8") == "if a:\n    b()\n\nc()\n"
    assert main([str(bad), str(good)]) == 0
    assert (
        "bad.py:2: missing blank line after block" in capsys.readouterr().out
    )
