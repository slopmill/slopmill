# SPDX-License-Identifier: MIT
"""SPEC-HEADINGS: a design that asks for section headings gets the missing ones on the way to
Proof. The server half; tests/ui/ui_basics.py drives the screen. No real model is reached."""
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURE_PACK, FRONT
from slopmill import agent, doc
from slopmill.pack import EnvironmentProblem, design_from_text, export_design, load_pack
from slopmill.server.app import create_app
from test_chat_pictures import H, Script, wait_job

HEADINGS = """
[headings]
add = true
level = 2
after_words = 60
not_before = ["fold", "closing"]
describe = "Short headings, 3 to 9 words, a line the author would say out loud."
"""

WORDS = " ".join(["word"] * 40)

ISSUE = FRONT + f"""{{#a}}
The cold open, a short hello. {WORDS}

{{#b}}
First subject starts here. {WORDS}

::: {{.prompt #p}}
Write about the second subject.
:::

::: {{.draft #d for=p prompt={doc.prompt_hash("Write about the second subject.")}}}
Second subject, as written. {WORDS}
:::

{{#c}}
Until next week,

{{#f}}
::: {{.fold}}
The teaser line.
:::

{{#e}}
Below the fold, a builder's part. {WORDS} {WORDS}

{{#q}}
::: {{.closing}}
A closing line.
:::
"""


def with_headings(tmp_path, extra=HEADINGS):
    dst = tmp_path / "pack-h"
    shutil.copytree(FIXTURE_PACK, dst)
    with open(dst / "pack.toml", "a", encoding="utf-8") as f:
        f.write(extra)
    return str(dst)


@pytest.fixture
def mk(tmp_path):
    def go(llm=None, text=ISSUE, pack=None):
        ws = tmp_path / "ws"
        d = ws / "issues" / "099-test"
        d.mkdir(parents=True, exist_ok=True)
        (d / "issue.md").write_text(text)
        app = create_app(workspace=str(ws), pack=load_pack(pack or with_headings(tmp_path)),
                         llm=llm or Script(), model_label="fake", token="tok")
        c = TestClient(app)
        c.cookies.set("slopmill_token", "tok")
        return c, d
    return go


def reply(*pairs, note="Added what was missing."):
    return lambda s, p, f: "".join(f"=== HEADING before {i} ===\n{t}\n=== END ===\n" for i, t in pairs) \
        + f"=== NOTE ===\n{note}\n=== END ==="


def ids_and_text(d):
    _, blocks = doc.parse((d / "issue.md").read_text())
    return [(b.id, b.type, b.text) for b in blocks]


# ── 1–2, 9: the design setting ───────────────────────────────────────────────────

@pytest.mark.parametrize("bad,why", [
    ('add = "yes"', "headings.add must be true or false"),
    ("add = true\nlevel = 3", "level 3 is not drawn"),
    ("add = true\nlevel = 9", "level must be a whole number 2 to 6"),
    ("add = true\nafter_words = 10", "after_words must be a whole number 50 to 2000"),
    ('add = true\nnot_before = ["nope"]', "names no component"),
    ('add = true\ndescribe = "' + "x" * 1501 + '"', "at most 1,500 characters"),
    ("add = true\ncolour = 1", "does not take colour"),
])
def test_1_bad_settings_are_refused_with_the_reason(tmp_path, bad, why):
    with pytest.raises(EnvironmentProblem, match=why):
        load_pack(with_headings(tmp_path, "\n[headings]\n" + bad + "\n"))


def test_1_an_uploaded_design_is_checked_and_round_trips(tmp_path):
    p = load_pack(with_headings(tmp_path))
    text = export_design(p)
    assert "[headings]" in text
    assert design_from_text(text, "x.design.toml").headings == p.headings
    with pytest.raises(EnvironmentProblem, match="after_words"):
        design_from_text(text.replace("after_words = 60", "after_words = 5"), "x.design.toml")


