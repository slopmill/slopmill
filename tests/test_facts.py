# SPDX-License-Identifier: MIT
"""SPEC-FACTS: Check the facts. The server half; tests/ui/ui_basics.py drives the screen.
No real model, search engine or web page is ever reached."""
import json

import pytest

from conftest import FRONT
from slopmill import doc, facts, research
from slopmill.net import Fetched
from test_chat_pictures import H, FakeSearch, Script, fake_fetch, make, wait_job  # noqa: F401

ISSUE = FRONT + """{#a}
There's also an open weight version of it call Laya that can run on about 1 gb of ram locally.

{#b}
I went looking at [JEV](https://jevmodel.org/) again, and [Laya](https://jevmodel.org/) too.

::: {.prompt #p}
Say more about the models.
:::

{#z}
Thank you readers.
"""

CLAIMS = """=== CLAIM c1 #a ===
QUOTE: an open weight version of it call Laya
CHECK: Laya is made by TypeSafe as an open version of JEV
SEARCH: Laya open weight model maker
=== END ===
=== CLAIM c2 #a ===
QUOTE: can run on about 1 gb of ram locally
CHECK: Laya runs in about 1 GB of RAM
SEARCH: Laya RAM requirement
=== END ===
=== CLAIM c3 #z ===
QUOTE: words that are not in the block at all
CHECK: nothing
=== END ===
=== CLAIM c4 #p ===
QUOTE: Say more about the models
CHECK: a prompt is not text
=== END ===
"""

VERDICTS = """=== VERDICT c1 ===
RESULT: wrong
SOURCE: S1
EVIDENCE: Laya is ConvAI Innovations' open model.
NOTE: Laya is ConvAI Innovations' own model, an open alternative to JEV.
FIND: an open weight version of it call Laya
REPLACE: an open-weight alternative to it called Laya
=== END ===
=== VERDICT c2 ===
RESULT: confirmed
NOTE: no source named
=== END ===
=== LINK L1 ===
RESULT: wrong page
NOTE: a look-alike site, not TypeSafe's.
=== END ===
=== LINK L2 ===
RESULT: wrong page
NOTE: this page is about JEV, not Laya.
=== END ===
"""

LAYA = [{"title": "Laya — ConvAI Innovations", "url": "https://laya.example/", "snippet": "Laya by ConvAI"}]
PAGES = {"https://laya.example/": "<title>Laya</title><p>Laya is ConvAI Innovations' open model.</p>",
         "https://jevmodel.org/": "<title>Jev AI Model</title><p>Jev makes fast structured decisions.</p>"}


def test_2_claims_must_quote_their_block():
    _, blocks = doc.parse(ISSUE)
    kept, dropped = facts.parse_claims(CLAIMS, blocks)
    assert [c["id"] for c in kept] == ["c1", "c2"]
    assert {c for c, _ in dropped} == {"c3", "c4"}           # words not there; a prompt is not text
    many = "".join(f"=== CLAIM c{i} #a ===\nQUOTE: can run on about\n=== END ===\n" for i in range(1, 25))
    assert len(facts.parse_claims(many, blocks)[0]) == facts.MAX_CLAIMS


def test_3_every_link_is_listed_once_per_block():
    _, blocks = doc.parse(ISSUE)
    links = facts.links_of(blocks)
    assert [(ln["words"], ln["url"]) for ln in links] == [("JEV", "https://jevmodel.org/"), ("Laya", "https://jevmodel.org/")]


def test_4_to_6_the_check(tmp_path):
    _, blocks = doc.parse(ISSUE)
    llm = Script(lambda s, p, f: CLAIMS, lambda s, p, f: VERDICTS)
    rec = facts.check({}, blocks, llm, searcher=FakeSearch(LAYA), workdir=str(tmp_path),
                      fetch=fake_fetch(PAGES))
    first, second = llm.calls
    assert "Every block is checked the same way" in first["system"]
    assert set(second["attached"]) == {"DOCUMENT.md", "CLAIMS.md", "SOURCES.md", "LINKS.md"}
    assert "Laya is ConvAI Innovations' open model." in second["attached"]["SOURCES.md"]
    assert "Jev makes fast structured decisions." in second["attached"]["LINKS.md"]   # the link was opened
    by = {i["quote"]: i for i in rec["items"]}
    wrong = by["an open weight version of it call Laya"]
    assert wrong["result"] == "wrong" and wrong["source"]["url"] == "https://laya.example/"
    assert wrong["fix"]["before"] in "an open weight version of it call Laya"   # the words it will switch
    assert wrong["fix"]["after"] != wrong["fix"]["before"]
    assert by["can run on about 1 gb of ram locally"]["result"] == "unclear"      # no source named
    assert rec["items"][0]["result"] == "wrong"                                  # wrong ones first
    assert [ln["result"] for ln in rec["links"]] == ["wrong page", "wrong page"]
    assert rec["seen"] == facts.fingerprint(blocks)


