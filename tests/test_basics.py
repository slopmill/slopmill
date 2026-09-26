# SPDX-License-Identifier: MIT
"""SPEC-BASICS: research on by default, Background notes, the writer's notes per block,
and the design's box titles (the server half; tests/ui/ui_basics.py drives the screen).
No real model, search engine or web page is ever reached."""
import json

import pytest

from conftest import FIXTURE_PACK, FRONT
from slopmill import agent, doc, research
from slopmill.pack import EnvironmentProblem, load_pack
from test_chat_pictures import (H, PAGE, FakeSearch, Script, fake_fetch, make,  # noqa: F401
                                wait_job)

PLAIN = FRONT + """{#a}
This week I tried a new model.

::: {.prompt #p}
Talk about Meta Muse Spark for everyday people.
:::

::: {.prompt #q research=off}
Wrap up the top section.
:::

::: {.prompt #pic}
Image: a lighthouse at dusk
:::

{#z}
Thank you readers.
"""


def plan_for(*ids):
    return "".join(f"=== SEARCH {i} ===\nmuse spark {i}\n=== END ===\n" for i in ids)


def write_all(ids):
    return lambda s, p, f: "".join(f"=== BLOCK {i} ===\nWords for {i}.\n=== END ===\n" for i in ids) \
        + "=== NOTE ===\n#p: I found the launch date on Meta's page.\nA general line.\n#q: Nothing to add.\n=== END ==="


# ── A. research on by default ────────────────────────────────────────────────────

def test_a1_every_text_prompt_is_researched_unless_switched_off(make, monkeypatch):
    monkeypatch.setattr(research, "fetch_public", fake_fetch({"https://example.org/sizes": PAGE}))
    llm = Script(lambda s, p, f: plan_for("p"), write_all(["p", "q"]))
    c, _ = make(llm=llm, searcher=FakeSearch(), text=PLAIN.replace(
        "::: {.prompt #pic}\nImage: a lighthouse at dusk\n:::\n\n", ""))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait_job(c)
    plan_call, write_call = llm.calls
    assert "[p] Talk about Meta Muse Spark" in plan_call["prompt"]      # no "research" word needed
    assert "[q]" not in plan_call["prompt"]                            # switched off
    assert "RESEARCHED: #p" in write_call["prompt"]
    assert "SOURCES.md" in write_call["attached"]


def test_a1_picture_prompts_are_not_researched():
    pic = doc.Block("prompt", "x", "Image: a lighthouse at dusk", {})
    assert research.draft_wants_research(pic) is False
    chart_pic = doc.Block("prompt", "x", "Image: a chart of model sizes", {})
    assert research.draft_wants_research(chart_pic) is True
    off = doc.Block("prompt", "x", "Research the numbers", {"research": "off"})
    assert research.draft_wants_research(off) is False


def test_a1_the_switch_is_saved_and_does_not_make_the_draft_stale():
    text = PLAIN.replace("{#z}", "::: {.draft #d for=q prompt=" + doc.prompt_hash("Wrap up the top section.")
                         + "}\nDone.\n:::\n\n{#z}")
    meta, blocks = doc.parse(text)
    q = next(b for b in blocks if b.id == "q")
    assert q.attrs == {"research": "off"}
    out = doc.serialize(meta, blocks) if hasattr(doc, "serialize") else doc.write(meta, blocks)
    assert "::: {.prompt #q research=off}" in out
    p = next(b for b in blocks if b.id == "p")
    assert "research" not in p.attrs
    # the draft's hash is of the prompt's words only
    d = next(b for b in blocks if b.id == "d")
    assert d.attrs["prompt"] == doc.prompt_hash(q.text)


def test_a1_saving_from_the_editor_keeps_the_switch(make):
    c, d = make(text=PLAIN)
    st = c.get("/api/issues/099-test").json()
    blocks = st["blocks"]
    for b in blocks:
        if b["id"] == "p":
            b["attrs"] = {"research": "off"}
        if b["id"] == "q":
            b["attrs"] = {}
    r = c.put("/api/issues/099-test/doc", json={"base": st["rev"], "meta": st["meta"], "blocks": blocks}, headers=H)
    assert r.status_code == 200, r.text
    text = (d / "issue.md").read_text()
    assert "::: {.prompt #p research=off}" in text and "::: {.prompt #q}" in text


