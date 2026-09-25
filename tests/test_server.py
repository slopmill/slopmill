# SPDX-License-Identifier: MIT
"""The web app's API, driven with a fake model. No real model is ever called."""
import os
import re
import shutil
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURE, FIXTURE_PACK, FRONT, HERE
from slopmill import agent, doc
from slopmill.pack import load_pack
from slopmill.server.app import create_app

H = {"x-slopmill": "1"}

DOC = FRONT + """{#a}
This week has found me on the road a lot more than usual.

::: {.prompt #p}
Talk about the voice file revisions. About 60 words.
:::

{#z}
Thank you readers for sticking with us.
"""


class FakeLLM:
    """Answers every block it is asked for. `replies` overrides per block ID."""

    def __init__(self, replies=None, delay=0.0, fail=None):
        self.replies = replies or {}
        self.delay = delay
        self.fail = fail
        self.calls = []

    def __call__(self, system, prompt, files, cancel=None):
        self.calls.append({"system": system, "prompt": prompt, "files": files,
                           "document": open(files[-1], encoding="utf-8").read()})
        end = time.time() + self.delay
        while time.time() < end:
            if cancel is not None and cancel.is_set():
                raise agent.Cancelled()
            time.sleep(0.02)
        if self.fail:
            raise agent.LLMError(self.fail)
        ids = re.findall(r"#([A-Za-z][\w-]*)", prompt.split("\n\nGeneral direction")[0])
        out = []
        for i in dict.fromkeys(ids):
            text = self.replies.get(i, f"Written for {i}. **A phrase**{{.green}} here.")
            if text is not None:
                out.append(f"=== BLOCK {i} ===\n{text}\n=== END ===")
        out.append("=== NOTE ===\nI kept it short.\n=== END ===")
        return "\n".join(out)


@pytest.fixture
def make(tmp_path):
    def go(llm=None, text=DOC, lint=None):
        ws = tmp_path / "ws"
        d = ws / "issues" / "099-test"
        d.mkdir(parents=True)
        (d / "issue.md").write_text(text)
        pack = load_pack(FIXTURE_PACK)
        app = create_app(workspace=str(ws), pack=pack, llm=llm or FakeLLM(), model_label="fake",
                         lint_argv=lint, token="tok")
        c = TestClient(app)
        c.cookies.set("slopmill_token", "tok")
        return c, d
    return go


