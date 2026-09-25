# SPDX-License-Identifier: MIT
"""Proof: the spelling and grammar pass (SPEC-PROOF.md). Numbers are the spec's checks."""
import json
import os
import time

import pytest

from conftest import FRONT
from slopmill import doc, proof, providers
from slopmill.voices import VoiceLibrary
from test_v2 import H, Fake, blocks_of, make, wait  # noqa: F401  (make is a fixture)

SLUG = "/api/issues/099-test"

DOC = FRONT + """{#a}
It was made using claude code and it's kinda meh at at the same time. I put it on git hub.

::: {.prompt #p}
Say what it does. Recieve this.
:::

::: {.draft #d for=p prompt=00000000}
Their going to love it. See [the recieve page](https://example.com/recieve) and `recieve`.
:::

{#c}
::: {.concept label="slopmill"}
A person who arange type for printing.
:::

{#z}
Thank you readers for sticking with us!
"""

GOOD = """=== FIX #a ===
FIND: claude code
REPLACE: Claude Code
WHY: name: Claude Code
=== END ===
=== FIX #a ===
FIND: at at the same
REPLACE: at the same
WHY: doubled word
=== END ===
=== FIX #a ===
FIND: git hub
REPLACE: GitHub
WHY: name: GitHub
=== END ===
=== FIX #d ===
FIND: Their going
REPLACE: They're going
WHY: their/they're
=== END ===
=== FIX #c ===
FIND: who arange type
REPLACE: who arranges type
WHY: spelling
=== END ===
"""


class ProofFake(Fake):
    """The writer, answering a proof call with a set reply."""

    def __init__(self, reply=GOOD, **kw):
        super().__init__(**kw)
        self.reply = reply

    def __call__(self, system, prompt, files, cancel=None):
        if system != proof.BRIEF:
            return super().__call__(system, prompt, files, cancel)
        self.calls.append({"system": system, "prompt": prompt, "files": files})
        end = time.time() + self.delay
        while time.time() < end:
            if cancel is not None and cancel.is_set():
                raise providers.Cancelled()
            time.sleep(0.02)
        return providers.Reply(self.reply, None)


def check(c):
    r = c.post(f"{SLUG}/proof", json={}, headers=H)
    assert r.status_code == 200, r.text
    return wait(c)


def fixes(c):
    return (c.get(SLUG).json()["review"].get("proof") or {}).get("fixes") or []


def toggle(c, fid, applied):
    return c.post(f"{SLUG}/proof/{fid}", json={"applied": applied}, headers=H)


# ── 2: what the checker is sent ─────────────────────────────────────────────────

def test_2_the_check_sends_the_brief_the_rules_and_the_text_not_the_samples(make):
    llm = ProofFake()
    c, d = make(llm=llm, text=DOC)
    assert check(c)["status"] == "done"
    call = llm.calls[-1]
    assert call["system"] == proof.BRIEF
    voice = VoiceLibrary(str(d.parent.parent)).get("fixture")
    roles = {e["path"]: e["role"] for e in voice.files()}
    names = [os.path.basename(p) for p in call["files"]]
    assert names[-1] == "DOCUMENT.md"
    assert names[:-1] and all(roles[n] == "rules" for n in names[:-1])
    assert not [n for n, r in roles.items() if r == "sample" and n in names]
    sent = open(call["files"][-1]).read()
    assert "[PROSE #a]" in sent and "[DRAFT #d]" in sent and "[COMPONENT #c concept]" in sent
    assert "Recieve this" not in sent            # prompts are not checked


def test_2_a_rules_file_switched_off_is_not_sent(make):
    llm = ProofFake()
    c, d = make(llm=llm, text=DOC)
    voice = VoiceLibrary(str(d.parent.parent)).get("fixture")
    m = voice.manifest()
    for e in m["files"]:
        if e["role"] == "rules":
            e["on"] = False
    voice._save_manifest(m)
    check(c)
    assert [os.path.basename(p) for p in llm.calls[-1]["files"]] == ["DOCUMENT.md"]


# ── 3: applied in one save ──────────────────────────────────────────────────────