def test_a2_the_planner_may_say_none(make, monkeypatch):
    llm = Script(lambda s, p, f: "=== SEARCH p ===\nNONE\n=== END ===", write_all(["p"]))
    search = FakeSearch()
    c, _ = make(llm=llm, searcher=search, text=PLAIN.replace("research=off", ""))
    c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    wait_job(c)
    assert len(llm.calls) == 2 and search.queries == []       # planned, nothing searched
    assert "SOURCES.md" not in llm.calls[-1]["attached"]
    assert c.get("/api/issues/099-test").json()["review"]["notes"]["p"]   # still written


def test_a3_limits():
    assert research.MAX_ITEMS == 8 and research.MAX_SEARCHES == 12 and research.MAX_PAGES == 12


def test_a3_searches_and_pages_are_shared_by_every_item():
    s = FakeSearch([])
    plan = {f"i{k}": [f"{k}a", f"{k}b", f"{k}c"] for k in range(8)}
    research.gather(plan, {k: "x" for k in plan}, s, 50_000, fetch=fake_fetch({}))
    # in turns: every item's first search, then second searches while the pass has any left
    assert sorted(s.queries) == sorted([f"{k}a" for k in range(8)] + [f"{k}b" for k in range(4)])
    s = FakeSearch([])
    plan = {f"i{k}": [f"{k}a", f"{k}b", f"{k}c"] for k in range(3)}
    research.gather(plan, {k: "x" for k in plan}, s, 50_000, fetch=fake_fetch({}))
    assert len(s.queries) == 9                                   # few items: all their searches

    class Many(FakeSearch):          # six fresh addresses for every search
        def search(self, query, limit=6):
            self.queries.append(query)
            return [{"title": "t", "url": f"https://{query}-{i}.example/", "snippet": "s"} for i in range(6)]
    pages = {f"https://{k}a-{i}.example/": "<p>words</p>" for k in range(8) for i in range(6)}
    sources, _ = research.gather({f"i{k}": [f"{k}a"] for k in range(8)}, {f"i{k}": "x" for k in range(8)},
                                 Many(), 50_000, fetch=fake_fetch(pages))
    per = {}
    for src in sources:
        for i in src["for"]:
            per[i] = per.get(i, 0) + 1
    assert set(per) == {f"i{k}" for k in range(8)}              # the eighth prompt got a page too


JINA_PAGE = """Title: Meta Muse Spark at DuckDuckGo

URL Source: https://lite.duckduckgo.com/lite/?q=Meta+Muse+Spark

Markdown Content:
1.[Introducing **Muse****Spark**](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fai.meta.com%2Fblog%2Fintroducing%2Dmuse%2Dspark%2Dmsl%2F&rut=abc)
Today, we're excited to introduce **Muse****Spark**, the first in the family.
ai.meta.com/blog/introducing-muse-spark-msl/2026-04-08T00:00:00.0000000

2.[An advert](https://duckduckgo.com/y.js?ad_domain=x)
Buy things.
x.example

3.[Muse Spark - Wikipedia](https://en.wikipedia.org/wiki/Muse_Spark)
It was introduced in April 2026.
en.wikipedia.org/wiki/Muse_Spark
"""


def test_a4_jina_results_parse_like_duckduckgo():
    got = research.parse_jina_lite(JINA_PAGE)
    assert [g["url"] for g in got] == ["https://ai.meta.com/blog/introducing-muse-spark-msl/",
                                        "https://en.wikipedia.org/wiki/Muse_Spark"]
    assert got[0]["title"] == "Introducing Muse Spark"
    assert got[0]["snippet"].startswith("Today, we're excited to introduce Muse Spark, the first")
    assert "ai.meta.com/blog" not in got[0]["snippet"]            # the address line is dropped


class Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