def test_4_a_broken_link_is_broken_whatever_the_writer_says(tmp_path):
    _, blocks = doc.parse(ISSUE)
    llm = Script(lambda s, p, f: CLAIMS, lambda s, p, f: VERDICTS.replace("wrong page", "fits"))
    rec = facts.check({}, blocks, llm, searcher=FakeSearch(LAYA), workdir=str(tmp_path),
                      fetch=fake_fetch({"https://laya.example/": PAGES["https://laya.example/"]}))
    assert [ln["result"] for ln in rec["links"]] == ["broken", "broken"]         # jevmodel.org did not open


def test_9_research_off(tmp_path):
    _, blocks = doc.parse(ISSUE)
    llm = Script(lambda s, p, f: CLAIMS)
    rec = facts.check({}, blocks, llm, searcher=None, workdir=str(tmp_path))
    assert len(llm.calls) == 1                                                   # no verdict call
    assert {i["result"] for i in rec["items"]} == {"unclear"}
    assert all("research is off" in i["note"] for i in rec["items"])
    assert {ln["result"] for ln in rec["links"]} == {"unchecked"}


def test_2_a_cut_off_reply_is_an_error(tmp_path):
    _, blocks = doc.parse(ISSUE)
    with pytest.raises(facts.FactError, match="cut off"):
        facts.check({}, blocks, Script(lambda s, p, f: "=== CLAIM c1"), searcher=None, workdir=str(tmp_path))


def test_5_a_correction_that_cannot_be_placed_is_offered_by_hand(tmp_path):
    _, blocks = doc.parse(ISSUE)
    bad = VERDICTS.replace("FIND: an open weight version of it call Laya", "FIND: not in the text")
    rec = facts.check({}, blocks, Script(lambda s, p, f: CLAIMS, lambda s, p, f: bad),
                      searcher=FakeSearch(LAYA), workdir=str(tmp_path), fetch=fake_fetch(PAGES))
    wrong = [i for i in rec["items"] if i["result"] == "wrong"][0]
    assert wrong["fix"] is None


# ── the server ───────────────────────────────────────────────────────────────────

def run_check(make, monkeypatch):
    monkeypatch.setattr(research, "fetch_public", fake_fetch(PAGES))
    monkeypatch.setattr(facts, "fetch_public", fake_fetch(PAGES))
    c, d = make(llm=Script(lambda s, p, f: CLAIMS, lambda s, p, f: VERDICTS), searcher=FakeSearch(LAYA), text=ISSUE)
    assert c.get("/api/issues/099-test").json()["facts_current"] is False        # never checked
    r = c.post("/api/issues/099-test/facts", json={}, headers=H)
    assert r.status_code == 200, r.text
    wait_job(c)
    return c, d


def test_1_6_the_pass_keeps_results_and_changes_no_text(make, monkeypatch):
    before = ISSUE
    c, d = run_check(make, monkeypatch)
    st = c.get("/api/issues/099-test").json()
    assert (d / "issue.md").read_text() == before
    assert st["facts_current"] is True
    assert st["review"]["facts"]["items"][0]["result"] == "wrong"


def test_7_use_the_fix_and_undo(make, monkeypatch):
    c, d = run_check(make, monkeypatch)
    item = c.get("/api/issues/099-test").json()["review"]["facts"]["items"][0]
    r = c.post(f"/api/issues/099-test/facts/{item['id']}", json={"applied": True}, headers=H)
    assert r.status_code == 200, r.text
    text = (d / "issue.md").read_text()
    assert "an open-weight alternative to it called Laya" in text and "call Laya" not in text
    st = c.get("/api/issues/099-test").json()
    assert st["facts_current"] is True                   # its own fix does not make the check stale
    assert any("fact fix" in json.dumps(h) for h in st["history"])            # a snapshot first
    r = c.post(f"/api/issues/099-test/facts/{item['id']}", json={"applied": False}, headers=H)
    assert "an open weight version of it call Laya" in (d / "issue.md").read_text()