def test_3_fixes_are_written_in_one_save_after_one_snapshot(make):
    c, d = make(llm=ProofFake(), text=DOC)
    before = len(c.get(SLUG).json()["history"])
    job = check(c)
    assert job["status"] == "done"
    text = (d / "issue.md").read_text()
    assert "using Claude Code and it's kinda meh at the same time. I put it on GitHub." in text
    assert "They're going to love it." in text
    assert "A person who arranges type for printing." in text
    st = c.get(SLUG).json()
    assert len(st["history"]) == before + 1 and "proof" in st["history"][-1]
    fx = st["review"]["proof"]["fixes"]
    assert len(fx) == 5 and all(f["applied"] for f in fx)
    assert st["review"]["last_pass"]["kind"] == "proof"


def test_3_nothing_to_fix_writes_nothing(make):
    c, d = make(llm=ProofFake(reply="=== NONE ==="), text=DOC)
    text = (d / "issue.md").read_text()
    assert check(c)["status"] == "done"
    assert (d / "issue.md").read_text() == text
    st = c.get(SLUG).json()
    assert st["review"]["proof"]["fixes"] == [] and "last_pass" not in st["review"]


# ── 4: what a fix may not touch ─────────────────────────────────────────────────

@pytest.mark.parametrize("block,find,replace,why", [
    ("d", "example.com/recieve", "example.com/receive", "not found in the words"),
    ("d", "`recieve`", "`receive`", "not plain text"),
    ("c", "slopmill", "Composer", "not found in the words"),
    ("a", "**made**", "made", "not plain text"),
    ("a", "made\nusing", "made using", "not plain text"),
    ("p", "Recieve this", "Receive this", "prompt block is not checked"),
    ("zz", "Thank you", "Thanks", "no such block"),
    ("z", "s", "S", "appears more than once"),
    ("a", "kinda", "kinda", "changes nothing"),
])
def test_4_a_fix_that_is_not_plain_visible_words_is_dropped(block, find, replace, why):
    blocks = doc.parse(DOC)[1]
    new, applied, dropped = proof.apply_fixes(blocks, [{"block": block, "find": find, "replace": replace, "why": "x"}])
    assert applied == [] and len(dropped) == 1 and why in dropped[0][1]
    assert [b.text for b in new] == [b.text for b in blocks]


def test_4_the_visible_word_in_link_text_is_fixed_and_the_address_is_not():
    blocks = doc.parse(DOC)[1]
    new, applied, dropped = proof.apply_fixes(blocks, [{"block": "d", "find": "the recieve page",
                                                        "replace": "the receive page", "why": "spelling"}])
    d = next(b for b in new if b.id == "d")
    assert "[the receive page](https://example.com/recieve)" in d.text and "`recieve`" in d.text
    assert len(applied) == 1 and not dropped


def test_4_bad_fixes_are_dropped_and_the_good_ones_still_applied(make):
    reply = GOOD + """=== FIX #d ===
FIND: example.com/recieve
REPLACE: example.com/receive
WHY: spelling
=== END ===
=== FIX #p ===
FIND: Recieve this
REPLACE: Receive this
WHY: spelling
=== END ===
"""
    c, d = make(llm=ProofFake(reply=reply), text=DOC)
    check(c)
    text = (d / "issue.md").read_text()
    assert "https://example.com/recieve" in text and "Recieve this." in text
    assert len(fixes(c)) == 5
    chat = c.get(SLUG).json()["review"]["chat"]    # nothing failed
    assert not [m for m in chat if m["role"] == "system"]


def test_4_two_fixes_whose_changed_letters_collide_keep_the_first():
    blocks = [doc.Block("prose", "a", "The word abcd is here and nowhere else.", {})]
    new, applied, dropped = proof.apply_fixes(blocks, [
        {"block": "a", "find": "abcd", "replace": "aXcd", "why": "one"},
        {"block": "a", "find": "aXcd", "replace": "aYcd", "why": "two"}])
    assert len(applied) == 1 and dropped[0][1] == "overlaps another fix"
    assert "aXcd" in new[0].text


# ── 5: switching back and forth ─────────────────────────────────────────────────

def test_5_every_fix_off_is_the_authors_file_byte_for_byte_and_on_again_is_the_fixed_one(make):
    c, d = make(llm=ProofFake(), text=DOC)
    original = (d / "issue.md").read_bytes()
    check(c)
    fixed = (d / "issue.md").read_bytes()
    for f in fixes(c):
        r = toggle(c, f["id"], False)
        assert r.status_code == 200, r.text
    assert (d / "issue.md").read_bytes() == original
    assert not [f for f in fixes(c) if f["applied"]]
    for f in fixes(c):
        assert toggle(c, f["id"], True).status_code == 200
    assert (d / "issue.md").read_bytes() == fixed