def test_a4_a_refusal_goes_to_jina_and_stays_there():
    posts, gets = [], []
    s = research.DuckDuckGo(post=lambda *a, **k: posts.append(1) or Resp(202),
                            get=lambda url, **k: gets.append(url) or Resp(200, JINA_PAGE),
                            sleep=lambda t: None)
    assert s.search("meta muse spark")[0]["url"].startswith("https://ai.meta.com/")
    assert len(posts) == 2 and "r.jina.ai/https://lite.duckduckgo.com/lite/?q=meta+muse+spark" in gets[0]
    s.search("again")                    # DuckDuckGo refused once: straight to Jina this pass
    assert len(posts) == 2 and len(gets) == 2


def test_a4_jina_can_be_turned_off_and_403_counts_as_refused():
    s = research.DuckDuckGo(jina=False, post=lambda *a, **k: Resp(202), get=None, sleep=lambda t: None)
    with pytest.raises(research.ResearchError, match="202"):
        s.search("x")
    gets = []
    s = research.DuckDuckGo(post=lambda *a, **k: Resp(403),
                            get=lambda url, **k: gets.append(url) or Resp(200, JINA_PAGE),
                            sleep=lambda t: None)
    assert s.search("x") and gets
    s = research.DuckDuckGo(post=lambda *a, **k: Resp(500), get=lambda *a, **k: Resp(200, JINA_PAGE),
                            sleep=lambda t: None)
    with pytest.raises(research.ResearchError, match="500"):
        s.search("x")                   # a real failure is not papered over
    s = research.DuckDuckGo(post=lambda *a, **k: Resp(202), get=lambda *a, **k: Resp(429),
                            sleep=lambda t: None)
    with pytest.raises(research.ResearchError, match="Jina's reader answered 429"):
        s.search("x")


ROBOT = "<html><body><div class='anomaly-modal'>Unfortunately, bots use DuckDuckGo too.</div></body></html>"
EMPTY = "<html><body><div class='no-results'>No results.</div></body></html>"


def test_a4_a_robot_check_with_200_is_a_refusal_too():
    gets = []
    s = research.DuckDuckGo(post=lambda *a, **k: Resp(200, ROBOT),
                            get=lambda url, **k: gets.append(url) or Resp(200, JINA_PAGE), sleep=lambda t: None)
    assert s.search("x")[0]["url"].startswith("https://ai.meta.com/") and gets
    gets = []
    s = research.DuckDuckGo(post=lambda *a, **k: Resp(200, EMPTY),
                            get=lambda url, **k: gets.append(url) or Resp(200, JINA_PAGE), sleep=lambda t: None)
    assert s.search("x") == [] and not gets                   # a real "no results" is believed


def test_a4_config():
    assert research.from_config({"research": {"provider": "duckduckgo", "jina": False}}).jina is False
    assert research.from_config({}).jina is True
    with pytest.raises(ValueError, match="jina"):
        research.from_config({"research": {"jina": "yes"}})


def test_a5_revise_and_chat_keep_the_word_rule():
    assert research.wants_research("make it shorter") is False
    assert research.wants_research("look up the date") is True


# ── B. Background ────────────────────────────────────────────────────────────────

NOTES = "Muse Spark launched April 8, 2026: https://ai.meta.com/blog/introducing-muse-spark-msl/"


def test_b6_saved_beside_the_issue_and_never_in_it(make):
    c, d = make(text=PLAIN)
    r = c.put("/api/issues/099-test/background", json={"text": NOTES}, headers=H)
    assert r.status_code == 200 and r.json()["next_request"]["bytes"]
    assert (d / "background.md").read_text() == NOTES
    assert NOTES not in (d / "issue.md").read_text()
    st = c.get("/api/issues/099-test").json()
    assert st["background"] == NOTES and st["background_max"] == agent.BACKGROUND_MAX == 40 * 1024
    preview = c.get("/api/issues/099-test/preview").text
    assert "April 8" not in preview


