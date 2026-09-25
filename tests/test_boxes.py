# SPDX-License-Identifier: MIT
"""A box the model writes on one line (::: {.concept label="X"} text :::).

2026-09-23: the live writer answered "Insert the definition of slopmill from the
dictionary" with exactly the one-line form the component line in its brief shows. Pandoc
reads that as a paragraph, the check refused it ("text takes no attributes, got label;
unknown accent .concept"), and the author got nothing for the prompt."""
import os
import re

import pytest

from conftest import HERE
from slopmill import agent, render
from slopmill.pack import load_pack
from test_v2 import DOC, H, Fake, blocks_of, make, wait  # noqa: F401  (make is a fixture)

# The reply the live writer sent, word for word.
LIVE_REPLY_BLOCK = '::: {.concept label="slopmill"} A person who arranges type for printing. :::'
ROOT_PACKS = os.path.join(os.path.dirname(HERE), "packs")
TEST_PACKS = os.path.join(HERE, "packs")
# Every design shipped in the repo or the tests, found on disk so a new one is covered.
PACKS = [load_pack(os.path.join(d, n)) for d in (ROOT_PACKS, TEST_PACKS) for n in sorted(os.listdir(d))
         if os.path.isfile(os.path.join(d, n, "pack.toml"))]
TAUGHT_RE = re.compile(r"(:{3,}\s*\{[^{}]*\}.*?:{3,})\s*$")


def drawn(md, pack):
    """The block as the design draws it once the author keeps it."""
    return render.compile_text(md + "\n", pack, mode="preview")


def taught_examples():
    for pack in PACKS:
        for name, comp in pack.components.items():
            if comp.describe:
                m = TAUGHT_RE.search(comp.describe)
                assert m, f"{pack.name}.{name}: describe line has no ::: example"
                yield pytest.param(pack, name, m.group(1), id=f"{pack.name}-{name}")


def test_the_packs_found_include_both_known_designs():
    assert {"fixture", "minimal"} <= {p.name for p in PACKS}


def test_the_live_reply_fails_the_check_as_it_stands():
    # Guards the fixture: if this passes, the tests below prove nothing.
    assert agent.check_markdown(LIVE_REPLY_BLOCK, next(p for p in PACKS if p.name == "fixture"))


@pytest.mark.parametrize("pack,name,example", list(taught_examples()))
def test_every_box_the_model_is_taught_is_accepted_as_taught(pack, name, example):
    md = agent.clean_markdown(example)
    assert agent.check_markdown(md, pack) == []
    res = drawn(md, pack)
    assert [b.kind for b in res.blocks] == [name]   # problems: check_markdown above


def test_the_live_reply_becomes_a_concept_box(make):
    doc_text = DOC.replace("Talk about the voice file revisions. About 60 words.",
                           "Insert the definition of slopmill from the dictionary.")
    c, d = make(llm=Fake(text={"p": LIVE_REPLY_BLOCK}), text=doc_text)
    c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    job = wait(c)
    draft = next(b for b in blocks_of(d) if b.type == "draft" and b.attrs["for"] == "p")
    assert draft.text == ('::: {.concept label="slopmill"}\n'
                          "A person who arranges type for printing.\n:::")
    assert "p" not in (job.get("failures") or {})
    html = c.get("/api/issues/099-test/preview").json()["html"]
    assert "Concept &middot; slopmill" in html and "A person who arranges type" in html


def test_a_revise_reply_on_one_line_is_unfolded_too(make):
    llm = Fake()
    c, d = make(llm=llm)
    c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    wait(c)
    draft = next(b for b in blocks_of(d) if b.type == "draft" and b.attrs["for"] == "p")
    llm.text = {draft.id: LIVE_REPLY_BLOCK}
    c.post("/api/issues/099-test/comments", json={"block": draft.id, "note": "make it a definition"},
           headers=H)
    c.post("/api/issues/099-test/revise", json={}, headers=H)
    assert wait(c)["status"] == "done"
    new = next(b for b in blocks_of(d) if b.id == draft.id)
    assert new.text.split("\n") == ['::: {.concept label="slopmill"}',
                                   "A person who arranges type for printing.", ":::"]