def test_2_the_shipped_example_designs_do_not_ask():
    assert load_pack("starter").headings is None and load_pack("storybook").headings is None


def test_9_no_setting_no_flag_no_pass(mk):
    c, _ = mk(pack=FIXTURE_PACK)
    assert "headings" not in c.get("/api/issues/099-test").json()
    r = c.post("/api/issues/099-test/headings", json={}, headers=H)
    assert r.status_code == 422 and "does not add section headings" in r.json()["error"]


# ── 3: needed ────────────────────────────────────────────────────────────────────

def test_3_needed_when_a_run_is_too_long(tmp_path):
    p = load_pack(with_headings(tmp_path))
    _, blocks = doc.parse(ISSUE)
    assert agent.headings_needed(p, blocks)
    # a heading ends a run; so does a not_before box
    short = FRONT + "{#a}\nOne short paragraph.\n\n{#h}\n## A heading\n\n{#b}\n" + WORDS + "\n"
    assert not agent.headings_needed(p, doc.parse(short)[1])
    boxed = FRONT + "{#a}\n" + WORDS + "\n\n{#f}\n::: {.fold}\n:::\n\n{#b}\n" + WORDS + "\n"
    assert not agent.headings_needed(p, doc.parse(boxed)[1])
    unboxed = boxed.replace("::: {.fold}\n:::", "::: {.concept}\nA box that does not end a section.\n:::")
    assert agent.headings_needed(p, doc.parse(unboxed)[1])


def test_3_prompts_notes_and_drawn_pictures_count_nothing(tmp_path):
    p = load_pack(with_headings(tmp_path))
    text = FRONT + "{#a}\n" + " ".join(["w"] * 50) + "\n\n::: {.prompt #p}\n" + WORDS + "\n:::\n\n" \
        + "::: {.draft #d for=p prompt=x}\n::: {.figure}\n![" + WORDS + "](x.jpg)\n:::\n:::\n"
    assert not agent.headings_needed(p, doc.parse(text)[1])


def test_3_the_state_says_so(mk):
    c, _ = mk()
    assert c.get("/api/issues/099-test").json()["headings"] == {"on": True, "needed": True}


# ── 5–8: the pass ────────────────────────────────────────────────────────────────

def test_5_to_7_the_pass_adds_good_headings_in_the_right_places(mk):
    llm = Script(reply(("b", "So where does the first bit go"), ("d", "*The second* subject, [really]"),
                       ("e", "What the builders get")))
    c, d = mk(llm=llm)
    c.put("/api/issues/099-test/background", json={"text": "Background fact."}, headers=H)
    r = c.post("/api/issues/099-test/headings", json={}, headers=H)
    assert r.status_code == 200
    wait_job(c)
    call = llm.calls[0]
    assert "Your only job: find the sections that have no heading" in call["system"]
    assert "Short headings, 3 to 9 words" in call["system"]                      # the design's describe
    assert "Background fact." in call["attached"]["DOCUMENT.md"]
    order = [(i, t) for i, kind, t in ids_and_text(d)]
    texts = [t for _, t in order]
    at = {t: k for k, t in enumerate(texts)}
    assert "## So where does the first bit go" in texts
    assert at["## So where does the first bit go"] == [i for i, _ in order].index("b") - 1
    # a draft's heading goes above its prompt
    assert "## The second subject, really" in texts
    assert [i for i, _ in order][at["## The second subject, really"] + 1] == "p"
    assert at["## What the builders get"] == [i for i, _ in order].index("e") - 1
    st = c.get("/api/issues/099-test").json()
    assert st["headings"]["needed"] is False
    assert any("Added what was missing." in m["text"] for m in st["review"]["chat"])