def test_7_a_changed_sentence_refuses_the_fix(make, monkeypatch):
    c, d = run_check(make, monkeypatch)
    st = c.get("/api/issues/099-test").json()
    item = st["review"]["facts"]["items"][0]
    blocks = st["blocks"]
    for b in blocks:
        if b["id"] == "a":
            b["text"] = "Laya is something else entirely now."
    c.put("/api/issues/099-test/doc", json={"base": st["rev"], "meta": st["meta"], "blocks": blocks}, headers=H)
    r = c.post(f"/api/issues/099-test/facts/{item['id']}", json={"applied": True}, headers=H)
    assert r.status_code == 409 and "changed" in r.json()["error"]


def test_8_editing_makes_the_results_out_of_date(make, monkeypatch):
    c, d = run_check(make, monkeypatch)
    st = c.get("/api/issues/099-test").json()
    blocks = st["blocks"]
    blocks[-1]["text"] += " More."
    r = c.put("/api/issues/099-test/doc", json={"base": st["rev"], "meta": st["meta"], "blocks": blocks}, headers=H)
    assert r.json()["facts_current"] is False


def test_1_one_pass_at_a_time_and_clear(make, monkeypatch):
    monkeypatch.setattr(facts, "fetch_public", fake_fetch(PAGES))
    c, _ = make(llm=Script(lambda s, p, f: CLAIMS, delay=1.0), text=ISSUE)
    assert c.post("/api/issues/099-test/facts", json={}, headers=H).status_code == 200
    assert c.post("/api/issues/099-test/facts", json={}, headers=H).status_code == 409
    wait_job(c)
    assert c.delete("/api/issues/099-test/facts", headers=H).json()["ok"]
    assert "facts" not in c.get("/api/issues/099-test").json()["review"]



# ── review round 1 fixes ─────────────────────────────────────────────────────────

def test_r1_an_address_with_brackets_is_kept_whole():
    _, blocks = doc.parse(FRONT + "{#a}\nSee [the article](https://example.org/wiki/AI_(model)) today.\n")
    assert facts.links_of(blocks)[0]["url"] == "https://example.org/wiki/AI_(model)"


def test_r1_links_past_fifteen_are_unchecked_not_broken(tmp_path):
    body = "".join(f"{{#p{i}}}\nSee [page {i}](https://ok.example/{i}).\n\n" for i in range(17))
    _, blocks = doc.parse(FRONT + body)
    pages = {f"https://ok.example/{i}": "<p>fine</p>" for i in range(17)}
    llm = Script(lambda s, p, f: "=== NONE ===", lambda s, p, f: "".join(
        f"=== LINK L{i} ===\nRESULT: fits\nNOTE: ok\n=== END ===\n" for i in range(1, 18)))
    rec = facts.check({}, blocks, llm, searcher=FakeSearch([]), workdir=str(tmp_path), fetch=fake_fetch(pages))
    assert [ln["result"] for ln in rec["links"]][-2:] == ["unchecked", "unchecked"]
    assert set(ln["result"] for ln in rec["links"][:15]) == {"fits"}


def test_r1_linked_pages_are_cut_to_fit(tmp_path):
    body = "".join(f"{{#p{i}}}\nSee [page {i}](https://ok.example/{i}).\n\n" for i in range(12))
    _, blocks = doc.parse(FRONT + body)
    pages = {f"https://ok.example/{i}": "<p>" + "word " * 2000 + "</p>" for i in range(12)}
    logs = []
    llm = Script(lambda s, p, f: "=== NONE ===", lambda s, p, f: "=== NONE ===", max_request_bytes=12_000)
    facts.check({}, blocks, llm, searcher=FakeSearch([]), workdir=str(tmp_path), fetch=fake_fetch(pages),
                log=lambda level, text: logs.append(text))
    assert len(llm.calls) == 2                                   # the verdict call went out: it fit
    assert len(llm.calls[1]["attached"]["LINKS.md"].encode()) <= 12_000 * facts.LINKS_SHARE + 2000