def wait(c, slug="099-test", timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        job = c.get(f"/api/issues/{slug}").json()["job"]
        if job and job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_access_needs_the_token(make):
    c, _ = make()
    anon = TestClient(c.app)
    assert anon.get("/api/state").status_code == 401
    assert anon.get("/").status_code == 401
    assert anon.get("/?t=wrong").status_code == 401
    r = anon.get("/?t=tok", follow_redirects=False)
    assert r.status_code == 303 and "slopmill_token=tok" in r.headers["set-cookie"]
    assert "samesite=strict" in r.headers["set-cookie"].lower()


def test_writes_need_the_header(make):
    c, _ = make()
    assert c.post("/api/issues", json={"title": "x"}).status_code == 403
    assert c.post("/api/issues", json={"title": "x"}, headers=H).status_code == 200


def test_load_and_save_round_trip(make):
    c, d = make()
    s = c.get("/api/issues/099-test").json()
    assert [b["type"] for b in s["blocks"]] == ["prose", "prompt", "prose"]
    s["blocks"][0]["text"] = "Edited first paragraph."
    r = c.put("/api/issues/099-test/doc", json={"base": s["rev"], "meta": s["meta"],
                                                "blocks": s["blocks"]}, headers=H)
    assert r.status_code == 200, r.text
    assert "Edited first paragraph." in (d / "issue.md").read_text()
    # a save that does not say what it was based on is refused
    r0 = c.put("/api/issues/099-test/doc", json={"meta": s["meta"], "blocks": s["blocks"]}, headers=H)
    assert r0.status_code == 422
    # stale base is refused
    r2 = c.put("/api/issues/099-test/doc", json={"base": s["rev"], "meta": s["meta"],
                                                 "blocks": s["blocks"]}, headers=H)
    assert r2.status_code == 409


def test_save_refuses_structure_breaking_text(make):
    c, d = make()
    s = c.get("/api/issues/099-test").json()
    before = (d / "issue.md").read_text()
    s["blocks"][0]["text"] = "one\n\ntwo"
    r = c.put("/api/issues/099-test/doc", json={"base": s["rev"], "meta": s["meta"],
                                                "blocks": s["blocks"]}, headers=H)
    assert r.status_code == 422
    assert (d / "issue.md").read_text() == before


def test_generate_writes_a_draft_and_never_touches_prose(make):
    llm = FakeLLM(replies={"a": "The model tried to rewrite the author."})
    c, d = make(llm)
    r = c.post("/api/issues/099-test/generate", json={}, headers=H)
    assert r.json()["targets"] == ["p"]
    job = wait(c)
    assert job["status"] == "done", c.get("/api/issues/099-test").json()
    s = c.get("/api/issues/099-test").json()
    types = [(b["type"], b["id"]) for b in s["blocks"]]
    assert types[:2] == [("prose", "a"), ("prompt", "p")] and types[2][0] == "draft"
    draft = s["blocks"][2]
    assert draft["attrs"]["for"] == "p" and draft["text"].startswith("Written for p.")
    assert s["blocks"][0]["text"] == "This week has found me on the road a lot more than usual."
    assert s["review"]["chat"][-1] == {**s["review"]["chat"][-1], "role": "model", "text": "I kept it short."}
    # the model saw the voice pack and the document, prose marked as prose
    call = llm.calls[0]
    assert "[PROSE #a]" in call["document"] and "[PROMPT #p]" in call["document"]
    assert any(f.endswith("VOICE.md") for f in call["files"])
    assert "No em-dashes" in call["system"] and "=== BLOCK" in call["system"]
    # the preview shows the draft
    p = c.get("/api/issues/099-test/preview").json()
    assert "Written for p." in p["html"] and p["prompts"]["p"]


def test_generate_discards_a_reply_the_pack_cannot_draw(make):
    c, d = make(FakeLLM(replies={"p": "Some <b>raw html</b> text."}))
    before = (d / "issue.md").read_text()
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    assert (d / "issue.md").read_text() == before
    events = c.app.state  # noqa
    s = c.get("/api/issues/099-test").json()
    assert s["job"]["blocks"]["p"]["status"] == "failed"
    assert "raw HTML" in s["job"]["blocks"]["p"]["detail"]


def test_generate_discards_a_reply_that_breaks_out_of_its_block(make):
    c, d = make(FakeLLM(replies={"p": "Escaping.\n:::\n\n{#evil}\nInjected prose."}))
    before = (d / "issue.md").read_text()
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    assert (d / "issue.md").read_text() == before


def test_stale_draft_regenerates(make):
    c, d = make()
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    s = c.get("/api/issues/099-test").json()
    assert s["blocks"][2]["attrs"]["stale"] == ""
    s["blocks"][1]["text"] = "A different instruction entirely."
    c.put("/api/issues/099-test/doc", json={"base": s["rev"], "meta": s["meta"], "blocks": s["blocks"]}, headers=H)
    s = c.get("/api/issues/099-test").json()
    assert s["blocks"][2]["attrs"]["stale"] == "1"
    r = c.post("/api/issues/099-test/generate", json={}, headers=H)
    assert r.json()["targets"] == ["p"]
    wait(c)
    s = c.get("/api/issues/099-test").json()
    assert s["blocks"][2]["attrs"]["stale"] == ""
    assert sum(1 for b in s["blocks"] if b["type"] == "draft") == 1


def test_revise_draft_applies_prose_proposes(make):
    llm = FakeLLM()
    c, d = make(llm)
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    s = c.get("/api/issues/099-test").json()
    did = s["blocks"][2]["id"]
    llm.replies = {did: "A shorter draft.", "a": "The model's version of the author's words."}
    c.post("/api/issues/099-test/comments", json={"block": did, "quote": "Written", "note": "Shorter."}, headers=H)
    c.post("/api/issues/099-test/comments", json={"block": "a", "quote": "road", "note": "Tighten."}, headers=H)
    r = c.post("/api/issues/099-test/revise", json={"general": "Keep it light."}, headers=H)
    assert r.status_code == 200, r.text
    assert wait(c)["status"] == "done"
    s = c.get("/api/issues/099-test").json()
    by = {b["id"]: b for b in s["blocks"]}
    assert by[did]["text"] == "A shorter draft."
    assert by["a"]["text"] == "This week has found me on the road a lot more than usual."
    props = [p for p in s["review"]["proposals"] if p["status"] == "pending"]
    assert len(props) == 1 and props[0]["block"] == "a"
    assert all(c_["status"] == "sent" for c_ in s["review"]["comments"])
    assert "Comments:" in llm.calls[-1]["prompt"] and "Keep it light." in llm.calls[-1]["prompt"]
    # accept puts it in
    r = c.post(f"/api/issues/099-test/proposals/{props[0]['id']}/accept", json={}, headers=H)
    assert r.status_code == 200, r.text
    s = c.get("/api/issues/099-test").json()
    assert {b["id"]: b for b in s["blocks"]}["a"]["text"] == "The model's version of the author's words."


def test_accept_refuses_a_stale_proposal(make):
    llm = FakeLLM(replies={"a": "Model version."})
    c, d = make(llm)
    c.post("/api/issues/099-test/comments", json={"block": "a", "quote": "", "note": "Tighten."}, headers=H)
    c.post("/api/issues/099-test/revise", json={}, headers=H)
    wait(c)
    s = c.get("/api/issues/099-test").json()
    prop = s["review"]["proposals"][0]
    s["blocks"][0]["text"] = "The author edited it meanwhile."
    c.put("/api/issues/099-test/doc", json={"base": s["rev"], "meta": s["meta"], "blocks": s["blocks"]}, headers=H)
    r = c.post(f"/api/issues/099-test/proposals/{prop['id']}/accept", json={}, headers=H)
    assert r.status_code == 409
    assert "The author edited it meanwhile." in (d / "issue.md").read_text()


def test_stop_writes_nothing(make):
    c, d = make(FakeLLM(delay=5))
    before = (d / "issue.md").read_text()
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    time.sleep(0.3)
    assert c.post("/api/issues/099-test/stop", json={}, headers=H).json()["stopped"]
    assert wait(c)["status"] == "stopped"
    assert (d / "issue.md").read_text() == before


def test_model_failure_is_reported(make):
    c, d = make(FakeLLM(fail="429 usage limit reached"))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    assert wait(c)["status"] == "failed"
    chat = c.get("/api/issues/099-test").json()["review"]["chat"]
    assert "429 usage limit reached" in chat[-1]["text"]


def test_saves_blocked_while_the_model_works(make):
    c, d = make(FakeLLM(delay=1))
    s = c.get("/api/issues/099-test").json()
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    time.sleep(0.2)
    r = c.put("/api/issues/099-test/doc", json={"base": s["rev"], "meta": s["meta"], "blocks": s["blocks"]}, headers=H)
    assert r.status_code == 409
    wait(c)


def test_undo_restores_before_the_pass(make):
    c, d = make()
    before = (d / "issue.md").read_text()
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    assert (d / "issue.md").read_text() != before
    assert c.post("/api/issues/099-test/undo", json={}, headers=H).status_code == 200
    assert (d / "issue.md").read_text() == before


def test_lint_runs_and_is_reported(make, tmp_path):
    script = tmp_path / "lint.py"
    script.write_text("import sys\nprint('✗ [em-dash] line 1: em-dash in prose')\nprint('    …context…')\nprint('! [soft] maybe')\nsys.exit(1)\n")
    c, d = make(lint=[sys.executable, str(script), "{body}", "--subject", "{subject}"])
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    lint = c.get("/api/issues/099-test/preview").json()["lint"]
    assert lint["ok"] is False
    assert lint["findings"][0] == {"level": "error", "text": "[em-dash] line 1: em-dash in prose", "hint": "…context…"}


def test_create_issue_and_list(make):
    c, _ = make()
    r = c.post("/api/issues", json={"title": "All About Goblins", "number": 23}, headers=H)
    slug = r.json()["slug"]
    assert slug == "023-all-about-goblins"
    s = c.get(f"/api/issues/{slug}").json()
    assert s["meta"]["title"] == "All About Goblins" and s["blocks"][0]["type"] == "prompt"
    assert any(i["slug"] == slug for i in c.get("/api/state").json()["issues"])


def test_events_stream_replays_over_a_real_server(make):
    import socket

    import httpx
    import uvicorn
    c, _ = make()
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(c.app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        seen = ""
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/issues/099-test/events?since=0",
                          cookies={"slopmill_token": "tok"}, timeout=5) as r:
            assert r.status_code == 200
            for chunk in r.iter_text():
                seen += chunk
                if '"status": "done"' in seen:
                    break
        assert '"type": "log"' in seen and '"type": "block"' in seen
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/issues/099-test/events",
                          timeout=5) as r:
            assert r.status_code == 401
    finally:
        server.should_exit = True
        t.join(timeout=5)