def test_6_bad_headings_are_dropped(mk):
    llm = Script(reply(("a", "The top of the issue"), ("zz", "No such block"), ("q", "On the closing"),
                       ("f", "On the fold"), ("b", "x" * 81), ("b", "Fine one"), ("b", "Same place again"),
                       ("b2", "Straight after a heading")))
    text = ISSUE.replace("{#e}\nBelow", "{#hd}\n## Already here\n\n{#b2}\nMore words.\n\n{#e}\nBelow")
    c, d = mk(llm=llm, text=text)
    c.post("/api/issues/099-test/headings", json={}, headers=H)
    wait_job(c)
    heads = [t for _, kind, t in ids_and_text(d) if t.startswith("## ")]
    assert heads == ["## Fine one", "## Already here"]


def test_6_at_most_ten(mk):
    many = [(f"x{i}", f"Heading {i}") for i in range(12)]
    body = "".join(f"{{#x{i}}}\nParagraph {i}. {WORDS}\n\n" for i in range(12))
    c, d = mk(llm=Script(reply(*many)), text=FRONT + "{#top}\nHello.\n\n" + body)
    c.post("/api/issues/099-test/headings", json={}, headers=H)
    wait_job(c)
    assert len([t for _, _, t in ids_and_text(d) if t.startswith("## ")]) == 10


def test_7_a_deleted_heading_does_not_come_back(mk):
    c, d = mk(llm=Script(reply(("b", "First subject"))))
    c.post("/api/issues/099-test/headings", json={}, headers=H)
    wait_job(c)
    st = c.get("/api/issues/099-test").json()
    blocks = [b for b in st["blocks"] if b["text"] != "## First subject"]
    r = c.put("/api/issues/099-test/doc", json={"base": st["rev"], "meta": st["meta"], "blocks": blocks}, headers=H)
    assert r.json()["headings"]["needed"] is False          # text unchanged: not asked again
    # a small edit does not ask again; a new paragraph does
    for b in blocks:
        if b["id"] == "e":
            b["text"] += " And a new sentence."
    st1 = c.get("/api/issues/099-test").json()
    r = c.put("/api/issues/099-test/doc", json={"base": st1["rev"], "meta": st1["meta"], "blocks": blocks}, headers=H)
    assert r.json()["headings"]["needed"] is False
    blocks.append({"type": "prose", "id": "e2", "text": "A new paragraph about something else.", "attrs": {}})
    st2 = c.get("/api/issues/099-test").json()
    r = c.put("/api/issues/099-test/doc", json={"base": st2["rev"], "meta": st2["meta"], "blocks": blocks}, headers=H)
    assert r.json()["headings"]["needed"] is True


def test_4_nothing_to_add_is_remembered(mk):
    llm = Script(lambda s, p, f: "=== NONE ===\n=== NOTE ===\nAll headed.\n=== END ===")
    c, d = mk(llm=llm)
    before = (d / "issue.md").read_text()
    c.post("/api/issues/099-test/headings", json={}, headers=H)
    wait_job(c)
    assert (d / "issue.md").read_text() == before
    assert c.get("/api/issues/099-test").json()["headings"]["needed"] is False


def test_4_a_failed_pass_is_tried_again(mk):
    c, d = mk(llm=Script(agent.LLMError("the model is down")))
    c.post("/api/issues/099-test/headings", json={}, headers=H)
    job = wait_job(c)
    assert job["status"] == "failed"
    assert c.get("/api/issues/099-test").json()["headings"]["needed"] is True


def test_8_undo_takes_the_headings_out(mk):
    c, d = mk(llm=Script(reply(("b", "First subject"))))
    before = (d / "issue.md").read_text()
    c.post("/api/issues/099-test/headings", json={}, headers=H)
    wait_job(c)
    assert "## First subject" in (d / "issue.md").read_text()
    r = c.post("/api/issues/099-test/undo", json={}, headers=H)
    assert r.status_code == 200, r.text
    assert (d / "issue.md").read_text() == before


def test_8_one_pass_at_a_time(mk):
    c, _ = mk(llm=Script(reply(("b", "First subject")), delay=1.0))
    assert c.post("/api/issues/099-test/headings", json={}, headers=H).status_code == 200
    assert c.post("/api/issues/099-test/headings", json={}, headers=H).status_code == 409
    wait_job(c)