def test_r1_a_changed_paragraph_refuses_the_fix(make, monkeypatch):
    c, d = run_check(make, monkeypatch)
    st = c.get("/api/issues/099-test").json()
    item = st["review"]["facts"]["items"][0]
    blocks = st["blocks"]
    for b in blocks:
        if b["id"] == "a":
            b["text"] = "Allegedly, there's also an open weight version of it call Laya that can run on about 1 gb of ram locally."
    c.put("/api/issues/099-test/doc", json={"base": st["rev"], "meta": st["meta"], "blocks": blocks}, headers=H)
    r = c.post(f"/api/issues/099-test/facts/{item['id']}", json={"applied": True}, headers=H)
    assert r.status_code == 409 and "changed since the check" in r.json()["error"]



# ── found in the first real run (issue 23) ───────────────────────────────────────

def test_real_a_source_named_with_words_counts(tmp_path):
    _, blocks = doc.parse(ISSUE)
    v = VERDICTS.replace("RESULT: confirmed\nNOTE: no source named", "RESULT: confirmed\nSOURCE: S1 (ConvAI's site)\nEVIDENCE: “Laya is ConvAI Innovations' open model”\nNOTE: yes")
    rec = facts.check({}, blocks, Script(lambda s, p, f: CLAIMS, lambda s, p, f: v), searcher=FakeSearch(LAYA),
                      workdir=str(tmp_path), fetch=fake_fetch(PAGES))
    ram = [i for i in rec["items"] if i["quote"].startswith("can run on")][0]
    assert ram["result"] == "confirmed" and ram["source"]["url"] == "https://laya.example/"


def test_real_a_site_that_refuses_robots_is_not_broken(tmp_path):
    _, blocks = doc.parse(ISSUE)

    def fetch(url, **kw):
        if "jevmodel" in url:
            return Fetched(403, b"", "text/html", url)
        return fake_fetch(PAGES)(url, **kw)
    rec = facts.check({}, blocks, Script(lambda s, p, f: CLAIMS, lambda s, p, f: VERDICTS), searcher=FakeSearch(LAYA),
                      workdir=str(tmp_path), fetch=fetch)
    assert {ln["result"] for ln in rec["links"]} == {"unchecked"}
    assert "refused an automated visit (403)" in rec["links"][0]["note"]


def test_real_first_hand_accounts_are_not_claims():
    assert "not the author's first-hand account" in facts.CLAIMS_BRIEF



# ── review round 2 fixes ─────────────────────────────────────────────────────────

TWO = FRONT + """{#a}
Laya is made by TypeSafe and it needs 1 GB of RAM to run anywhere at all.
"""
TWO_CLAIMS = """=== CLAIM c1 #a ===
QUOTE: Laya is made by TypeSafe
SEARCH: laya maker
=== END ===
=== CLAIM c2 #a ===
QUOTE: it needs 1 GB of RAM to run
SEARCH: laya ram
=== END ===
"""
TWO_VERDICTS = """=== VERDICT c1 ===
RESULT: wrong
SOURCE: S1
EVIDENCE: Laya is ConvAI Innovations' open model.
NOTE: ConvAI makes it.
FIND: Laya is made by TypeSafe
REPLACE: Laya is made by ConvAI Innovations
=== END ===
=== VERDICT c2 ===
RESULT: wrong
SOURCE: S1
EVIDENCE: Laya is ConvAI Innovations' open model.
NOTE: not stated.
FIND: it needs 1 GB of RAM to run
REPLACE: it runs on your own machine
=== END ===
"""


def test_r2_two_fixes_in_one_paragraph_both_apply(make, monkeypatch):
    monkeypatch.setattr(research, "fetch_public", fake_fetch(PAGES))
    monkeypatch.setattr(facts, "fetch_public", fake_fetch(PAGES))
    c, d = make(llm=Script(lambda s, p, f: TWO_CLAIMS, lambda s, p, f: TWO_VERDICTS), searcher=FakeSearch(LAYA), text=TWO)
    c.post("/api/issues/099-test/facts", json={}, headers=H)
    wait_job(c)
    items = c.get("/api/issues/099-test").json()["review"]["facts"]["items"]
    for it in items:
        r = c.post(f"/api/issues/099-test/facts/{it['id']}", json={"applied": True}, headers=H)
        assert r.status_code == 200, r.text
    text = (d / "issue.md").read_text()
    assert "Laya is made by ConvAI Innovations" in text and "it runs on your own machine" in text