def test_voice_files_listed_and_confined(make):
    """The design's old [voice] list seeds a voice pack; only its own files can be read."""
    c, _ = make()
    v = c.get("/api/voices/fixture").json()
    assert any(f["path"] == "VOICE.md" and f["role"] == "rules" for f in v["files"])
    assert c.get("/api/voices/fixture/file", params={"path": "VOICE.md"}).status_code == 200
    for bad in ("../pack.toml", "/etc/passwd", "voice.json", ".history"):
        assert c.get("/api/voices/fixture/file", params={"path": bad}).status_code == 404
    assert c.put("/api/voices/fixture/file", json={"path": "pack.toml", "text": "x"},
                 headers=H).status_code == 422


def test_command_llm_payload_cap_and_cancel(tmp_path):
    big = tmp_path / "big.md"
    big.write_text("x" * 120_000)
    llm = agent.CommandLLM(["true"])
    with pytest.raises(agent.LLMError, match="capped"):
        llm("s", "p", [str(big)])
    slow = agent.CommandLLM([sys.executable, "-c", "import time; time.sleep(30)"])
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()
    t0 = time.time()
    with pytest.raises(agent.Cancelled):
        slow("s", "p", [], cancel=ev)
    assert time.time() - t0 < 5
    echo = agent.CommandLLM([sys.executable, "-c", "import sys; print(sys.argv[1:4])"])
    assert "'-s', 'SYS', 'PROMPT'" in echo("SYS", "PROMPT", [])


