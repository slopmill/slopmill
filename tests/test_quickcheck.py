# SPDX-License-Identifier: MIT
"""The free quick check (LanguageTool), against a stand-in for its API: no network."""
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURE_PACK, FRONT
from slopmill import doc, quickcheck
from slopmill.pack import load_pack
from slopmill.server.app import create_app

H = {"x-slopmill": "1"}
WORDS = {"recieve": ("receive", "TYPOS", "Spelling mistake"), "the the": ("the", "MISC", "Word repetition"),
         "tuesday": ("Tuesday", "TYPOS", "Spelling mistake"), "Frobnitz": ("Frobnicate", "TYPOS", "Spelling mistake")}


def fake_lt(seen):
    """Answers /check like LanguageTool: a match for every known mistake in the text."""
    def handler(req):
        form = parse_qs(req.content.decode())
        text = form["text"][0]
        seen.append({"text": text, "form": form})
        matches = []
        for bad, (good, cat, msg) in WORDS.items():
            start = 0
            while (i := text.find(bad, start)) >= 0:
                matches.append({"offset": i, "length": len(bad), "replacements": [{"value": good}],
                                "shortMessage": msg, "rule": {"category": {"id": cat}}})
                start = i + 1
        return httpx.Response(200, json={"matches": matches})
    return handler


def blocks_of(text):
    return doc.parse(text)[1]


def test_suggestions_become_fixes_on_the_right_blocks():
    seen = []
    lt = quickcheck.LanguageTool(transport=httpx.MockTransport(fake_lt(seen)))
    blocks = blocks_of("{#a}\nI will recieve it on tuesday.\n\n{#b}\nWe read the the book.\n")
    new, applied, dropped = quickcheck.quick_check(blocks, lt)
    assert [b.text for b in new] == ["I will receive it on Tuesday.", "We read the book."]
    assert {f["block"] for f in applied} == {"a", "b"} and not dropped
    assert len(seen) == 1                                          # both blocks in one request
    assert "STYLE" in seen[0]["form"]["disabledCategories"][0]     # style is never asked about


def test_the_authors_own_words_are_left_alone():
    lt = quickcheck.LanguageTool(transport=httpx.MockTransport(fake_lt([])))
    blocks = blocks_of("{#a}\nThe Frobnitz will recieve it.\n")
    new, applied, _ = quickcheck.quick_check(blocks, lt, known={"frobnitz"})
    assert new[0].text == "The Frobnitz will receive it."


def test_known_words_come_from_the_voice_pack_and_the_dictionary(tmp_path):
    (tmp_path / "dictionary.txt").write_text("Zorbling\n")
    class V:
        def files(self):
            return [{"path": "VOICE.md", "on": True, "missing": False}, {"path": "off.md", "on": False, "missing": False}]
        def path(self, p):
            return str(tmp_path / p)
    (tmp_path / "VOICE.md").write_text("I say Frobnitz a lot.")
    (tmp_path / "off.md").write_text("Wibblewobble")
    words = quickcheck.known_words(V(), str(tmp_path))
    assert {"frobnitz", "zorbling", "slopmill"} <= words and "wibblewobble" not in words


def test_links_and_markup_are_never_sent_as_words():
    seen = []
    lt = quickcheck.LanguageTool(transport=httpx.MockTransport(fake_lt(seen)))
    blocks = blocks_of("{#a}\nSee [the page](https://example.com/recieve) on tuesday.\n")
    new, _, _ = quickcheck.quick_check(blocks, lt)
    assert "example.com" not in seen[0]["text"] and len(seen[0]["text"]) == len(blocks[0].text)
    assert new[0].text == "See [the page](https://example.com/recieve) on Tuesday."


def test_long_issues_go_in_several_requests(monkeypatch):
    monkeypatch.setattr(quickcheck, "CHUNK", 60)
    seen = []
    lt = quickcheck.LanguageTool(transport=httpx.MockTransport(fake_lt(seen)))
    text = "".join(f"{{#b{n}}}\nParagraph {n} will recieve a letter on the same day.\n\n" for n in range(4))
    new, applied, _ = quickcheck.quick_check(blocks_of(text), lt)
    assert len(seen) == 4 and len(applied) == 4
    assert all("receive" in b.text for b in new)


def test_a_busy_service_says_so():
    lt = quickcheck.LanguageTool(transport=httpx.MockTransport(lambda r: httpx.Response(429, text="slow down")))
    with pytest.raises(quickcheck.CheckError, match="busy"):
        quickcheck.quick_check(blocks_of("{#a}\nText.\n"), lt)


def make(tmp_path, checker, demo=False):
    ws = tmp_path / "ws"
    d = ws / "issues" / "099-test"
    d.mkdir(parents=True)
    (d / "issue.md").write_text(FRONT + "{#a}\nWe will recieve it on tuesday.\n")
    app = create_app(workspace=str(ws), pack=load_pack(FIXTURE_PACK), llm=lambda *a, **k: "",
                     model_label="x", token="tok", checker=checker, demo=demo)
    c = TestClient(app)
    c.cookies.set("slopmill_token", "tok")
    return c, d