def test_r2_an_opened_link_can_be_the_source(tmp_path):
    _, blocks = doc.parse(ISSUE)
    v = VERDICTS.replace("RESULT: confirmed\nNOTE: no source named", "RESULT: confirmed\nSOURCE: L1\nEVIDENCE: Jev makes fast structured decisions\nNOTE: the linked page says so")
    rec = facts.check({}, blocks, Script(lambda s, p, f: CLAIMS, lambda s, p, f: v), searcher=FakeSearch([]),
                      workdir=str(tmp_path), fetch=fake_fetch(PAGES))
    ram = [i for i in rec["items"] if i["quote"].startswith("can run on")][0]
    assert ram["result"] == "confirmed" and ram["source"]["url"] == "https://jevmodel.org/"


def test_r2_reference_links_are_found():
    _, blocks = doc.parse(FRONT + "{#a}\nSee [Laya][model] and [JEV][].\n\n{#b}\n[model]: https://jevmodel.org/\n[jev]: https://typesafe.example/\n")
    got = [(ln["words"], ln["url"]) for ln in facts.links_of(blocks)]
    assert ("Laya", "https://jevmodel.org/") in got and ("JEV", "https://typesafe.example/") in got


def test_r2_link_text_is_cut_before_the_search_budget(tmp_path):
    body = "".join(f"{{#p{i}}}\nSee [page {i}](https://ok.example/{i}).\n\n" for i in range(12))
    _, blocks = doc.parse(FRONT + "{#top}\nLaya is made by TypeSafe.\n\n" + body)
    pages = {f"https://ok.example/{i}": "<p>" + "word " * 2000 + "</p>" for i in range(12)}
    pages["https://laya.example/"] = "<p>Laya is ConvAI's.</p>"
    s = FakeSearch(LAYA)
    llm = Script(lambda s_, p, f: "=== CLAIM c1 #top ===\nQUOTE: Laya is made by TypeSafe\nSEARCH: laya maker\n=== END ===",
                 lambda s_, p, f: "=== VERDICT c1 ===\nRESULT: unclear\nNOTE: x\n=== END ===", max_request_bytes=20_000)
    facts.check({}, blocks, llm, searcher=s, workdir=str(tmp_path), fetch=fake_fetch(pages))
    assert s.queries == ["laya maker"]                      # the search still ran



def test_real_a_link_is_read_where_it_talks_about_the_claim():
    page = "<title>Laya</title>" + "".join(f"<p>Menu item {i} for navigation.</p>" for i in range(60)) \
        + "<p>Treat Laya as a fast foundation model to specialize, not a zero-shot oracle; fine-tuning reaches 0.766.</p>"
    links = [{"id": "L1", "block": "a", "words": "Laya", "url": "https://laya.example/"}]
    claims = [{"id": "c1", "block": "a", "quote": "ConvAI says you should expect to tune it", "check": "Laya needs fine-tuning for a task"}]
    facts.open_links(links, lambda *a: None, fetch=fake_fetch({"https://laya.example/": page}), claims=claims)
    assert "fine-tuning reaches 0.766" in links[0]["text"]



# ── review round 3 fixes ─────────────────────────────────────────────────────────

def test_r3_a_confirmation_must_quote_its_source(tmp_path):
    _, blocks = doc.parse(ISSUE)
    made_up = VERDICTS.replace("EVIDENCE: Laya is ConvAI Innovations' open model.", "EVIDENCE: Laya needs one gigabyte of memory")
    rec = facts.check({}, blocks, Script(lambda s, p, f: CLAIMS, lambda s, p, f: made_up), searcher=FakeSearch(LAYA),
                      workdir=str(tmp_path), fetch=fake_fetch(PAGES))
    first = [i for i in rec["items"] if i["quote"].startswith("an open weight")][0]
    assert first["result"] == "unclear" and "not quoted from it" in first["note"] and first["fix"] is None


def test_r3_autolinks_and_titled_references():
    _, blocks = doc.parse(FRONT + "{#a}\nSee <https://example.org/story> and [Laya][m].\n\n{#b}\n[m]: https://laya.example/ \"Laya\"\n")
    got = [(ln["words"], ln["url"]) for ln in facts.links_of(blocks)]
    assert ("https://example.org/story", "https://example.org/story") in got and ("Laya", "https://laya.example/") in got