def test_5_one_fix_off_changes_only_its_own_words(make):
    c, d = make(llm=ProofFake(), text=DOC)
    check(c)
    fixed = (d / "issue.md").read_text()
    gh = next(f for f in fixes(c) if "GitHub" in f["after"])
    assert toggle(c, gh["id"], False).status_code == 200
    now = (d / "issue.md").read_text()
    assert now == fixed.replace(gh["after"], gh["before"], 1)
    assert "Claude Code" in now and "They're going" in now


def test_5_toggle_needs_a_true_or_false(make):
    c, d = make(llm=ProofFake(), text=DOC)
    check(c)
    f = fixes(c)[0]
    assert c.post(f"{SLUG}/proof/{f['id']}", json={"applied": "no"}, headers=H).status_code == 422
    assert c.post(f"{SLUG}/proof/fx-nope", json={"applied": False}, headers=H).status_code == 404


# ── 6: an edited sentence ───────────────────────────────────────────────────────

def test_6_a_fix_whose_sentence_was_edited_is_refused_and_marked_stale(make):
    c, d = make(llm=ProofFake(), text=DOC)
    check(c)
    gh = next(f for f in fixes(c) if "GitHub" in f["after"])
    (d / "issue.md").write_text((d / "issue.md").read_text().replace("I put it on GitHub.", "It lives on GitHub now."))
    edited = (d / "issue.md").read_text()
    r = toggle(c, gh["id"], False)
    assert r.status_code == 409 and "changed since the check" in r.json()["error"]
    assert (d / "issue.md").read_text() == edited
    assert next(f for f in fixes(c) if f["id"] == gh["id"])["stale"] is True


# ── 7: undo ─────────────────────────────────────────────────────────────────────

def test_7_undo_after_a_check_restores_the_file_and_clears_the_fixes(make):
    c, d = make(llm=ProofFake(), text=DOC)
    original = (d / "issue.md").read_text()
    check(c)
    r = c.post(f"{SLUG}/undo", json={}, headers=H)
    assert r.status_code == 200, r.text
    assert (d / "issue.md").read_text() == original
    assert "proof" not in c.get(SLUG).json()["review"]


# ── 8 and 9: while a pass runs ──────────────────────────────────────────────────

def test_8_a_change_to_the_file_during_the_check_means_it_writes_nothing(make):
    c, d = make(llm=ProofFake(delay=1.0), text=DOC)
    c.post(f"{SLUG}/proof", json={}, headers=H)
    time.sleep(0.3)
    assert c.put(f"{SLUG}/doc", json={"base": "x", "blocks": [], "meta": {}}, headers=H).status_code in (409, 422)
    changed = (d / "issue.md").read_text().replace("Thank you readers", "Thanks, readers")
    (d / "issue.md").write_text(changed)
    job = wait(c)
    assert job["status"] == "failed"
    assert (d / "issue.md").read_text() == changed
    assert "proof" not in c.get(SLUG).json()["review"]


def test_9_check_toggle_and_done_are_refused_while_a_pass_runs(make):
    c, d = make(llm=ProofFake(delay=1.0), text=DOC)
    check(c)
    f = fixes(c)[0]
    c.post(f"{SLUG}/generate", json={"targets": ["p"]}, headers=H)
    time.sleep(0.2)
    assert c.post(f"{SLUG}/proof", json={}, headers=H).status_code == 409
    assert toggle(c, f["id"], False).status_code == 409
    assert c.delete(f"{SLUG}/proof", headers=H).status_code == 409
    wait(c)


def test_done_clears_the_list_and_keeps_the_text(make):
    c, d = make(llm=ProofFake(), text=DOC)
    check(c)
    fixed = (d / "issue.md").read_text()
    assert c.delete(f"{SLUG}/proof", headers=H).status_code == 200
    assert (d / "issue.md").read_text() == fixed and fixes(c) == []


def test_there_is_nothing_to_check_in_an_issue_of_prompts(make):
    only = FRONT + "::: {.prompt #p}\nWrite something.\n:::\n"
    c, d = make(llm=ProofFake(), text=only)
    r = c.post(f"{SLUG}/proof", json={}, headers=H)
    assert r.status_code == 422 and "no text to check" in r.json()["error"]