def test_b7_every_writing_call_gets_it(make, monkeypatch):
    monkeypatch.setattr(research, "fetch_public", fake_fetch({"https://example.org/sizes": PAGE}))
    llm = Script(lambda s, p, f: plan_for("p"), write_all(["p"]))
    c, _ = make(llm=llm, searcher=FakeSearch(), text=PLAIN)
    c.put("/api/issues/099-test/background", json={"text": NOTES}, headers=H)
    c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    wait_job(c)
    for call in llm.calls:                        # planning the research, then writing
        assert "[BACKGROUND: the author's notes for this issue, not part of it]" in call["attached"]["DOCUMENT.md"]
        assert NOTES in call["attached"]["DOCUMENT.md"]
    assert "Background:" in llm.calls[-1]["system"]


def test_b7_counts_toward_the_request_size(make):
    c, _ = make(text=PLAIN)
    before = c.get("/api/issues/099-test").json()["next_request"]["bytes"]
    after = c.put("/api/issues/099-test/background", json={"text": "x" * 20_000},
                  headers=H).json()["next_request"]["bytes"]
    assert after - before >= 20_000


def test_b7_a_background_that_does_not_fit_is_refused_like_voice_files(make):
    c, _ = make(llm=Script(max_request_bytes=30_000), text=PLAIN)
    c.put("/api/issues/099-test/background", json={"text": "x" * 35_000}, headers=H)
    r = c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    assert r.status_code == 422 and "the next request would be" in r.json()["error"]


def test_b8_cap_and_type(make):
    c, d = make(text=PLAIN)
    r = c.put("/api/issues/099-test/background", json={"text": "x" * (agent.BACKGROUND_MAX + 1)}, headers=H)
    assert r.status_code == 422 and "at most" in r.json()["error"]
    assert not (d / "background.md").exists()
    r = c.put("/api/issues/099-test/background", json={"text": 5}, headers=H)
    assert r.status_code == 422


def test_b8_refused_while_the_model_works(make):
    llm = Script(write_all(["p"]), delay=1.0)
    c, d = make(llm=llm, text=PLAIN)
    c.post("/api/issues/099-test/generate", json={"targets": ["q"]}, headers=H)
    r = c.put("/api/issues/099-test/background", json={"text": NOTES}, headers=H)
    assert r.status_code == 409
    wait_job(c)
    assert not (d / "background.md").exists()


def test_b7_chat_questions_get_it(make):
    llm = Script(lambda s, p, f: "An answer.")
    c, _ = make(llm=llm, text=PLAIN)
    c.put("/api/issues/099-test/background", json={"text": NOTES}, headers=H)
    c.post("/api/issues/099-test/ask", json={"text": "What do you think?"}, headers=H)
    from test_chat_pictures import wait_answer
    wait_answer(c)
    assert NOTES in llm.calls[0]["attached"]["DOCUMENT.md"]


# ── C. the writer's note, per block ──────────────────────────────────────────────

def test_c9_split_note():
    note = "#p: found it.\n- #q: nothing.\nb-x: not asked.\nGeneral line.\n#p: and more."
    assert agent.split_note(note, {"p", "q"}) == {"p": "found it. and more.", "q": "nothing."}


def test_c9_notes_are_stored_per_prompt_and_replaced(make):
    llm = Script(write_all(["p", "q"]),
                 lambda s, p, f: "=== BLOCK q ===\nAgain.\n=== END ===\n=== NOTE ===\nAll good.\n=== END ===")
    c, _ = make(llm=llm, text=PLAIN)
    c.post("/api/issues/099-test/generate", json={"targets": ["p", "q"]}, headers=H)
    wait_job(c)
    st = c.get("/api/issues/099-test").json()
    assert st["review"]["notes"] == {"p": "I found the launch date on Meta's page.", "q": "Nothing to add."}
    assert any("A general line." in m["text"] for m in st["review"]["chat"])      # chat keeps it all
    c.post("/api/issues/099-test/generate", json={"targets": ["q"]}, headers=H)
    wait_job(c)
    notes = c.get("/api/issues/099-test").json()["review"]["notes"]
    assert notes == {"p": "I found the launch date on Meta's page."}          # q lost its old note


