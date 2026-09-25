# SPDX-License-Identifier: MIT
"""Acceptance check 10: loud failures, with the source line where Pandoc gives one."""
import pytest

from conftest import FRONT
from slopmill.errors import CompileError

FIG = "![A chart](chart.jpg)"

CASES = {
    "no ID": ("Just a paragraph.\n", "1 block has no ID"),
    "duplicate ID": ("{#a}\nOne.\n\n{#a}\nTwo.\n", "duplicate block ID #a"),
    "ID attached to text": ("{#a}\nOne.\n{#b}\n## Heading\n", "attached to text instead of a block"),
    "ID line at the end": ("{#a}\nOne.\n\n{#b}\n", "#b was written but is attached to nothing"),
    "two ID lines in a row": ("{#a}\n{#b}\nOne.\n", "#b was written but is attached to nothing"),
    "ID line above a div with its own ID": ("{#a}\n::: {.closing #b}\nx\n:::\n", "#b was written but is attached to nothing"),
    "unknown component": ("::: {.sidebar #a}\nx\n:::\n", "unknown component .sidebar"),
    "two component classes": ("::: {.figure .closing #a}\nx\n:::\n", "one component class per block"),
    "unknown component attribute": ('::: {.closing tone="dark" #a}\nx\n:::\n', "does not take tone="),
    "container paragraph count": ("::: {.closing #a}\nOne.\n\nTwo.\n:::\n", "holds exactly 1 paragraph"),
    "image outside a figure": ("{#a}\n" + FIG + "\n", "image outside a figure"),
    "figure without alt": ("::: {.figure #a}\n![](chart.jpg)\n:::\n", "has no alt text"),
    "figure with two images": ("::: {.figure #a}\n" + FIG + " " + FIG + "\n:::\n", "exactly one image"),
    "video without poster": ("::: {.figure #a}\n![A clip](clip.mp4)\n:::\n", "needs a poster"),
    "unknown image attribute": ("::: {.figure #a}\n![A chart](chart.jpg){width=50}\n:::\n", "does not take width="),
    "poster on an image": ("::: {.figure #a}\n![A chart](chart.jpg){poster=x.jpg}\n:::\n", "does not take poster="),
    "asset path with a directory": ("::: {.figure #a}\n![A chart](../chart.jpg)\n:::\n", "must be a bare filename"),
    "raw HTML block": ("<div>hello</div>\n", "raw HTML is not allowed"),
    "raw HTML inline": ("{#a}\nSome <b>bold</b> text.\n", "raw HTML is not allowed"),
    "heading level without template": ("{#a}\n### Small heading\n", "no template for a level-3 heading"),
    "nested list": ("{#a}\n- one\n  - nested\n", "nested lists"),
    "quote with two paragraphs": ("{#a}\n> One.\n>\n> Two.\n", "exactly one paragraph"),
    "unknown accent": ("{#a}\nSome **text**{.purple}.\n", "unknown accent .purple"),
    "footnote-ish / unsupported inline": ("{#a}\nSome `code` here.\n", "not styled by this pack"),
    "relative link": ("{#a}\nSee [this](/newsletter).\n", "not an absolute http(s)"),
    "javascript link": ("{#a}\nSee [this](javascript:alert(1)).\n", "not an absolute http(s)"),
    "code block": ("```\ncode\n```\n", "CodeBlock blocks are not supported"),
    "horizontal rule": ("---\n", "HorizontalRule blocks are not supported"),
}


@pytest.mark.parametrize("name", CASES)
def test_10_loud_failure(name, compile_md):
    body, message = CASES[name]
    with pytest.raises(CompileError) as e:
        compile_md(body)
    assert message in e.value.render(), e.value.render()


def test_10_line_numbers_point_at_the_block(compile_md):
    front_lines = FRONT.count("\n")
    body = "{#a}\nFine.\n\n{#b}\nBad **x**{.purple}.\n"
    with pytest.raises(CompileError) as e:
        compile_md(body)
    assert f"line {front_lines + 5}:" in e.value.render()