# ── the parts ───────────────────────────────────────────────────────────────────

def test_mask_hides_everything_that_is_not_words_on_the_page():
    t = 'See [the page](https://a.b/c d) and **bold**{.green} `code` <b>x</b> https://x.y/z\n::: {.concept}\nok'
    m = proof.mask(t)
    assert len(m) == len(t)
    shown = "".join(ch for ch in m if ch != "\0")
    assert "https" not in shown and "green" not in shown and "code" not in shown
    assert "concept" not in shown and "the page" in shown and "bold" in shown and "ok" in shown


def test_widen_grows_a_short_fix_until_it_is_unique_and_visible():
    blocks = [doc.Block("prose", "a", "at the start, meh at at the end, and at the close.", {})]
    new, applied, _ = proof.apply_fixes(blocks, [{"block": "a", "find": "at at", "replace": "at", "why": "d"}])
    f = applied[0]
    assert new[0].text.count(f["after"]) == 1 and len(f["after"]) >= proof.MIN_SHOWN
    assert f["before"].replace("at at", "at", 1) == f["after"]


def test_a_fix_with_no_unique_words_around_it_goes_back_to_the_original():
    # after the fix both lines read "cat the", and a fix never widens across a line
    blocks = [doc.Block("prose", "a", "cat hte\ncat the", {}),
              doc.Block("prose", "b", "a hte b", {})]
    new, applied, dropped = proof.apply_fixes(blocks, [
        {"block": "a", "find": "hte", "replace": "the", "why": "spelling"},
        {"block": "b", "find": "hte", "replace": "the", "why": "spelling"}])
    assert new[0].text == "cat hte\ncat the" and "not unique enough" in dropped[0][1]
    assert [f["block"] for f in applied] == ["b"] and new[1].text == "a the b"


def test_parse_fixes_reads_the_format_and_ignores_the_rest():
    got = proof.parse_fixes("chatter\n=== FIX #b-1 ===\nFIND: a  b\nREPLACE: a b\nWHY:  doubled   space\n=== END ===\n")
    assert got == [{"block": "b-1", "find": "a  b", "replace": "a b", "why": "doubled space"}]


# ── review round 1 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,find", [
    ("Read [the guide][recieve] now.\n\n[recieve]: https://example.com/guide", "recieve"),   # F1
    ("Read [recieve][] now.", "recieve"),
    ("Read [recieve] now.", "recieve"),
])
def test_r1_a_reference_link_label_is_never_changed(text, find):
    blocks = [doc.Block("prose", "a", text, {})]
    new, applied, dropped = proof.apply_fixes(blocks, [{"block": "a", "find": find, "replace": "receive", "why": "s"}])
    assert applied == [] and new[0].text == text and "not found" in dropped[0][1]


def test_r1_the_visible_text_of_a_full_reference_link_can_be_fixed():
    text = "Read [the guied][guide] now."
    new, applied, _ = proof.apply_fixes([doc.Block("prose", "a", text, {})],
                                        [{"block": "a", "find": "the guied", "replace": "the guide", "why": "s"}])
    assert new[0].text == "Read [the guide][guide] now." and len(applied) == 1


def test_r1_a_fix_never_contains_or_grows_across_an_entity():
    # F3: the page shows "&" where the file has "&amp;", so a highlight could not find it
    text = "Tom &amp; teh friends went out."
    blocks = [doc.Block("prose", "a", text, {})]
    new, applied, _ = proof.apply_fixes(blocks, [{"block": "a", "find": "teh friends", "replace": "the friends", "why": "s"}])
    assert new[0].text == "Tom &amp; the friends went out."
    assert "&" not in applied[0]["after"] and "&" not in applied[0]["before"]
    _, none, dropped = proof.apply_fixes(blocks, [{"block": "a", "find": "&amp; teh", "replace": "&amp; the", "why": "s"}])
    assert none == [] and "not found" in dropped[0][1]


