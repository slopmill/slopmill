# SPDX-License-Identifier: MIT
"""SPEC-STEPS server side: editing one block from Draft or Proof locks it as the author's
words. Driven with the fake model from test_server; no real model is called."""
import time

from test_server import DOC, H, FakeLLM, make, wait  # noqa: F401  (make is a fixture)

S = "/api/issues/099-test"


def state(c):
    return c.get(S).json()


def drafted(c):
    c.post(f"{S}/generate", json={}, headers=H)
    assert wait(c)["status"] == "done"
    st = state(c)
    return st, next(b for b in st["blocks"] if b["type"] == "draft")


def edit(c, bid, orig, text):
    return c.post(f"{S}/blocks/{bid}/edit", json={"orig": orig, "text": text}, headers=H)


def test_7_saving_a_draft_locks_it_and_removes_its_prompt(make):
    c, d = make()
    st, draft = drafted(c)
    r = edit(c, draft["id"], draft["text"], "My own version of it.")
    assert r.status_code == 200, r.text
    after = state(c)
    kinds = [(b["type"], b["id"]) for b in after["blocks"]]
    assert ("prose", draft["id"]) in kinds
    assert not any(t == "prompt" for t, _ in kinds) and not any(t == "draft" for t, _ in kinds)
    assert next(b for b in after["blocks"] if b["id"] == draft["id"])["text"] == "My own version of it."
    assert r.json()["ids"] == [draft["id"]] and r.json()["rev"] == after["rev"]
    # locked: a later generate has nothing to write, and the text is untouched
    c.post(f"{S}/generate", json={}, headers=H)
    wait(c)
    assert "My own version of it." in (d / "issue.md").read_text()


def test_8_saving_prose_keeps_its_type_and_id(make):
    c, d = make()
    r = edit(c, "a", "This week has found me on the road a lot more than usual.", "This week I stayed home.")
    assert r.status_code == 200, r.text
    b = next(b for b in state(c)["blocks"] if b["id"] == "a")
    assert (b["type"], b["text"]) == ("prose", "This week I stayed home.")
    assert "::: {.prompt #p}" in (d / "issue.md").read_text()      # nothing else moved


def test_9_several_paragraphs_become_several_blocks_first_keeps_the_id(make):
    c, _ = make()
    r = edit(c, "a", "This week has found me on the road a lot more than usual.",
             "First paragraph.\n\nSecond paragraph.\n\n## A heading")
    assert r.status_code == 200, r.text
    ids = r.json()["ids"]
    assert len(ids) == 3 and ids[0] == "a" and len(set(ids)) == 3
    blocks = state(c)["blocks"]
    texts = [b["text"] for b in blocks if b["id"] in ids]
    assert texts == ["First paragraph.", "Second paragraph.", "## A heading"]
    assert all(b["type"] == "prose" for b in blocks if b["id"] in ids)


def test_10_block_changed_since_opened_is_refused_and_says_so(make):
    c, d = make()
    before = (d / "issue.md").read_text()
    r = edit(c, "a", "What the box showed an hour ago.", "New text.")
    assert r.status_code == 409 and "changed since you opened it" in r.json()["error"]
    assert r.json()["current"].startswith("This week")
    assert (d / "issue.md").read_text() == before


def test_10_a_change_elsewhere_does_not_refuse_the_edit(make):
    c, d = make()
    st = state(c)
    blocks = [dict(b) for b in st["blocks"]]
    blocks[-1]["text"] = "Thanks, everyone."
    assert c.put(f"{S}/doc", json={"base": st["rev"], "meta": st["meta"], "blocks": blocks}, headers=H).status_code == 200
    r = edit(c, "a", "This week has found me on the road a lot more than usual.", "Edited.")
    assert r.status_code == 200, r.text
    text = (d / "issue.md").read_text()
    assert "Edited." in text and "Thanks, everyone." in text


def test_10_empty_prompt_fence_and_breakage_are_refused(make):
    c, d = make()
    before = (d / "issue.md").read_text()
    orig = "This week has found me on the road a lot more than usual."
    r = edit(c, "a", orig, "   \n ")
    assert r.status_code == 422 and "empty" in r.json()["error"]
    r = edit(c, "a", orig, "Mine.\n\n::: {.prompt}\nWrite more.\n:::")
    assert r.status_code == 422 and "prompt" in r.json()["error"]
    r = edit(c, "a", orig, "Mine.\n\n::: {.draft for=p}\nSneaky.\n:::")
    assert r.status_code == 422
    assert (d / "issue.md").read_text() == before


def test_10_refused_while_the_model_works(make):
    c, d = make(FakeLLM(delay=1.5))
    c.post(f"{S}/generate", json={}, headers=H)
    time.sleep(0.2)
    r = edit(c, "a", "This week has found me on the road a lot more than usual.", "Edited.")
    assert r.status_code == 409 and "working" in r.json()["error"]
    wait(c)
    assert "Edited." not in (d / "issue.md").read_text()