def test_c9_a_prompt_that_failed_keeps_its_note(make):
    llm = Script(write_all(["p", "q"]),
                 lambda s, p, f: "=== BLOCK q ===\nAgain.\n=== END ===\n=== NOTE ===\nOnly q.\n=== END ===")
    c, _ = make(llm=llm, text=PLAIN)
    c.post("/api/issues/099-test/generate", json={"targets": ["p", "q"]}, headers=H)
    wait_job(c)
    c.post("/api/issues/099-test/generate", json={"targets": ["p", "q"]}, headers=H)   # p gets no block
    wait_job(c)
    notes = c.get("/api/issues/099-test").json()["review"]["notes"]
    assert notes.get("p") == "I found the launch date on Meta's page." and "q" not in notes


def test_c9_the_writer_is_asked_for_id_lines(make):
    llm = Script(write_all(["p"]))
    c, _ = make(llm=llm, text=PLAIN)
    c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    wait_job(c)
    assert "starts its own line with that block's id" in llm.calls[0]["system"]


# ── G–H. the design's boxes ──────────────────────────────────────────────────────

def test_g15_boxes_are_listed_with_titles(make):
    c, _ = make(text=PLAIN)
    boxes = c.get("/api/issues/099-test").json()["boxes"]
    names = [b["name"] for b in boxes]
    assert "figure" not in names and {"previously", "concept"} <= set(names)
    prev = next(b for b in boxes if b["name"] == "previously")
    assert prev["label"] is True and prev["title"]


def test_g16_shipped_designs_have_titles():
    for name in ("starter", "storybook"):
        p = load_pack(name)
        assert all(c.title for c in p.components.values() if c.shape == "container")


def test_g16_a_title_falls_back_to_the_name():
    from slopmill.pack import Component
    c = Component("pull-quote", "container", "t.html", {}, {}, 1, 1)
    assert c.label == "Pull quote"


def test_g16_the_design_checker_accepts_a_title_and_refuses_a_bad_one(tmp_path):
    from slopmill.pack import design_from_text, export_design
    text = export_design(load_pack("starter"))
    assert 'title = "Note"' in text
    design_from_text(text, "x.design.toml")          # round-trips
    bad = text.replace('title = "Note"', "title = 5")
    with pytest.raises(EnvironmentProblem, match="title must be text"):
        design_from_text(bad, "x.design.toml")


# ── review round 2 fixes ─────────────────────────────────────────────────────────

def test_c9_a_revision_note_goes_on_the_prompt(make):
    text = PLAIN.replace("{#z}", "::: {.draft #d for=p prompt=" + doc.prompt_hash(
        "Talk about Meta Muse Spark for everyday people.") + "}\nOld words.\n:::\n\n{#z}")
    llm = Script(lambda s, p, f: "=== BLOCK d ===\nNew words.\n=== END ===\n=== NOTE ===\n#d: I cut the second sentence.\n=== END ===")
    c, d = make(llm=llm, text=text)
    d.joinpath("review.json").write_text(json.dumps({"comments": [], "proposals": [], "chat": [],
                                                      "notes": {"p": "an old note from the first draft"}}))
    c.post("/api/issues/099-test/comments", json={"block": "d", "note": "shorter"}, headers=H)
    c.post("/api/issues/099-test/revise", json={}, headers=H)
    wait_job(c)
    notes = c.get("/api/issues/099-test").json()["review"]["notes"]
    assert notes == {"p": "I cut the second sentence."}


def test_b7_the_rule_says_unsure_notes_are_leads():
    assert "is a lead, not a fact" in agent.BACKGROUND_RULES


def test_a1_the_picture_test_is_slopmills_own():
    b = doc.Block("prompt", "x", "Photo: a harbour", {})
    assert research.draft_wants_research(b, picture=True) is False
    b = doc.Block("prompt", "x", "Display this image of how these top models compare", {})
    assert research.draft_wants_research(b, picture=agent.is_picture_prompt(b.text)) is True


# ── screen review round 2 fixes ──────────────────────────────────────────────────

def test_a_box_still_holding_its_placeholder_is_a_problem(make, tmp_path):
    from slopmill import render
    text = PLAIN.replace("{#z}", '{#bx}\n::: {.concept label="Concept"}\nWrite here.\n:::\n\n{#z}')
    result = render.compile_text(text, load_pack(FIXTURE_PACK), mode="preview")
    assert any("still says" in str(p) for p in result.problems), result.problems
    c, _ = make(text=text)
    probs = c.get("/api/issues/099-test").json()["problems"]
    assert any("still says" in m for m in probs.get("bx", [])), probs