def test_r1_a_check_that_fixed_nothing_records_nothing_when_the_file_moved_on(make):
    # F2
    c, d = make(llm=ProofFake(reply="=== NONE ===", delay=1.0), text=DOC)
    c.post(f"{SLUG}/proof", json={}, headers=H)
    time.sleep(0.3)
    (d / "issue.md").write_text((d / "issue.md").read_text().replace("Thank you readers", "Thanks, readers"))
    assert wait(c)["status"] == "failed"
    assert "proof" not in c.get(SLUG).json()["review"]


def test_r1_a_check_during_a_pass_is_refused_before_anything_else(make):
    # F4: an issue with no text to check still answers 409, not 422, while a pass runs
    only = FRONT + "::: {.prompt #p}\nWrite something.\n:::\n"
    c, d = make(llm=ProofFake(delay=1.0), text=only)
    c.post(f"{SLUG}/generate", json={"targets": ["p"]}, headers=H)
    time.sleep(0.2)
    assert c.post(f"{SLUG}/proof", json={}, headers=H).status_code == 409
    wait(c)


def test_r1_a_toggle_whose_text_is_already_switched_only_updates_the_flag(make):
    # blind spot B2: issue.md saved, review.json not (a crash between the two)
    c, d = make(llm=ProofFake(), text=DOC)
    check(c)
    gh = next(f for f in fixes(c) if "GitHub" in f["after"])
    (d / "issue.md").write_text((d / "issue.md").read_text().replace(gh["after"], gh["before"], 1))
    now = (d / "issue.md").read_bytes()
    r = toggle(c, gh["id"], False)
    assert r.status_code == 200 and r.json()["fix"]["applied"] is False
    assert (d / "issue.md").read_bytes() == now
    assert toggle(c, gh["id"], True).status_code == 200 and "GitHub" in (d / "issue.md").read_text()


# ── review round 2 ──────────────────────────────────────────────────────────────

def test_r2_neighbouring_fixes_that_share_words_both_apply_and_switch_alone():
    # F1: "recieve teh" then "teh thing" share "teh"; only the changed letters must not collide
    blocks = [doc.Block("prose", "a", "I recieve teh thing today.", {})]
    new, applied, dropped = proof.apply_fixes(blocks, [
        {"block": "a", "find": "recieve teh", "replace": "receive teh", "why": "spelling"},
        {"block": "a", "find": "teh thing", "replace": "the thing", "why": "spelling"}])
    assert new[0].text == "I receive the thing today." and len(applied) == 2 and not dropped
    one = proof.toggle(new, applied[0], False)
    assert one[0].text == "I recieve the thing today."
    both = proof.toggle(one, applied[1], False)
    assert both[0].text == "I recieve teh thing today."
    assert proof.toggle(proof.toggle(both, applied[1], True), applied[0], True)[0].text == new[0].text


def test_r2_the_other_earlier_cases_still_widen_to_whole_words():
    blocks = [doc.Block("prose", "a", "It was kinda meh at at the same time.", {})]
    _, applied, _ = proof.apply_fixes(blocks, [{"block": "a", "find": "at at", "replace": "at", "why": "d"}])
    assert applied[0]["after"] == "kinda meh at the same" and applied[0]["before"] == "kinda meh at at the same"


@pytest.mark.parametrize("text", [
    "See ![chart](images/foo_(bar)-recieve.png) now.",                   # F2: balanced parentheses
    "See [the page](https://x.com/a_(recieve)) now.",
    "See https://x.com/a_(recieve) now.",                                # a bare address
])
def test_r2_no_part_of_an_address_is_ever_changed(text):
    blocks = [doc.Block("prose", "a", text, {})]
    new, applied, dropped = proof.apply_fixes(blocks, [{"block": "a", "find": "recieve", "replace": "receive", "why": "s"}])
    assert applied == [] and new[0].text == text


def test_r2_the_full_stop_after_a_link_or_address_is_still_words():
    for t in ("Go [here](https://x.com). Recieve it.", "Go to https://x.com. Recieve it."):
        new, applied, _ = proof.apply_fixes([doc.Block("prose", "a", t, {})],
                                            [{"block": "a", "find": "Recieve it", "replace": "Receive it", "why": "s"}])
        assert new[0].text.endswith(". Receive it.") and len(applied) == 1


def test_r2_a_fix_that_would_look_the_same_twice_on_the_page_is_left_out():
    # F3: unique in the file (straight vs curly, link brackets), not on the page
    text = "Their going now. [They\u2019re going now](https://example.com)."
    new, applied, dropped = proof.apply_fixes([doc.Block("prose", "a", text, {})],
                                              [{"block": "a", "find": "Their going now", "replace": "They're going now", "why": "s"}])
    assert applied == [] and new[0].text == text and "not unique enough" in dropped[0][1]