def test_prompts_notes_and_unknown_blocks_are_not_edited_here(make):
    c, _ = make()
    assert edit(c, "p", "Talk about the voice file revisions. About 60 words.", "x").status_code == 422
    assert edit(c, "nope", "x", "y").status_code == 404
    assert c.post("/api/issues/no-such/blocks/a/edit", json={"orig": "", "text": "y"}, headers=H).status_code == 404
    assert c.post(f"{S}/blocks/a/edit", json={"text": "y"}, headers=H).status_code == 422


def test_13_lock_keeps_the_draft_as_it_stands(make):
    c, _ = make()
    st, draft = drafted(c)
    r = c.post(f"{S}/keep", json={"draft": draft["id"], "base": st["rev"]}, headers=H)
    assert r.status_code == 200
    b = next(b for b in state(c)["blocks"] if b["id"] == draft["id"])
    assert b["type"] == "prose" and b["text"] == draft["text"]
    assert not any(x["type"] == "prompt" for x in state(c)["blocks"])


def test_14_editing_a_block_marks_its_spelling_fixes_stale(make):
    c, _ = make()
    iss = c.app.state.workspace.issue("099-test")
    iss.update_review(lambda d: d.__setitem__("proof", {"job": "j", "ts": 0, "skipped": 0, "fixes": [
        {"id": "f1", "block": "a", "before": "road", "after": "road", "applied": True},
        {"id": "f2", "block": "z", "before": "readers", "after": "readers", "applied": True}]}))
    assert edit(c, "a", "This week has found me on the road a lot more than usual.", "Other words.").status_code == 200
    fixes = {f["id"]: f for f in state(c)["review"]["proof"]["fixes"]}
    assert fixes["f1"].get("stale") is True and not fixes["f2"].get("stale")


def test_an_unchanged_save_on_prose_writes_nothing(make):
    c, d = make()
    before = (d / "issue.md").read_text()
    orig = "This week has found me on the road a lot more than usual."
    r = edit(c, "a", orig, orig + "\n")
    assert r.status_code == 200 and r.json().get("unchanged")
    assert (d / "issue.md").read_text() == before


def test_editing_is_undoable_from_history_not_by_undo_last_pass(make):
    """An edit is the author's own change: Undo last model pass must refuse to throw it away."""
    c, _ = make()
    st, draft = drafted(c)
    assert edit(c, draft["id"], draft["text"], "Mine now.").status_code == 200
    r = c.post(f"{S}/undo", json={}, headers=H)
    assert r.status_code == 409
    assert any(h.endswith("edit.md") or "edit" in h for h in state(c)["history"])


def test_r1_an_html_comment_in_the_box_is_refused_not_dropped(make):
    c, d = make()
    before = (d / "issue.md").read_text()
    r = edit(c, "a", "This week has found me on the road a lot more than usual.",
             "New introduction.\n\n<!-- Private editorial note -->")
    assert r.status_code == 422 and "<!--" in r.json()["error"]
    assert (d / "issue.md").read_text() == before


COMPONENT_DOC = DOC.replace("{#z}\n", """::: {.concept label="Voice file" #k}
A plain definition of one term.
:::

{#z}
""")


def test_r1_a_component_stays_that_component(make):
    c, d = make(text=COMPONENT_DOC)
    comp = next(b for b in state(c)["blocks"] if b["id"] == "k")
    assert comp["type"] == "component"
    before = (d / "issue.md").read_text()
    r = edit(c, "k", comp["text"], "Updated caption only.")
    assert r.status_code == 422 and "concept" in r.json()["error"]
    r = edit(c, "k", comp["text"], comp["text"].replace("concept", "previously"))
    assert r.status_code == 422
    assert (d / "issue.md").read_text() == before
    r = edit(c, "k", comp["text"], comp["text"].replace("A plain definition", "A shorter definition"))
    assert r.status_code == 200, r.text
    after = next(b for b in state(c)["blocks"] if b["id"] == "k")
    assert after["type"] == "component" and after["attrs"]["class"] == "concept"
    assert "A shorter definition of one term." in after["text"]


def test_r1_an_edit_withdraws_pending_proposals_for_that_block(make):
    c, _ = make()
    iss = c.app.state.workspace.issue("099-test")
    iss.update_review(lambda d: d["proposals"].extend([
        {"id": "p1", "status": "pending", "block": "a", "base": "x", "text": "y"},
        {"id": "p2", "status": "pending", "block": "z", "base": "x", "text": "y"}]))
    assert edit(c, "a", "This week has found me on the road a lot more than usual.", "Mine.").status_code == 200
    props = {p["id"]: p for p in state(c)["review"]["proposals"]}
    assert props["p1"]["status"] == "stale" and props["p2"]["status"] == "pending"


def test_r2_text_stays_text(make):
    c, d = make()
    before = (d / "issue.md").read_text()
    r = edit(c, "a", "This week has found me on the road a lot more than usual.",
             '::: {.concept label="X"}\nA definition.\n:::')
    assert r.status_code == 422 and "box" in r.json()["error"]
    r = edit(c, "a", "This week has found me on the road a lot more than usual.",
             'Mine first.\n\n::: {.concept label="X"}\nA definition.\n:::')
    assert r.status_code == 422
    assert (d / "issue.md").read_text() == before