# ── review round 1 fixes ─────────────────────────────────────────────────────────

def test_r1_a_draft_that_opens_with_a_box_still_counts(tmp_path):
    p = load_pack(with_headings(tmp_path))
    text = FRONT + "{#a}\nHello.\n\n::: {.prompt #p}\nx\n:::\n\n::: {.draft #d for=p prompt=x}\n" \
        + "::: {.concept}\nA box.\n:::\n\n" + WORDS + " " + WORDS + "\n:::\n"
    assert agent.headings_needed(p, doc.parse(text)[1])
    only_box = FRONT + "{#a}\nHello.\n\n::: {.prompt #p}\nx\n:::\n\n::: {.draft #d for=p prompt=x}\n" \
        + "::: {.concept}\n" + WORDS + " " + WORDS + "\n:::\n:::\n"
    assert not agent.headings_needed(p, doc.parse(only_box)[1])       # words inside a box count nothing


def test_r1_a_multi_line_answer_is_dropped(mk):
    c, d = mk(llm=Script(reply(("b", "First line\nSecond line"))))
    c.post("/api/issues/099-test/headings", json={}, headers=H)
    wait_job(c)
    assert not [t for _, _, t in ids_and_text(d) if t.startswith("## ")]


def test_r1_the_level_must_be_drawn_even_when_off(tmp_path):
    with pytest.raises(EnvironmentProblem, match="level 3 is not drawn"):
        load_pack(with_headings(tmp_path, "\n[headings]\nadd = false\nlevel = 3\n"))


def test_r1_searches_are_handed_out_in_turns():
    from slopmill import research
    from test_chat_pictures import FakeSearch, fake_fetch
    s = FakeSearch([])
    plan = {"p": ["JEV official site", "Laya open-weight model"], **{f"i{k}": [f"q{k}"] for k in range(7)}}
    logs = []
    research.gather(plan, {k: "x" for k in plan}, s, 50_000, fetch=fake_fetch({}),
                    log=lambda level, text: logs.append(text))
    assert "Laya open-weight model" in s.queries                     # 8 items, and the second thing still searched
    many = {f"i{k}": [f"{k}a", f"{k}b", f"{k}c"] for k in range(8)}
    s2, logs2 = FakeSearch([]), []
    research.gather(many, {k: "x" for k in many}, s2, 50_000, fetch=fake_fetch({}),
                    log=lambda level, text: logs2.append(text))
    assert len(s2.queries) == 12 and any("not searched" in t and "7c" in t for t in logs2)


# ── review round 2 fixes ─────────────────────────────────────────────────────────

def test_r2_a_pass_that_hit_the_cap_is_asked_again(mk):
    many = [(f"x{i}", f"Heading {i}") for i in range(12)]
    body = "".join(f"{{#x{i}}}\nParagraph {i}. {WORDS} {WORDS}\n\n" for i in range(12))
    c, d = mk(llm=Script(reply(*many)), text=FRONT + "{#top}\nHello.\n\n" + body)
    c.post("/api/issues/099-test/headings", json={}, headers=H)
    wait_job(c)
    assert c.get("/api/issues/099-test").json()["headings"]["needed"] is True


def test_r2_searches_run_in_turns_before_time_runs_out():
    from slopmill import research
    from test_chat_pictures import FakeSearch, fake_fetch
    t = [0.0]

    class Slow(FakeSearch):
        def search(self, query, limit=6):
            self.queries.append(query)
            t[0] += 20            # each search takes 20 "seconds"
            return []
    s = Slow()
    plan = {f"i{k}": [f"{k}a", f"{k}b", f"{k}c"] for k in range(4)}
    research.gather(plan, {k: "x" for k in plan}, s, 50_000, fetch=fake_fetch({}), clock=lambda: t[0])
    assert s.queries[:4] == ["0a", "1a", "2a", "3a"]          # every item's first search, before any second


