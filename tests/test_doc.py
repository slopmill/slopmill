# SPDX-License-Identifier: MIT
"""The editor's view of issue.md: blocks out, the same blocks back in."""
import os

import pytest

from conftest import FIXTURE, FRONT, needs_022
from slopmill import doc, render
from slopmill.doc import Block, DocError

SAMPLE = FRONT + """{#a}
This week has found me on the road.
Second line of the same paragraph.

{#h}
## A heading

::: {.prompt #p}
Talk about the voice file. About 220 words.

Keep the apology short.
:::

::: {.draft #d for=p prompt=abcd1234}
Last week I revised the voice file.

## And a heading inside

- one
- two
:::

<!-- a note to self -->

::: {.concept label="System One" #c}
A model that returns a decision.
:::

{#l}
- Parking Lot
- Street
"""


def test_parse_types_ids_and_text():
    meta, blocks = doc.parse(SAMPLE)
    assert meta["title"] == "Test"
    assert [(b.type, b.id) for b in blocks] == [
        ("prose", "a"), ("prose", "h"), ("prompt", "p"), ("draft", "d"),
        ("comment", None), ("component", "c"), ("prose", "l")]
    assert blocks[0].text == "This week has found me on the road.\nSecond line of the same paragraph."
    assert blocks[1].text == "## A heading"
    assert blocks[2].text == "Talk about the voice file. About 220 words.\n\nKeep the apology short."
    assert blocks[3].attrs == {"for": "p", "prompt": "abcd1234"}
    assert blocks[3].text.startswith("Last week") and blocks[3].text.endswith("- two")
    assert blocks[4].text == "<!-- a note to self -->"
    assert blocks[5].text.startswith("::: {.concept") and blocks[5].attrs == {"class": "concept"}
    assert blocks[6].text == "- Parking Lot\n- Street"


def test_round_trip_is_stable():
    meta, blocks = doc.parse(SAMPLE)
    text = doc.save_text(meta, blocks)
    meta2, blocks2 = doc.parse(text)
    assert [(b.type, b.id, b.text, b.attrs) for b in blocks2] == \
        [(b.type, b.id, b.text, b.attrs) for b in blocks]
    assert doc.serialize(meta2, blocks2) == text


@needs_022
def test_022_round_trips_and_compiles_the_same(pack):
    text = open(os.path.join(FIXTURE, "issue.md"), encoding="utf-8").read()
    meta, blocks = doc.parse(text)
    assert len(blocks) == 70
    again = doc.save_text(meta, blocks, pack.meta_fields)
    a = render.compile_text(text, pack, mode="publish")
    b = render.compile_text(again, pack, mode="publish")
    assert (a.body, a.email, a.meta_json) == (b.body, b.email, b.meta_json)


def test_heading_with_inline_id_reads_without_it():
    meta, blocks = doc.parse(FRONT + "## Title {#h}\n")
    assert blocks[0].text == "## Title" and blocks[0].id == "h"


@pytest.mark.parametrize("bad, why", [
    ("one\n\ntwo", "blank line"),
    ("::: {.closing}\ntwo\n:::", ":::"),
    ("text\n{#zz}", "{#"),
])
def test_save_refuses_text_that_changes_structure(bad, why):
    meta, blocks = doc.parse(SAMPLE)
    blocks[0].text = bad
    with pytest.raises(DocError):
        doc.save_text(meta, blocks)


def test_save_needs_ids():
    with pytest.raises(DocError, match="no ID"):
        doc.save_text({}, [Block("prose", None, "x")])


def test_prompt_hash_ignores_rewrapping():
    assert doc.prompt_hash("Talk about\nthe voice file.") == doc.prompt_hash("Talk about the  voice file.")
    assert doc.prompt_hash("a") != doc.prompt_hash("b")


def test_preview_compile_with_prompts(pack):
    r = render.compile_text(SAMPLE.replace("::: {.draft #d for=p prompt=abcd1234}", "::: {.draft #d for=p}"),
                            pack, mode="preview")
    assert r.prompts == {"p": "d"}
    assert 'data-block="d"' in r.preview and 'data-kind="draft"' in r.preview
    assert "Talk about the voice file" not in r.body
    assert "Last week I revised" in r.body and "And a heading inside" in r.body


def test_unwritten_prompt_blocks_publish_not_preview(pack):
    text = FRONT + "::: {.prompt #p}\nWrite something.\n:::\n\n{#a}\nText.\n"
    r = render.compile_text(text, pack, mode="preview")
    assert r.prompts == {"p": None} and 'cmp-pending' in r.preview
    with pytest.raises(render.CompileError, match="#p has not been written yet"):
        render.compile_text(text, pack, mode="publish")


def test_draft_pairing_problems(pack):
    text = FRONT + "::: {.prompt #p}\nx\n:::\n\n::: {.draft #d1 for=p}\na\n:::\n\n::: {.draft #d2 for=p}\nb\n:::\n\n::: {.draft #d3 for=nope}\nc\n:::\n"
    r = render.compile_text(text, pack, mode="preview")
    msgs = " ".join(str(p) for p in r.problems)
    assert "two drafts" in msgs and "not a prompt here" in msgs


def test_preview_keeps_going_past_a_bad_block(pack):
    r = render.compile_text(FRONT + "{#a}\nGood.\n\n{#b}\nBad **x**{.purple}.\n\n{#c}\nAlso good.\n",
                            pack, mode="preview")
    assert "Good." in r.preview and "Also good." in r.preview
    bad = [b for b in r.blocks if b.id == "b"][0]
    assert bad.problems and 'data-problems="1"' in r.preview
    # the preview marks the block's own elements; it adds no wrapper
    assert '<p data-block="a" data-kind="paragraph" style=' in r.preview
    assert "<div" not in r.preview.split('data-block="b"')[0]


def test_pack_cannot_define_reserved_components(tmp_path):
    import shutil
    from slopmill.errors import EnvironmentProblem
    from slopmill.pack import load_pack
    from conftest import HERE
    d = tmp_path / "p"
    shutil.copytree(os.path.join(HERE, "packs", "minimal"), d)
    (d / "pack.toml").write_text((d / "pack.toml").read_text() + '\n[components.prompt]\nshape = "container"\ntemplate = "p.html"\n')
    with pytest.raises(EnvironmentProblem, match="slopmill's own blocks"):
        load_pack(str(d))