def test_the_too_big_message_names_the_background(make):
    c, _ = make(llm=Script(max_request_bytes=30_000), text=PLAIN)
    c.put("/api/issues/099-test/background", json={"text": "x" * 35_000}, headers=H)
    r = c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    assert "shorten the Background notes" in r.json()["error"]


# ── K. rewrites with directions ──────────────────────────────────────────────────

REDO = PLAIN.replace("{#z}", "::: {.draft #dp for=p prompt=" + doc.prompt_hash(
    "Talk about Meta Muse Spark for everyday people.") + "}\nOld words about Muse.\n:::\n\n::: {.draft #dq for=q prompt="
    + doc.prompt_hash("Wrap up the top section.") + "}\nOld wrap up.\n:::\n\n{#z}")


def test_k20_directions_reach_the_writer_with_their_prompts(make, monkeypatch):
    monkeypatch.setattr(research, "fetch_public", fake_fetch({"https://example.org/sizes": PAGE}))
    llm = Script(lambda s, p, f: plan_for("p"), write_all(["p", "q"]))
    c, _ = make(llm=llm, searcher=FakeSearch(), text=REDO)
    r = c.post("/api/issues/099-test/generate", json={"targets": ["p", "q"], "notes": {
        "p": "Add the launch date.", "q": "", "zz": "not in this pass"}}, headers=H)
    assert r.status_code == 200, r.text
    wait_job(c)
    plan_call, write_call = llm.calls
    assert "The author's direction for this rewrite: Add the launch date." in plan_call["prompt"]
    ask = write_call["prompt"]
    assert "start from that draft, change what the direction asks" in ask
    assert "#p: Add the launch date." in ask and "#q:" not in ask.split("directions for this rewrite")[1]
    assert "not in this pass" not in ask
    assert "Old words about Muse." in write_call["attached"]["DOCUMENT.md"]        # the draft it starts from


def test_k20_no_directions_is_as_before(make):
    llm = Script(write_all(["p"]))
    c, _ = make(llm=llm, text=REDO)
    c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    wait_job(c)
    assert "directions for this rewrite" not in llm.calls[-1]["prompt"]


def test_k20_directions_are_checked(make):
    c, _ = make(text=REDO)
    r = c.post("/api/issues/099-test/generate", json={"targets": ["p"], "notes": ["p"]}, headers=H)
    assert r.status_code == 422
    r = c.post("/api/issues/099-test/generate", json={"targets": ["p"], "notes": {"p": "x" * 2001}}, headers=H)
    assert r.status_code == 422 and "2,000" in r.json()["error"]


def test_k20_directions_count_toward_the_size(make):
    c, _ = make(text=REDO)
    base = c.get("/api/issues/099-test").json()["next_request"]["bytes"]
    c2, _ = make(llm=Script(write_all(["p"]), max_request_bytes=base + 3000), text=REDO)
    ok = c2.post("/api/issues/099-test/generate", json={"targets": ["p"], "notes": {"p": "short"}}, headers=H)
    assert ok.status_code == 200, ok.text                 # fits without long directions
    wait_job(c2)
    big = c2.post("/api/issues/099-test/generate", json={"targets": ["p", "q"], "notes": {
        "p": "y" * 2000, "q": "z" * 2000}}, headers=H)
    assert big.status_code == 422 and "the next request would be" in big.json()["error"]


# ── research takes pages from every search, not the first alone ─────────────────