def test_r2_the_page_count_straightens_quotes_and_drops_markdown_marks():
    t = "It\u2019s **very** [good](https://x.y) and it's very good."
    m = proof.mask(t)
    assert proof.shown_count(t, "it's very good", m) == 1 and proof.shown_count(t, "very good", m) == 2


def test_r2_a_check_refuses_an_issue_where_two_blocks_share_an_id(make):
    # blind spot B2
    dup = FRONT + "{#a}\nOne teh.\n\n{#a}\nTwo teh.\n"
    c, d = make(llm=ProofFake(), text=dup)
    r = c.post(f"{SLUG}/proof", json={}, headers=H)
    assert r.status_code == 422 and "share the ID #a" in r.json()["error"]


# ── review round 3 ──────────────────────────────────────────────────────────────

def test_r3_a_file_change_while_the_pass_waits_for_the_model_writes_nothing(make):
    # F1: another issue's call holds the model slot; the file changes before this pass starts
    from slopmill.server.jobs import MODEL_SLOT
    c, d = make(llm=ProofFake(), text=DOC)
    assert MODEL_SLOT.acquire(timeout=5)
    try:
        c.post(f"{SLUG}/proof", json={}, headers=H)
        time.sleep(0.4)
        changed = (d / "issue.md").read_text().replace("Thank you readers", "Thanks, readers")
        (d / "issue.md").write_text(changed)
    finally:
        MODEL_SLOT.release()
    assert wait(c)["status"] == "failed"
    assert (d / "issue.md").read_text() == changed
    assert "proof" not in c.get(SLUG).json()["review"]


def test_r3_a_toggle_is_refused_when_the_page_would_show_the_words_twice(make):
    # F2: the author adds a curly-quote copy of the fixed words after the check
    c, d = make(llm=ProofFake(), text=DOC)
    check(c)
    fx = next(f for f in fixes(c) if "They're" in f["after"])
    text = (d / "issue.md").read_text()
    (d / "issue.md").write_text(text.replace(fx["after"], fx["after"].replace("'", "’") + " " + fx["after"], 1))
    before = (d / "issue.md").read_text()
    r = toggle(c, fx["id"], False)
    assert r.status_code == 409 and (d / "issue.md").read_text() == before


def test_r3_fixes_on_touching_letters_both_apply_when_they_can_be_told_apart():
    # F3: adjacent changed spans do not collide; an insertion touching a span does
    assert proof._collide(0, 1, 1, 2) is False
    assert proof._collide(1, 1, 1, 2) is True and proof._collide(0, 2, 1, 1) is True
    blocks = [doc.Block("prose", "a", "The team wrote teh abc report on Monday afternoon.", {})]
    new, applied, dropped = proof.apply_fixes(blocks, [
        {"block": "a", "find": "teh abc report", "replace": "the abc report", "why": "s"},
        {"block": "a", "find": "the abc report", "replace": "the ABC report", "why": "name"}])
    assert new[0].text == "The team wrote the ABC report on Monday afternoon." and len(applied) == 2


def test_r3_a_reply_with_no_fixes_and_no_none_fails_instead_of_saying_all_clear(make):
    # blind spot B1: a cut-off reply
    c, d = make(llm=ProofFake(reply="=== FIX #a ===\nFIND: git h"), text=DOC)
    text = (d / "issue.md").read_text()
    assert check(c)["status"] == "failed"
    assert (d / "issue.md").read_text() == text and "proof" not in c.get(SLUG).json()["review"]
    chat = c.get(SLUG).json()["review"]["chat"]
    assert any("cut off" in m["text"] for m in chat if m["role"] == "system")


def test_r3_a_block_quote_is_never_corrected():
    # blind spot B2: someone else's words
    text = "He said:\n\n> I recieve too many emails.\n\nI recieve them too."
    new, applied, dropped = proof.apply_fixes([doc.Block("prose", "a", text, {})],
                                              [{"block": "a", "find": "I recieve", "replace": "I receive", "why": "s"}])
    assert new[0].text == "He said:\n\n> I recieve too many emails.\n\nI receive them too."