def wait(c):
    for _ in range(200):
        j = c.get("/api/issues/099-test").json()["job"]
        if j and j["status"] != "running":
            return j
        time.sleep(0.05)


def test_the_endpoint_applies_switchable_fixes_like_the_ai_check(tmp_path):
    c, d = make(tmp_path, quickcheck.LanguageTool(transport=httpx.MockTransport(fake_lt([]))))
    assert c.get("/api/state").json()["quickcheck"] is True
    assert c.post("/api/issues/099-test/quickcheck", json={}, headers=H).status_code == 200
    assert wait(c)["status"] == "done"
    assert "We will receive it on Tuesday." in (d / "issue.md").read_text()
    pr = c.get("/api/issues/099-test").json()["review"]["proof"]
    assert pr["source"] == "languagetool" and len(pr["fixes"]) == 2
    fid = pr["fixes"][0]["id"]
    assert c.post(f"/api/issues/099-test/proof/{fid}", json={"applied": False}, headers=H).status_code == 200
    assert "recieve" in (d / "issue.md").read_text() or "tuesday" in (d / "issue.md").read_text()


def test_off_when_not_configured_and_in_the_demo(tmp_path):
    c, _ = make(tmp_path, None)
    assert c.get("/api/state").json()["quickcheck"] is False
    assert c.post("/api/issues/099-test/quickcheck", json={}, headers=H).status_code == 422
    c2, _ = make(tmp_path / "d", quickcheck.LanguageTool(transport=httpx.MockTransport(fake_lt([]))), demo=True)
    assert c2.get("/api/state").json()["quickcheck"] is False
    assert c2.post("/api/issues/099-test/quickcheck", json={}, headers=H).status_code == 422


def test_config_turns_it_on_by_default_and_off_with_an_empty_url():
    from slopmill.cli import quickcheck_from
    assert quickcheck_from({}).url == quickcheck.DEFAULT_URL
    assert quickcheck_from({"checker": {"url": ""}}) is None
    assert quickcheck_from({"checker": {"url": "http://localhost:8010/v2", "language": "en-GB"}}).language == "en-GB"


def test_positions_after_an_emoji_land_on_the_word():
    """LanguageTool counts UTF-16 units, as Java does: an emoji is two of them."""
    def handler(req):
        text = parse_qs(req.content.decode())["text"][0]
        units = text.encode("utf-16-le")
        i = units.find("recieve".encode("utf-16-le")) // 2
        return httpx.Response(200, json={"matches": [{"offset": i, "length": 7, "replacements": [{"value": "receive"}],
                                                      "rule": {"category": {"id": "TYPOS"}}}]})
    lt = quickcheck.LanguageTool(transport=httpx.MockTransport(handler))
    new, applied, _ = quickcheck.quick_check(blocks_of("{#a}\nHi 😀 we will recieve it.\n"), lt)
    assert new[0].text == "Hi 😀 we will receive it."


def test_one_long_paragraph_is_split_across_requests(monkeypatch):
    monkeypatch.setattr(quickcheck, "CHUNK", 80)
    seen = []
    lt = quickcheck.LanguageTool(transport=httpx.MockTransport(fake_lt(seen)))
    para = " ".join(f"Sentence {n} will recieve a note." for n in range(8))
    new, applied, _ = quickcheck.quick_check(blocks_of("{#a}\n" + para + "\n"), lt)
    assert len(seen) > 1 and all(len(s["text"]) <= 80 for s in seen)
    assert new[0].text.count("receive") == 8 and "recieve" not in new[0].text


def test_a_busy_service_partway_keeps_what_was_checked(monkeypatch):
    monkeypatch.setattr(quickcheck, "CHUNK", 40)
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) > 1:
            return httpx.Response(429, text="slow down")
        return fake_lt([])(req)
    lt = quickcheck.LanguageTool(transport=httpx.MockTransport(handler))
    logs = []
    text = "{#a}\nWe will recieve it.\n\n{#b}\nThey will recieve it too.\n"
    new, applied, _ = quickcheck.quick_check(blocks_of(text), lt, log=lambda lv, t: logs.append(t))
    assert new[0].text == "We will receive it." and new[1].text == "They will recieve it too."
    assert any("only part of the issue was checked" in t for t in logs)


def test_pieces_fit_in_bytes_and_do_not_cut_words(monkeypatch):
    accented = "é" * 50 + " " + "à" * 50 + " " + "ü" * 50
    for start, piece in quickcheck._pieces(accented, 120):
        assert quickcheck._bytes(piece) <= 120
    words = "a " * 60 + "recieve"
    pieces = quickcheck._pieces(words, 100)
    assert any("recieve" in p for _, p in pieces)
    assert "".join(p for _, p in pieces) == words