def test_a3_a_second_thing_to_link_gets_its_own_pages():
    class ByQuery(FakeSearch):
        def search(self, query, limit=6):
            self.queries.append(query)
            key = "laya" if "Laya" in query else "jev"
            return [{"title": f"{key} {i}", "url": f"https://{key}-{i}.example/", "snippet": "s"} for i in range(6)]
    pages = {f"https://{k}-{i}.example/": "<p>words</p>" for k in ("jev", "laya") for i in range(6)}
    s = ByQuery()
    sources, _ = research.gather({"p": ["JEV official site", "Laya open-weight model"]}, {"p": "link JEV and Laya"},
                                 s, 50_000, fetch=fake_fetch(pages))
    assert s.queries == ["JEV official site", "Laya open-weight model"]
    urls = [x["url"] for x in sources]
    assert any("laya" in u for u in urls) and any("jev" in u for u in urls), urls
    assert urls[:2] == ["https://jev-0.example/", "https://laya-0.example/"]      # each search's top result first


def test_a_links_go_to_a_page_about_the_thing_named():
    assert "Never point one thing's name at another thing's page." in research.USE_SOURCES
    assert "prefer the maker's own site" in research.USE_SOURCES
    assert "a page for each thing it" in research.PLAN_SYSTEM


# ── L. title suggestions ─────────────────────────────────────────────────────────

MESSY = ('1. Birds, Again\n- "The Porch Light Question"\n**Small Birds, Big Opinions**\nBirds, again\n'
         '=== NOTE ===\n' + "x" * 130 + '\n6) Last One Here\nSeven\nEight')


def test_l21_titles_come_from_the_page_in_the_voice(make):
    llm = Script(lambda s, p, f: MESSY)
    c, _ = make(llm=llm, text=PLAIN)
    c.put("/api/issues/099-test/background", json={"text": NOTES}, headers=H)
    r = c.post("/api/issues/099-test/titles", json={}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["titles"] == ["Birds, Again", "The Porch Light Question", "Small Birds, Big Opinions",
                                  "Last One Here", "Seven", "Eight"]
    call = llm.calls[0]
    assert "Suggest titles for it" in call["system"] and "This week I tried a new model." in call["attached"]["DOCUMENT.md"]
    assert NOTES in call["attached"]["DOCUMENT.md"]
    assert len([f for f in call["files"] if not f.endswith("DOCUMENT.md")]) >= 1        # the voice files


def test_l22_nothing_else_changes(make):
    c, d = make(llm=Script(lambda s, p, f: "One\nTwo"), text=PLAIN)
    before = (d / "issue.md").read_text()
    c.post("/api/issues/099-test/titles", json={}, headers=H)
    assert (d / "issue.md").read_text() == before


def test_l23_refusals(make):
    c, _ = make(text=FRONT + "::: {.prompt #p}\nOnly a prompt.\n:::\n")
    r = c.post("/api/issues/099-test/titles", json={}, headers=H)
    assert r.status_code == 422 and "nothing on the page" in r.json()["error"]
    c, _ = make(llm=Script(write_all(["p"]), delay=1.5), text=PLAIN)
    c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    r = c.post("/api/issues/099-test/titles", json={}, headers=H)
    assert r.status_code == 409 and "working on this issue" in r.json()["error"]
    wait_job(c)


def test_l23_a_model_failure_is_a_message(make):
    from slopmill import agent as ag
    c, _ = make(llm=Script(ag.LLMError("the model is down")), text=PLAIN)
    r = c.post("/api/issues/099-test/titles", json={}, headers=H)
    assert r.status_code == 502 and "no titles" in r.json()["error"]


def test_l_review_fixes(make):
    c, _ = make(llm=Script(lambda s, p, f: "+ Moonlit Lake\nTwo"), text=FRONT + "::: {.prompt #p}\nOnly a prompt.\n:::\n")
    c.put("/api/issues/099-test/background", json={"text": "Notes about a lake at night."}, headers=H)
    r = c.post("/api/issues/099-test/titles", json={}, headers=H)
    assert r.status_code == 200 and r.json()["titles"] == ["Moonlit Lake", "Two"]



def test_l_round2_fixes(make):
    c, _ = make(llm=Script(lambda s, p, f: 'Here are six titles:\n"1. A Working Title"\n“2) Second One”\nThird\nFourth\nFifth\nSixth'),
                text=PLAIN)
    got = c.post("/api/issues/099-test/titles", json={}, headers=H).json()["titles"]
    assert got == ["A Working Title", "Second One", "Third", "Fourth", "Fifth", "Sixth"]