def test_stop_after_the_result_is_written_says_so(make):
    """Stop and the write take the same lock: a pass is stopped or it wrote, never both."""
    c, d = make()
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    assert wait(c)["status"] == "done"
    r = c.post("/api/issues/099-test/stop", json={}, headers=H)
    assert r.json()["stopped"] is False


def test_commit_refuses_after_stop(make):
    import threading as th
    gate = th.Event()

    class Gated(FakeLLM):
        def __call__(self, *a, **k):
            out = super().__call__(*a, **k)
            gate.wait(5)
            return out
    c, d = make(Gated())
    before = (d / "issue.md").read_text()
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    time.sleep(0.3)
    assert c.post("/api/issues/099-test/stop", json={}, headers=H).json()["stopped"] is True
    gate.set()
    assert wait(c)["status"] == "stopped"
    assert (d / "issue.md").read_text() == before


def test_proposal_is_stale_when_the_block_changed_type(make):
    c, d = make(FakeLLM(replies={"a": "Model version."}))
    c.post("/api/issues/099-test/comments", json={"block": "a", "quote": "", "note": "x"}, headers=H)
    c.post("/api/issues/099-test/revise", json={}, headers=H)
    wait(c)
    s = c.get("/api/issues/099-test").json()
    prop = s["review"]["proposals"][0]
    s["blocks"][0]["type"] = "prompt"          # same text, now an instruction
    r = c.put("/api/issues/099-test/doc", json={"base": s["rev"], "meta": s["meta"], "blocks": s["blocks"]}, headers=H)
    assert r.status_code == 200, r.text
    assert c.post(f"/api/issues/099-test/proposals/{prop['id']}/accept", json={}, headers=H).status_code == 409


def test_missing_model_command_is_reported_in_chat(make, tmp_path):
    c, d = make(agent.CommandLLM([str(tmp_path / "no-such-writer")]))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    assert wait(c)["status"] == "failed"
    chat = c.get("/api/issues/099-test").json()["review"]["chat"]
    assert "could not start the model command" in chat[-1]["text"]


def test_bus_reports_a_gap():
    from slopmill.server.jobs import Bus
    b = Bus(keep=3)
    for i in range(6):
        b.publish("log", text=str(i))
    events, gap = b.since(0)
    assert gap and [e["seq"] for e in events] == [4, 5, 6]
    events, gap = b.since(3)
    assert not gap


def test_undo_refused_after_later_edits(make):
    c, d = make()
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    s = c.get("/api/issues/099-test").json()
    s["blocks"][0]["text"] = "An edit made after the pass."
    c.put("/api/issues/099-test/doc", json={"base": s["rev"], "meta": s["meta"], "blocks": s["blocks"]}, headers=H)
    r = c.post("/api/issues/099-test/undo", json={}, headers=H)
    assert r.status_code == 409 and "changed since the last model pass" in r.json()["error"]
    assert "An edit made after the pass." in (d / "issue.md").read_text()


def test_failed_comment_stays_queued_and_nth_reaches_the_model(make):
    llm = FakeLLM(replies={"a": "Some <b>bad</b> markup."})
    c, d = make(llm)
    c.post("/api/issues/099-test/comments", json={"block": "a", "quote": "the", "nth": 1, "note": "x"}, headers=H)
    c.post("/api/issues/099-test/revise", json={}, headers=H)
    wait(c)
    com = c.get("/api/issues/099-test").json()["review"]["comments"][0]
    assert com["status"] == "queued" and "raw HTML" in com["error"]
    assert "second time" in llm.calls[-1]["prompt"]


def test_a_broken_linter_is_reported_as_an_error(make, tmp_path):
    script = tmp_path / "lint.py"
    script.write_text("import sys\nsys.stderr.write('configuration file missing')\nsys.exit(2)\n")
    c, d = make(lint=[sys.executable, str(script), "{body}"])
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    lint = c.get("/api/issues/099-test/preview").json()["lint"]
    assert lint["findings"][0]["level"] == "error"
    assert "configuration file missing" in lint["findings"][0]["text"]