def test_10_meta_unknown_and_missing(compile_md):
    front = FRONT.replace("subject: Test subject\n", "").replace("title: Test\n", "title: Test\ntitel: typo\n")
    with pytest.raises(CompileError) as e:
        compile_md("{#a}\nx\n", front=front)
    msg = e.value.render()
    assert "unknown front matter key 'titel'" in msg and "missing subject" in msg


def test_10_missing_asset(compile_md, tmp_path):
    body = "::: {.figure #a}\n![A chart](chart.jpg){light=chart-light.jpg}\n:::\n"
    with pytest.raises(CompileError) as e:
        compile_md(body, check_assets=True)
    assert "chart.jpg is referenced but not in" in e.value.render()
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "chart.jpg").write_bytes(b"x")
    (tmp_path / "images" / "chart-light.jpg").write_bytes(b"x")
    res = compile_md(body, check_assets=True)
    assert res.assets == ["chart.jpg", "chart-light.jpg"]


def test_10_every_problem_reported_at_once(compile_md):
    body = "{#a}\nBad **x**{.purple}.\n\n::: {.sidebar #b}\nx\n:::\n\nNo id.\n"
    with pytest.raises(CompileError) as e:
        compile_md(body)
    assert len(e.value.problems) == 3


def test_dollar_amounts_stay_text(compile_md):
    res = compile_md("{#a}\nIt costs $99 a month and $7 an hour, or $5/$10.\n")
    assert "$99 a month and $7 an hour, or $5/$10." in res.body


def test_prose_that_looks_like_markup_stays_text(compile_md):
    res = compile_md("{#a}\nB. B. King said ~30x more :smile: than 2^10.\n\n{#b}\nTerm\n: not a definition\n")
    assert "B. B. King said ~30x more :smile: than 2^10." in res.body
    assert ": not a definition" in res.body


def test_comments_dropped(compile_md):
    res = compile_md("<!-- a note to self -->\n\n{#a}\nText <!-- inline note --> here.\n")
    assert "note" not in res.body.split("-->", 1)[1]


def test_meta_types_are_explicit(compile_md):
    front = FRONT.replace("number: 99", "number: 023").replace(
        "issue_date: JANUARY 1, 2027", "issue_date: 2027-01-01")
    res = compile_md("{#a}\nx\n", front=front)
    assert res.meta["number"] == 23
    assert res.meta["issue_date"] == "2027-01-01"
    with pytest.raises(CompileError, match="must be a whole number"):
        compile_md("{#a}\nx\n", front=FRONT.replace("number: 99", "number: twelve"))


def test_html_between_two_comments_is_not_a_comment(compile_md):
    with pytest.raises(CompileError, match="raw HTML is not allowed"):
        compile_md("<!-- a --> <p>smuggled</p> <!-- b -->\n\n{#a}\nx\n")


def test_comments_inside_components_are_dropped(compile_md):
    res = compile_md("::: {.closing #a}\nThe quote.\n\n<!-- where it came from -->\n:::\n\n"
                     "::: {.figure #b}\n![A chart](c.jpg)\n\n<!-- note -->\n\nCaption.\n:::\n")
    assert "where it came from" not in res.body and "Caption." in res.body


def test_raw_html_in_alt_text_is_an_error(compile_md):
    with pytest.raises(CompileError, match="raw HTML is not allowed in alt text"):
        compile_md("::: {.figure #a}\n![A <b>bold</b> chart](c.jpg)\n:::\n")


def test_duplicate_id_line_is_not_lost_silently(compile_md):
    with pytest.raises(CompileError, match="#a was written 2 times"):
        compile_md("{#a}\n{#a}\nOne.\n")


def test_id_inside_a_comment_is_not_an_id(compile_md):
    res = compile_md("<!-- retired: {#old-block} -->\n\n{#current}\nText.\n")
    assert [b.id for b in res.blocks] == ["current"]


def test_unknown_accent_in_alt_text_is_an_error(compile_md):
    with pytest.raises(CompileError, match="unknown accent .purple"):
        compile_md("::: {.figure #a}\n![A **warning**{.purple}](chart.jpg)\n:::\n")