@pytest.mark.parametrize("md", [
    # already on its own lines
    '::: {.concept label="T"}\nOne.\n:::',
    # colons in the middle of a sentence
    "The ratio was 3::: {not a box} honestly :::",
    # inside a code fence, backticks and tildes, longer fences
    '```\n::: {.concept label="T"} shown as an example :::\n```',
    '~~~~ markdown\n::: {.concept label="T"} shown :::\n~~~~',
    '````\n```\n::: {.concept label="T"} still code :::\n````',
    # no attributes, or no text
    "::: concept A person who arranges type. :::",
    '::: {.concept label="T"} :::',
    # a fence opened inside a list item or a quote (review round 1, F1)
    '- ```\n  ::: {.concept label="T"} shown as code :::\n  ```',
    '> ~~~\n> ::: {.concept label="T"} quoted code :::\n> ~~~',
    '1. ```md\n   ::: {.concept label="T"} numbered :::\n   ```',
    # an indented code block: four spaces or a tab (round 1, B2)
    '    ::: {.concept label="T"} indented code :::',
    '\t::: {.concept label="T"} tab-indented code :::',
    # "- ```" inside a top-level fence is code, not a close (round 2, F1)
    '```\n- ```\n::: {.concept label="T"} shown as an example :::\n```',
    # raw HTML blocks
    '<!--\n::: {.concept label="T"} a comment :::\n-->',
    '<pre>\n::: {.concept label="T"} preformatted :::\n</pre>',
    # the middle line of an inline code span that runs over three lines (round 3, B1)
    'Here is `code\n::: {.concept label="T"} example :::\nend` done.',
])
def test_lines_that_are_not_a_one_line_box_are_left_alone(md):
    assert agent.unfold_boxes(md) == md


@pytest.mark.parametrize("before", [
    "```\ncode\n```",
    "- ```\n  code\n  ```",                       # fence in a list item (round 1, F1)
    "> ~~~\n> code\n> ~~~",
    "```inline``` code, not a fence",                # backticks closed on the same line
    "Some prose.",
    "    ```",                                       # an indented code line, not a fence (round 2, B1)
    "> ```\n> code\n",                               # a quoted fence ends with its quote (round 2, B2)
    "- ```\n  code\n",                               # ...and a list item's with its item
])
def test_a_box_after_code_or_prose_is_unfolded(before):
    got = agent.unfold_boxes(before + '\n::: {.concept label="T"} Def. :::').split("\n")
    assert got[-3:] == ['::: {.concept label="T"}', "Def.", ":::"]
    assert "\n".join(got[:-3]) == before


def test_a_box_holding_inline_code_is_unfolded():
    got = agent.unfold_boxes('::: {.concept label="T"} Use `slopmill build` here. :::').split("\n")
    assert got == ['::: {.concept label="T"}', "Use `slopmill build` here.", ":::"]


def test_a_box_in_the_middle_of_a_paragraph_is_unfolded():
    got = agent.unfold_boxes('Before.\n::: {.concept label="T"} Def. :::\nAfter.').split("\n")
    assert got == ["Before.", '::: {.concept label="T"}', "Def.", ":::", "After."]


def test_pandoc_is_not_called_when_no_line_looks_like_a_box(monkeypatch):
    monkeypatch.setattr(agent.pandoc, "to_ast", lambda *a, **k: 1 / 0)
    assert agent.unfold_boxes("Just prose.\n\n::: {.concept}\nDef.\n:::") \
        == "Just prose.\n\n::: {.concept}\nDef.\n:::"


def test_a_box_indented_under_a_list_item_keeps_its_indent():
    got = agent.unfold_boxes('- item\n\n  ::: {.concept label="T"} Def. :::')
    assert got.split("\n")[-3:] == ['  ::: {.concept label="T"}', "  Def.", "  :::"]


def test_the_brief_says_to_write_boxes_on_their_own_lines():
    assert "::: alone on the last line" in agent.vocabulary(next(p for p in PACKS if p.name == "fixture"))


def test_a_design_that_offers_no_box_gets_no_box_rule():
    minimal = next(p for p in PACKS if p.name == "minimal")
    assert not any(c.describe for c in minimal.components.values())
    assert ":::" not in agent.vocabulary(minimal)


def test_a_described_component_without_box_syntax_gets_no_box_rule():
    # round 1, B1: a design may describe a component in some other notation
    from types import SimpleNamespace as NS
    real = next(p for p in PACKS if p.name == "fixture")
    pack = NS(accents=real.accents, components={
        "concept": NS(describe="a plain definition of one term, as its own short paragraph")})
    assert "one term" in agent.vocabulary(pack) and ":::" not in agent.vocabulary(pack)
