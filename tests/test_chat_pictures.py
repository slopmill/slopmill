# SPDX-License-Identifier: MIT
"""SPEC-CHAT-PICTURES: the chat answers, the author's own pictures, charts, research.
No real model, image model, search engine or web page is ever reached."""
import json
import os
import re
import struct
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURE_PACK, FRONT
from slopmill import agent, charts, doc, providers, research
from slopmill.net import Fetched, FetchRefused
from slopmill.pack import load_pack
from slopmill.server import app as app_mod
from slopmill.server.app import create_app
from slopmill.server.jobs import MODEL_SLOT

H = {"x-slopmill": "1"}
def _png(w=4, h=3):
    import zlib

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\x10\x80\xf0" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = _png()

DOC = FRONT + """{#a}
This week has found me on the road a lot more than usual.

::: {.prompt #p}
Talk about the road. About 60 words.
:::

{#z}
Thank you readers for sticking with us.
"""

CHART_OK = """=== CHART {id} ===
TYPE: column
TITLE: How big the big models are
UNIT: billion parameters
DATA:
Small | 100
Medium | 4,000
Large | 10000
SOURCE: {source}
ALT: Three columns: 100, 4,000 and 10,000 billion.
CAPTION: The little one is there.
=== END ==="""


class Script:
    """A writer that answers each call with the next function of `replies`, given (system,
    prompt, files). Calls are kept for the test to read."""

    def __init__(self, *replies, delay=0.0, max_request_bytes=115_000):
        self.replies, self.delay, self.calls = list(replies), delay, []
        self.label, self.max_request_bytes = "script", max_request_bytes

    def __call__(self, system, prompt, files, cancel=None):
        self.calls.append({"system": system, "prompt": prompt, "files": list(files),
                           "attached": {os.path.basename(f): open(f, encoding="utf-8").read() for f in files}})
        time.sleep(self.delay)
        r = self.replies.pop(0) if self.replies else (lambda s, p, f: "=== NOTE ===\nnothing\n=== END ===")
        out = r(system, prompt, files) if callable(r) else r
        if isinstance(out, Exception):
            raise out
        return providers.Reply(out, None)


class FakeSearch:
    label = "fake search"

    def __init__(self, results=None):
        self.queries = []
        self.results = results if results is not None else [
            {"title": "Model sizes", "url": "https://example.org/sizes",
             "snippet": "Large has 10,000 billion parameters."}]

    def search(self, query, limit=6):
        self.queries.append(query)
        return self.results[:limit]


def fake_fetch(pages):
    def fetch(url, **kw):
        page = pages.get(url)
        if isinstance(page, Exception):
            raise page
        if page is None:
            return Fetched(404, b"", "text/html", url)
        return Fetched(200, page.encode(), "text/html; charset=utf-8", url)
    return fetch


@pytest.fixture
def make(tmp_path):
    def go(llm=None, images=None, text=DOC, searcher=None, demo=False):
        ws = tmp_path / "ws"
        d = ws / "issues" / "099-test"
        d.mkdir(parents=True, exist_ok=True)
        (d / "issue.md").write_text(text)
        app = create_app(workspace=str(ws), pack=load_pack(FIXTURE_PACK), llm=llm or Script(),
                         model_label="fake", token="tok", images=images, research=searcher, demo=demo)
        c = TestClient(app)
        c.cookies.set("slopmill_token", "tok")
        return c, d
    return go


def chat_of(c):
    return c.get("/api/issues/099-test").json()["review"]["chat"]


def wait_answer(c, n=1, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        got = [m for m in chat_of(c) if m.get("kind") in ("answer", "ask-failed")]
        if len(got) >= n and not c.get("/api/issues/099-test").json()["asking"]:
            return got[-1]
        time.sleep(0.05)
    raise AssertionError("no answer")


def wait_job(c, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        job = c.get("/api/issues/099-test").json()["job"]
        if job and job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("pass did not finish")


def blocks_of(d):
    return doc.parse((d / "issue.md").read_text())[1]


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


# ── A. the chat answers ──────────────────────────────────────────────────────────

def test_1_ask_answers_and_changes_nothing(make):
    llm = Script("Hello! Here is a prompt for it:\n=== PROMPT ===\nImage: a red kite at dusk\n=== END ===")
    c, d = make(llm=llm)
    before = (d / "issue.md").read_bytes()
    snaps = c.get("/api/issues/099-test").json()["history"]
    r = c.post("/api/issues/099-test/ask", json={"text": "hello"}, headers=H)
    assert r.status_code == 200 and r.json()["asking"]
    m = wait_answer(c)
    assert m["role"] == "model" and m["text"].startswith("Hello!") and "=== PROMPT" not in m["text"]
    assert m["prompts"] == ["Image: a red kite at dusk"] and m["change"] is False and m["asked"] == "hello"
    assert (d / "issue.md").read_bytes() == before                     # nothing written
    st = c.get("/api/issues/099-test").json()
    assert st["history"] == snaps and st["job"] is None                # no pass, no snapshot
    assert [x["role"] for x in st["review"]["chat"]] == ["user", "model"]
    assert agent.ASK_MARK in llm.calls[0]["system"]
    assert "DOCUMENT.md" in llm.calls[0]["attached"] and "[PROMPT #p]" in llm.calls[0]["attached"]["DOCUMENT.md"]
    usage = [json.loads(x) for x in (d.parent.parent / "usage.jsonl").read_text().splitlines()]
    assert usage[-1]["pass_kind"] == "ask"


def test_5_a_change_request_offers_the_button(make):
    llm = Script("I would cut the second sentence of your opening.\n=== CHANGE ===")
    c, _ = make(llm=llm)
    c.post("/api/issues/099-test/ask", json={"text": "make the intro shorter"}, headers=H)
    m = wait_answer(c)
    assert m["change"] is True and m["asked"] == "make the intro shorter" and "CHANGE" not in m["text"]


def test_3_history_goes_with_the_next_question(make):
    llm = Script("First answer.", "Second answer.")
    c, _ = make(llm=llm)
    c.post("/api/issues/099-test/ask", json={"text": "first question"}, headers=H)
    wait_answer(c)
    c.post("/api/issues/099-test/ask", json={"text": "second question"}, headers=H)
    wait_answer(c, 2)
    p = llm.calls[1]["prompt"]
    assert "Author: first question" in p and "You: First answer." in p and p.rstrip().endswith("second question")


def test_6_one_question_at_a_time_and_editing_carries_on(make):
    llm = Script("slow answer", delay=0.6)
    c, d = make(llm=llm)
    assert c.post("/api/issues/099-test/ask", json={"text": "one"}, headers=H).status_code == 200
    assert c.post("/api/issues/099-test/ask", json={"text": "two"}, headers=H).status_code == 409
    st = c.get("/api/issues/099-test").json()
    assert st["asking"] is True
    blocks = st["blocks"]
    blocks[0]["text"] = "Edited while the model thinks."
    r = c.put("/api/issues/099-test/doc", json={"base": st["rev"], "meta": st["meta"], "blocks": blocks}, headers=H)
    assert r.status_code == 200, r.text
    wait_answer(c)
    assert "Edited while the model thinks." in (d / "issue.md").read_text()


def test_6_ask_waits_for_the_model_slot_instead_of_failing(make):
    c, _ = make(llm=Script("answered"))
    MODEL_SLOT.acquire()
    try:
        c.post("/api/issues/099-test/ask", json={"text": "q"}, headers=H)
        time.sleep(0.4)
        assert not [m for m in chat_of(c) if m.get("kind") == "answer"]
    finally:
        MODEL_SLOT.release()
    assert wait_answer(c)["text"] == "answered"


def test_6_a_failed_call_says_so(make):
    c, _ = make(llm=Script(lambda s, p, f: agent.LLMError("the plan is used up")))
    c.post("/api/issues/099-test/ask", json={"text": "q"}, headers=H)
    m = wait_answer(c)
    assert m["role"] == "system" and m["kind"] == "ask-failed" and "the plan is used up" in m["text"]
    assert c.post("/api/issues/099-test/ask", json={"text": "again"}, headers=H).status_code == 200


def test_6_empty_or_long_questions_are_refused(make):
    c, _ = make()
    assert c.post("/api/issues/099-test/ask", json={"text": "  "}, headers=H).status_code == 422
    assert c.post("/api/issues/099-test/ask", json={"text": "x" * 4001}, headers=H).status_code == 422
    assert c.post("/api/issues/099-test/ask", json={"text": 5}, headers=H).status_code == 422


def test_4_answer_parsing_caps():
    reply = "Here.\n" + "".join(f"=== PROMPT ===\nChart: n{i}\n=== END ===\n" for i in range(7))
    answer, prompts, change = agent.parse_answer(reply)
    assert answer == "Here." and len(prompts) == 5 and not change
    assert agent.parse_answer("")[0] == "(the model sent an empty answer)"


def test_7_demo_answers_from_a_stand_in():
    from slopmill.demo import DemoWriter
    out = DemoWriter(delay=0)(agent.ASK_MARK + "\nrest", "hello", [])
    answer, prompts, _ = agent.parse_answer(out)
    assert "demo" in answer and prompts and prompts[0].startswith("Chart:")


# ── B. your own picture ──────────────────────────────────────────────────────────

def upload(c, data, **q):
    from urllib.parse import quote
    st = c.get("/api/issues/099-test").json()
    q.setdefault("base", st["rev"])
    words = {f"x-picture-{k}": quote(q.pop(k)) for k in ("alt", "caption", "name") if k in q}
    words.setdefault("x-picture-alt", quote("A red kite over a field"))
    return c.post("/api/issues/099-test/pictures", params=q, content=data,
                  headers={**H, "content-type": "application/octet-stream", **words})


def test_8_upload_goes_in_as_the_authors_figure(make):
    c, d = make()
    r = upload(c, PNG, after="a", caption="Taken on the *road*", name="My Kite!!.PNG")
    assert r.status_code == 200, r.text
    name = r.json()["name"]
    assert re.fullmatch(r"own-\d{8}-\d{6}-my-kite\.png", name)
    from PIL import Image
    with Image.open(d / "images" / name) as im:          # saved again from its pixels alone
        assert im.format == "PNG" and im.size == (4, 3)
    bs = blocks_of(d)
    assert [b.type for b in bs][:2] == ["prose", "component"] and bs[1].id == r.json()["id"]
    assert f"({name})" in bs[1].text and "Taken on the \\*road\\*" in bs[1].text
    assert "A red kite over a field" in bs[1].text
    prev = c.get("/api/issues/099-test/preview").json()
    assert f"/api/issues/099-test/asset/{name}" in prev["html"]
    assert len(c.get("/api/issues/099-test").json()["history"]) == 1      # a snapshot to undo to


def test_8_upload_at_the_top(make):
    c, d = make()
    assert upload(c, PNG, after="").status_code == 200
    assert blocks_of(d)[0].type == "component"


def test_9_alt_and_caption_cannot_break_the_document(make):
    c, d = make()
    r = upload(c, PNG, after="a", alt="a ](evil.jpg) ![x", caption="::: {.prompt #hack}\nboo\n:::")
    assert r.status_code == 200, r.text
    bs = blocks_of(d)
    assert [b.type for b in bs] == ["prose", "component", "prompt", "prose"]
    assert agent.media_refs(bs[1].text, load_pack(FIXTURE_PACK), top_level=True) == {r.json()["name"]}


@pytest.mark.parametrize("data,q,status,what", [
    (b"GIF89a....", {}, 422, "JPEG, PNG or WebP"),
    (PNG, {"alt": "   "}, 422, "alt text"),
    (PNG, {"alt": "x" * 401}, 422, "400"),
    (PNG, {"base": "stale"}, 409, "changed"),
    (PNG, {"after": "nope"}, 409, "no longer exists"),
    (PNG, {"replace": "a"}, 409, "no longer exists"),       # a prose block is not a draft
    (b"", {}, 422, "no picture"),
])
def test_11_refusals_leave_no_file(make, data, q, status, what):
    c, d = make()
    before = (d / "issue.md").read_bytes()
    r = upload(c, data, **q)
    assert r.status_code == status and what in r.json()["error"]
    assert (d / "issue.md").read_bytes() == before
    assert not (d / "images").exists() or not os.listdir(d / "images")


def test_9_too_big_is_refused(make, monkeypatch):
    monkeypatch.setattr(app_mod, "MAX_UPLOAD", 50)
    c, d = make()
    r = upload(c, PNG + b"\x00" * 100)
    assert r.status_code == 413
    assert not (d / "images").exists() or not os.listdir(d / "images")


def test_9_demo_cap_is_smaller(make, monkeypatch):
    monkeypatch.setattr(app_mod, "DEMO_MAX_UPLOAD", 60)
    c, _ = make(demo=True)
    assert upload(c, PNG + b"\x00" * 100).status_code == 413


def test_11_refused_while_a_pass_runs(make):
    c, _ = make(llm=Script("=== BLOCK p ===\nx\n=== END ===", delay=0.8))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    time.sleep(0.2)
    assert upload(c, PNG, after="a").status_code == 409
    wait_job(c)


def test_11_design_without_a_figure(make, tmp_path):
    import shutil
    no_fig = tmp_path / "nofig"
    shutil.copytree(FIXTURE_PACK, no_fig)
    toml = (no_fig / "pack.toml").read_text()
    toml = re.sub(r"\[components\.figure\][^\[]*", "", toml)
    (no_fig / "pack.toml").write_text(toml)
    ws = tmp_path / "ws2"
    (ws / "issues" / "099-test").mkdir(parents=True)
    (ws / "issues" / "099-test" / "issue.md").write_text(DOC)
    c = TestClient(create_app(workspace=str(ws), pack=load_pack(str(no_fig)), llm=Script(), token="tok"))
    c.cookies.set("slopmill_token", "tok")
    assert upload(c, PNG).status_code == 422


def test_10_use_my_own_picture_replaces_a_drawn_draft(make):
    from test_v2 import FakeImages
    pic_doc = DOC.replace("Talk about the road. About 60 words.", "Image: a road at dusk")
    llm = Script("=== IMAGE p ===\nDESCRIPTION: a road\nALT: A road at dusk\nCAPTION: x\n=== END ===")
    c, d = make(llm=llm, images=FakeImages(), text=pic_doc)
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait_job(c)
    draft = next(b for b in blocks_of(d) if b.type == "draft")
    r = upload(c, PNG, replace=draft.id)
    assert r.status_code == 200, r.text
    bs = blocks_of(d)
    assert [b.type for b in bs] == ["prose", "component", "prose"]      # prompt and draft gone
    assert r.json()["name"] in bs[1].text


# ── C. charts ───────────────────────────────────────────────────────────────────

def test_14_numbers():
    assert charts.number("4,000") == 4000 and charts.number("$1,234.5") == 1234.5
    assert charts.number("12%") == 12 and charts.number("-3") == -3 and charts.number(".5") == 0.5
    for bad in ("4k", "1,23", "ten", "1e9", "inf", "", "4 000 000x"):
        with pytest.raises(charts.ChartError):
            charts.number(bad)


def raw(**over):
    base = {"type": "column", "title": "T", "unit": "", "series": "", "scale": "",
            "source": "S", "alt": "A", "caption": "", "rows": ["a | 1", "b | 2"]}
    base.update(over)
    return base


@pytest.mark.parametrize("over,what", [
    ({"type": "donut"}, "type"),
    ({"title": ""}, "title"),
    ({"source": ""}, "SOURCE"),
    ({"alt": ""}, "alt"),
    ({"rows": []}, "no data"),
    ({"rows": [f"r{i} | {i}" for i in range(31)]}, "at most 30"),
    ({"rows": ["a | 1 | 2", "b | 2"]}, "same number"),
    ({"rows": ["a | 1 | 2", "b | 2 | 3"]}, "SERIES"),
    ({"rows": ["a | 1 | 2", "b | 2 | 3"], "series": "x"}, "names 1"),
    ({"rows": ["a | 1 | 2 | 3 | 4 | 5", "b | 1 | 2 | 3 | 4 | 5"], "series": "a|b|c|d|e"}, "at most 4"),
    ({"type": "pie", "rows": [f"r{i} | 1" for i in range(9)]}, "at most 8"),
    ({"type": "pie", "rows": ["a | -1", "b | 2"]}, "zero or more"),
    ({"type": "pie", "rows": ["a | 0", "b | 0"]}, "zero or more"),
    ({"scale": "log", "rows": ["a | 0", "b | 2"]}, "log"),
    ({"scale": "cubic"}, "SCALE"),
    ({"rows": ["a"]}, "label | number"),
    ({"rows": [" | 3", "b | 1"]}, "label"),
    ({"rows": ["a | 3 apples"]}, "plain number"),
    ({"title": "x" * 121}, "title"),
])
def test_14_bad_data_is_refused(over, what):
    with pytest.raises(charts.ChartError, match=re.escape(what)):
        charts.check_spec(raw(**over))


def test_13_every_type_draws_a_png_of_the_right_size(tmp_path):
    specs = [raw(type=t) for t in charts.TYPES] + [
        raw(type="line", rows=["a | 1 | 2", "b | 3 | 1"], series="x | y"),
        raw(type="bar", rows=["A very long label that has to wrap somewhere | 300", "b | 1"], scale="log")]
    for i, r in enumerate(specs):
        out = tmp_path / f"c{i}.png"
        charts.draw(charts.check_spec(r), str(out), ["#112233", "#445566"])
        assert png_size(out) == (charts.WIDTH, charts.HEIGHT)


def chart_doc(prompt):
    return DOC.replace("Talk about the road. About 60 words.", prompt)


def test_12_chart_prompt_is_drawn_by_slopmill_without_the_image_model(make):
    llm = Script(CHART_OK.format(id="p", source="The author's prompt") + "\n=== NOTE ===\nok\n=== END ===")
    c, d = make(llm=llm, text=chart_doc("Chart: model sizes. Small 100, Medium 4000, Large 10000"))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    assert wait_job(c)["status"] == "done"
    assert "=== CHART <id> ===" in llm.calls[0]["system"] and "CHART prompts" in llm.calls[0]["prompt"]
    draft = next(b for b in blocks_of(d) if b.type == "draft")
    name = re.search(r"\]\((chart-p-[^)]+\.png)\)", draft.text).group(1)
    assert png_size(d / "images" / name) == (1600, 900)
    side = json.loads((d / "images" / f".{name}.json").read_text())
    assert side["chart"]["values"] == [[100.0], [4000.0], [10000.0]] and side["model"] == "slopmill chart"
    st = c.get("/api/issues/099-test").json()
    assert any(b["attrs"].get("chart") == "1" for b in st["blocks"])
    assert not any(n.startswith(".drawing-") for n in os.listdir(d / "images"))


def test_15_an_image_prompt_asking_for_a_chart_becomes_a_chart(make):
    from test_v2 import FakeImages
    imgs = FakeImages()
    llm = Script(CHART_OK.format(id="p", source="The writer's memory, not checked"))
    c, d = make(llm=llm, images=imgs, text=chart_doc("Image: make a chart of model sizes"))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    assert wait_job(c)["status"] == "done"
    assert imgs.calls == []
    assert "chart-p-" in next(b for b in blocks_of(d) if b.type == "draft").text


def test_14_a_bad_chart_fails_only_itself_and_leaves_nothing(make):
    doc2 = chart_doc("Chart: sizes").replace("{#z}", "::: {.prompt #q}\nA closing line.\n:::\n\n{#z}")
    bad = CHART_OK.format(id="p", source="x").replace("Medium | 4,000", "Medium | lots")
    llm = Script(bad + "\n=== BLOCK q ===\nGoodbye.\n=== END ===")
    c, d = make(llm=llm, text=doc2)
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait_job(c)
    drafts = {b.attrs["for"]: b for b in blocks_of(d) if b.type == "draft"}
    assert "p" not in drafts and drafts["q"].text == "Goodbye."
    assert not (d / "images").exists() or not os.listdir(d / "images")


def test_12_a_chart_prompt_answered_with_text_fails(make):
    c, d = make(llm=Script("=== BLOCK p ===\nNo chart today.\n=== END ==="), text=chart_doc("Chart: sizes"))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait_job(c)
    assert not [b for b in blocks_of(d) if b.type == "draft"]


def test_17_a_comment_redraws_the_chart_and_the_writer_sees_its_data(make):
    first = CHART_OK.format(id="p", source="The author")
    llm = Script(first)
    c, d = make(llm=llm, text=chart_doc("Chart: sizes"))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait_job(c)
    draft = next(b for b in blocks_of(d) if b.type == "draft")
    llm.replies.append(lambda s, p, f: CHART_OK.format(id=draft.id, source="The author").replace("TYPE: column", "TYPE: bar"))
    st = c.get("/api/issues/099-test").json()
    c.post("/api/issues/099-test/comments", json={"block": draft.id, "note": "make it horizontal", "quote": ""}, headers=H)
    c.post("/api/issues/099-test/revise", json={"general": "make it horizontal"}, headers=H)
    wait_job(c)
    sent = llm.calls[1]["attached"]["DOCUMENT.md"]
    assert f"[DRAFT #{draft.id} for #p · CHART]" in sent and "Medium | 4,000" in sent
    assert "A DRAFT marked CHART" in llm.calls[1]["prompt"]
    new = next(b for b in blocks_of(d) if b.type == "draft")
    assert new.text != draft.text and "chart-" in new.text
    name = re.search(r"\]\((chart-[^)]+\.png)\)", new.text).group(1)
    assert json.loads((d / "images" / f".{name}.json").read_text())["chart"]["type"] == "bar"


def test_18_demo_draws_the_visitors_numbers():
    from slopmill.demo import DemoWriter
    import tempfile
    tmp = tempfile.mkdtemp()
    docf = os.path.join(tmp, "DOCUMENT.md")
    open(docf, "w").write("[PROMPT #c · CHART]\nChart: apples 12, pears 7\n")
    out = DemoWriter(delay=0)("sys", "Write the text for these PROMPT blocks: #c. x\n"
                              "These are CHART prompts: answer each with a CHART block: #c.", [docf])
    spec = charts.check_spec(charts.parse_charts(out)["c"])
    assert spec["labels"] == ["apples", "pears"] and spec["values"] == [[12.0], [7.0]]


# ── research ─────────────────────────────────────────────────────────────────────

PAGE = """<html><head><title>Model sizes, 2026</title><script>var x = 1;</script></head><body>
<nav>Home | About</nav><p>Some chatter about the weather.</p>
<table><tr><th>Model</th><th>Parameters</th></tr><tr><td>Large</td><td>10,000 billion</td></tr></table>
<p>The Large model has 10,000 billion parameters, the report says.</p><footer>© someone</footer></body></html>"""


def test_research_page_text_keeps_tables_and_drops_scripts():
    title, lines = research.page_text(PAGE.encode(), "text/html")
    assert title == "Model sizes, 2026"
    assert "Large | 10,000 billion" in lines and not any("var x" in ln for ln in lines)
    assert not any("Home" in ln for ln in lines) and not any("someone" in ln for ln in lines)
    ex = research.excerpt(lines, {"large", "parameters"}, 200)
    assert "10,000" in ex and "weather" not in ex


def test_research_duckduckgo_results_page():
    page = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa&amp;rut=x">'
            'The <b>A</b> page</a><a class="result__snippet" href="x">Snippet <b>one</b></a>'
            '<a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Advert</a><a class="result__snippet">ad</a>'
            '<a class="result__a" href="http://plain.example/b">Not https</a><a class="result__snippet">b</a>')
    got = research.parse_duckduckgo(page)
    assert got == [{"title": "The A page", "url": "https://example.org/a", "snippet": "Snippet one"}]


def test_research_wants():
    w = research.wants_research
    assert w("Chart: anything", chart=True) and w("Research the tallest trees") and w("please look it up")
    assert w("Image: make a chart of sizes") and w("pull information on the larger models")
    assert w("the latest numbers on rent") and not w("Image: a red kite") and not w("Two paragraphs on my week")


def test_research_cite_maps_numbers_to_sites():
    src = [{"n": 1, "title": "A", "url": "https://www.example.org/a", "fetched": "d"},
           {"n": 2, "title": "B", "url": "https://data.example.net/b", "fetched": "d"}]
    line, used = research.cite("S1, S2; S1, S9, the author", src)
    assert line == "example.org, data.example.net, the author"      # S9 was never a source
    assert [u["n"] for u in used] == [1, 2]


def test_research_gather_budget_refusals_and_snippets():
    results = [{"title": "A", "url": "https://a.example/1", "snippet": "a snippet"},
               {"title": "B", "url": "https://b.example/2", "snippet": "b snippet with 12"},
               {"title": "C", "url": "https://c.example/3", "snippet": ""}]
    fetch = fake_fetch({"https://a.example/1": PAGE, "https://b.example/2": FetchRefused("private")})
    logs = []
    sources, text = research.gather({"c": ["q1"]}, {"c": "chart the large model"}, FakeSearch(results), 5000,
                                    lambda lv, t: logs.append(t), fetch=fetch)
    assert [s["url"] for s in sources] == ["https://a.example/1", "https://b.example/2"]   # C had nothing
    assert "[S1] Model sizes, 2026" in text and "10,000" in text
    assert "[S2] B" in text and "(search snippet only" in text and "b snippet with 12" in text
    assert any("could not read b.example" in ln for ln in logs)
    assert len(text.encode()) <= 5000


def test_research_chart_end_to_end(make, monkeypatch):
    monkeypatch.setattr(research, "fetch_public", fake_fetch({"https://example.org/sizes": PAGE}))
    search = FakeSearch()
    llm = Script("=== SEARCH p ===\nlargest model parameters 2026\n=== END ===",
                 CHART_OK.format(id="p", source="S1") + "\n=== NOTE ===\nfrom S1\n=== END ===")
    c, d = make(llm=llm, searcher=search, text=chart_doc("Chart: research the biggest models by size"))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    assert wait_job(c)["status"] == "done"
    assert research.PLAN_SYSTEM in llm.calls[0]["system"] and "[p] Chart: research" in llm.calls[0]["prompt"]
    assert search.queries == ["largest model parameters 2026"]
    write = llm.calls[1]
    assert "SOURCES.md" in write["attached"] and "[S1]" in write["attached"]["SOURCES.md"]
    assert "Research:" in write["system"] and "RESEARCHED: #p" in write["prompt"]
    draft = next(b for b in blocks_of(d) if b.type == "draft")
    name = re.search(r"\]\((chart-[^)]+\.png)\)", draft.text).group(1)
    side = json.loads((d / "images" / f".{name}.json").read_text())
    assert side["chart"]["source"] == "example.org"
    assert side["sources"] == [{"n": 1, "title": "Model sizes, 2026", "url": "https://example.org/sizes",
                                "fetched": time.strftime("%Y-%m-%d")}]
    st = c.get("/api/issues/099-test").json()
    assert st["sources"][draft.id][0]["url"] == "https://example.org/sizes"


def test_research_none_means_no_search(make):
    search = FakeSearch()
    llm = Script("=== SEARCH p ===\nNONE\n=== END ===", CHART_OK.format(id="p", source="the prompt"))
    c, d = make(llm=llm, searcher=search, text=chart_doc("Chart: a 1, b 2"))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait_job(c)
    assert search.queries == [] and "SOURCES.md" not in llm.calls[1]["attached"]


def test_research_off_or_no_room_skips_the_planning_call(make):
    llm = Script(CHART_OK.format(id="p", source="x"))
    c, _ = make(llm=llm, text=chart_doc("Chart: research sizes"))           # no searcher
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait_job(c)
    assert len(llm.calls) == 1
    small = Script(CHART_OK.format(id="p", source="x"))
    c2, _ = make(llm=small, searcher=FakeSearch(), text=chart_doc("Chart: research sizes"))
    # room for the writing call, not for sources on top of it
    small.max_request_bytes = c2.get("/api/issues/099-test").json()["next_request"]["bytes"] + 1000
    assert c2.post("/api/issues/099-test/generate", json={}, headers=H).status_code == 200
    assert wait_job(c2)["status"] == "done"
    assert len(small.calls) == 1


def test_research_in_the_chat_returns_sources(make, monkeypatch):
    monkeypatch.setattr(research, "fetch_public", fake_fetch({"https://example.org/sizes": PAGE}))
    llm = Script("=== SEARCH question ===\nlarge model size\n=== END ===",
                 "The large one has 10,000 billion ([source](https://example.org/sizes)).")
    c, _ = make(llm=llm, searcher=FakeSearch())
    c.post("/api/issues/099-test/ask", json={"text": "research how big the large model is"}, headers=H)
    m = wait_answer(c)
    assert m["sources"] == [{"n": 1, "title": "Model sizes, 2026", "url": "https://example.org/sizes"}]
    assert "SOURCES.md" in llm.calls[1]["attached"]


def test_research_config():
    assert isinstance(research.from_config({}), research.DuckDuckGo)          # no writer given
    assert research.from_config({"research": {"provider": "off"}}) is None
    s = research.from_config({"research": {"provider": "command", "command": "x --q {query}"}})
    assert s.argv == ["x", "--q", "{query}"]
    with pytest.raises(ValueError):
        research.from_config({"research": {"provider": "bing"}})


# ── the writer's own web search (API keys) ──────────────────────────────────────

NATIVE_REPLY = """=== SOURCE ===
FOR: p
TITLE: Model sizes, 2026
URL: https://example.org/sizes/
FACTS:
- Large: 10,000 billion parameters (report, March 2026)
=== END ===
=== SOURCE ===
FOR: p
TITLE: Made up
URL: https://invented.example/nothing
FACTS:
- 99 trillion
=== END ==="""


def test_native_sources_are_checked_against_what_the_search_saw():
    logs = []
    sources, text = research.from_native(NATIVE_REPLY, [{"url": "https://example.org/sizes", "title": "t"}],
                                         {"p": "chart it"}, 5000, lambda lv, t: logs.append(t))
    assert [s["url"] for s in sources] == ["https://example.org/sizes"]
    assert "invented" not in text and "10,000 billion" in text and "[S1] Model sizes, 2026" in text
    assert any("never returned" in ln for ln in logs)


def test_native_openai_responses_call():
    import httpx
    seen_body = {}

    def handler(req):
        seen_body.update(json.loads(req.content))
        assert req.url.path.endswith("/responses")
        return httpx.Response(200, json={"output": [
            {"type": "web_search_call", "action": {"sources": [{"url": "https://example.org/sizes", "title": "S"}]}},
            {"type": "message", "content": [{"type": "output_text", "text": NATIVE_REPLY,
                                             "annotations": [{"type": "url_citation", "url": "https://b.example/x", "title": "B"}]}]}],
            "usage": {"input_tokens": 100, "output_tokens": 50}})
    w = providers.OpenAIText("gpt-6-sol", key_env=None, transport=httpx.MockTransport(handler))
    reply, seen = w.web_research("sys", "find it")
    assert seen_body["tools"] == [{"type": "web_search"}] and seen_body["instructions"] == "sys"
    assert "=== SOURCE ===" in reply and reply.usage == {"in": 100, "out": 50, "exact": True}
    assert [s["url"] for s in seen] == ["https://example.org/sizes", "https://b.example/x"]
    assert providers.OpenAIText("x", base_url="https://openrouter.ai/api/v1").web_search is False


def test_native_anthropic_resumes_a_paused_search():
    from types import SimpleNamespace as NS

    class Stream:
        def __init__(self, msg):
            self.msg, self.text_stream = msg, iter(())
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get_final_message(self):
            return self.msg

    usage = NS(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    first = NS(stop_reason="pause_turn", usage=usage, content=[
        NS(type="server_tool_use"),
        NS(type="web_search_tool_result", content=[NS(url="https://example.org/sizes", title="S")])])
    second = NS(stop_reason="end_turn", usage=usage, content=[
        NS(type="web_search_tool_result", content=NS(error_code="max_uses_exceeded")),
        NS(type="text", text=NATIVE_REPLY, citations=[NS(url="https://c.example/y", title="C")])])
    calls = []

    class Messages:
        def stream(self, **kw):
            calls.append(kw)
            return Stream([first, second][len(calls) - 1])

    os.environ["FAKE_ANTHROPIC_KEY"] = "k"
    w = providers.AnthropicText("claude-opus-5", key_env="FAKE_ANTHROPIC_KEY",
                                client_factory=lambda key: NS(messages=Messages()))
    reply, seen = w.web_research("sys", "find it")
    assert calls[0]["tools"][0]["type"] == "web_search_20260209"
    assert len(calls) == 2 and calls[1]["messages"][-1] == {"role": "assistant", "content": first.content}
    assert reply == NATIVE_REPLY and reply.usage["in"] == 20
    assert [s["url"] for s in seen] == ["https://example.org/sizes", "https://c.example/y"]
    old = providers.AnthropicText("claude-haiku-4-5", key_env="FAKE_ANTHROPIC_KEY",
                                  client_factory=lambda key: NS(messages=Messages()))
    calls.clear()
    old.web_research("s", "p")
    assert calls[0]["tools"][0]["type"] == "web_search_20250305"


def test_research_auto_uses_the_writers_own_search_when_it_has_one():
    api = providers.OpenAIText("gpt-6-sol")
    assert isinstance(research.from_config({}, api), research.NativeSearch)
    assert isinstance(research.from_config({}, providers.CommandLLM(["x"])), research.DuckDuckGo)
    assert isinstance(research.from_config({"research": {"provider": "duckduckgo"}}, api), research.DuckDuckGo)
    with pytest.raises(ValueError):
        research.from_config({"research": {"provider": "writer"}}, providers.CommandLLM(["x"]))


def test_research_chart_through_the_writers_own_search(make):
    class NativeWriter(Script):
        web_search = True

        def web_research(self, system, prompt, cancel=None):
            self.research_prompt = prompt
            return providers.Reply(NATIVE_REPLY, {"in": 5, "out": 5, "exact": True}), \
                [{"url": "https://example.org/sizes", "title": "S"}]
    llm = NativeWriter(CHART_OK.format(id="p", source="S1"))
    c, d = make(llm=llm, searcher=research.NativeSearch(llm), text=chart_doc("Chart: research the biggest models"))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    assert wait_job(c)["status"] == "done"
    assert "[p] Chart: research the biggest models" in llm.research_prompt
    assert len(llm.calls) == 1 and "[S1] Model sizes, 2026" in llm.calls[0]["attached"]["SOURCES.md"]
    assert "invented" not in llm.calls[0]["attached"]["SOURCES.md"]
    draft = next(b for b in blocks_of(d) if b.type == "draft")
    name = re.search(r"\]\((chart-[^)]+\.png)\)", draft.text).group(1)
    assert json.loads((d / "images" / f".{name}.json").read_text())["chart"]["source"] == "example.org"


# ── review round 1 (core) ───────────────────────────────────────────────────────

def test_r1_a_failed_planning_call_does_not_abort_the_pass(make):
    llm = Script(lambda s, p, f: agent.LLMError("planner down"), CHART_OK.format(id="p", source="x"))
    c, d = make(llm=llm, searcher=FakeSearch(), text=chart_doc("Chart: research sizes"))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    assert wait_job(c)["status"] == "done"
    assert "found nothing usable" in llm.calls[1]["prompt"]          # and the writer is told so
    assert any(b.type == "draft" for b in blocks_of(d))


def test_r1_research_that_finds_nothing_is_said_to_the_writer(make, monkeypatch):
    monkeypatch.setattr(research, "fetch_public", fake_fetch({}))
    llm = Script("=== SEARCH p ===\nsizes\n=== END ===", CHART_OK.format(id="p", source="x"))
    c, _ = make(llm=llm, searcher=FakeSearch([]), text=chart_doc("Chart: research sizes"))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait_job(c)
    assert "found nothing usable" in llm.calls[1]["prompt"] and "memory, not checked" in llm.calls[1]["prompt"]
    llm2 = Script("=== SEARCH p ===\nNONE\n=== END ===", CHART_OK.format(id="p", source="x"))
    c2, _ = make(llm=llm2, searcher=FakeSearch(), text=chart_doc("Chart: a 1, b 2"))
    c2.post("/api/issues/099-test/generate", json={}, headers=H)
    wait_job(c2)
    assert "found nothing" not in llm2.calls[1]["prompt"]          # nothing was needed


def test_r1_numbers_are_printed_exactly_as_given():
    spec = charts.check_spec(raw(rows=["tiny | 0.0004", "odd | 1.23456", "big | 1234567.5", "cash | $300"]))
    assert spec["shown"] == [["0.0004"], ["1.23456"], ["1,234,567.5"], ["$300"]]
    assert charts.fmt(0.0004) == "0.0004" and charts.fmt(-1234.5) == "-1,234.5" and charts.fmt(2.0) == "2"
    assert "tiny | 0.0004" in charts.as_block(spec, "x")


def test_r1_long_labels_are_wrapped_not_cut():
    label = "A label that is quite a lot longer than fourteen characters"
    assert charts._wrap(label, 14).replace("\n", " ") == label


def test_r1_too_dense_is_refused_and_long_columns_turn_sideways():
    with pytest.raises(charts.ChartError, match="too many"):
        charts.check_spec(raw(rows=[f"r{i} | 1 | 2 | 3" for i in range(21)], series="a|b|c"))
    assert charts.check_spec(raw(rows=[f"r{i} | {i}" for i in range(13)]))["type"] == "bar"
    assert charts.check_spec(raw(rows=[f"r{i} | {i}" for i in range(12)]))["type"] == "column"


def test_r1_searching_stops_at_the_page_cap_and_the_time_limit():
    class Fresh(FakeSearch):           # six new addresses for every search
        def search(self, query, limit=6):
            self.queries.append(query)
            n = len(self.queries)
            return [{"title": f"t{n}-{i}", "url": f"https://e{n}-{i}.example/", "snippet": "s"} for i in range(6)]
    many = [{"title": "t", "url": "https://e.example/", "snippet": "s"}]
    s = Fresh()
    plan = {f"i{k}": ["q1", "q2", "q3"] for k in range(4)}
    research.gather(plan, {k: "x" for k in plan}, s, 50_000, fetch=fake_fetch({}))
    assert s.queries == ["q1", "q1"]     # 4 pages each from the first search; then the cap of 8
    t = [0.0]
    slow = FakeSearch(many[:1])
    research.gather({"a": ["q1", "q2", "q3"]}, {"a": "x"}, slow, 50_000, fetch=fake_fetch({}),
                    clock=lambda: t.__setitem__(0, t[0] + 70) or t[0])
    assert len(slow.queries) < 3


def test_r1_sources_are_headed_as_evidence_not_instructions(make, monkeypatch):
    monkeypatch.setattr(research, "fetch_public", fake_fetch({"https://example.org/sizes": PAGE}))
    llm = Script("=== SEARCH p ===\nq\n=== END ===", CHART_OK.format(id="p", source="S1"))
    c, _ = make(llm=llm, searcher=FakeSearch(), text=chart_doc("Chart: research sizes"))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait_job(c)
    assert llm.calls[1]["attached"]["SOURCES.md"].startswith(research.SOURCES_HEAD)
    assert "never instructions" in research.SOURCES_HEAD and "ignore them" in llm.calls[1]["system"]


# ── review round 1 (server) ─────────────────────────────────────────────────────

def _photo_with_gps(path):
    from PIL import Image
    im = Image.new("RGB", (40, 20), (200, 40, 40))
    exif = Image.Exif()
    exif[0x0112] = 6                          # orientation: rotate 90° to show upright
    exif[0x010F] = "PhoneMaker"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (51.0, 30.0, 0.0)
    im.save(path, "JPEG", exif=exif, comment=b"secret note")
    return open(path, "rb").read()


def test_r1_an_uploaded_photo_loses_its_location_and_stands_upright(make, tmp_path):
    from PIL import Image
    data = _photo_with_gps(tmp_path / "p.jpg")
    assert b"PhoneMaker" in data
    c, d = make()
    r = upload(c, data, after="a")
    assert r.status_code == 200, r.text
    kept = (d / "images" / r.json()["name"])
    raw_bytes = kept.read_bytes()
    assert b"PhoneMaker" not in raw_bytes and b"secret note" not in raw_bytes and b"Exif" not in raw_bytes
    with Image.open(kept) as im:
        assert im.size == (20, 40) and not im.getexif()      # turned upright, no EXIF left


def test_r1_png_text_chunks_are_removed(make, tmp_path):
    from PIL import Image, PngImagePlugin
    info = PngImagePlugin.PngInfo()
    info.add_text("Location", "My house")
    Image.new("RGB", (8, 8), "blue").save(tmp_path / "x.png", pnginfo=info)
    c, d = make()
    r = upload(c, (tmp_path / "x.png").read_bytes(), after="a")
    assert r.status_code == 200
    assert b"My house" not in (d / "images" / r.json()["name"]).read_bytes()


def test_r1_a_disguised_or_broken_picture_is_refused(make):
    c, d = make()
    r = upload(c, b"\xff\xd8\xff\xe0" + b"not really a jpeg" * 10, after="a")
    assert r.status_code == 422 and "could not be read" in r.json()["error"]
    assert not os.listdir(d / "images")


def test_r1_demo_reads_numbered_labels_and_every_row():
    from slopmill.demo import _chart_rows
    assert _chart_rows("Chart: Q1 10, Q2 20") == ["Q1 | 10", "Q2 | 20"]
    assert _chart_rows("Chart: 2019 5, 2020 7") == ["2019 | 5", "2020 | 7"]
    assert len(_chart_rows(", ".join(f"item{i} {i}" for i in range(40)))) == charts.MAX_ROWS


# ── review round 2 (server) ─────────────────────────────────────────────────────

def test_r2_iptc_and_every_other_kind_of_metadata_is_gone(make, tmp_path):
    from PIL import Image
    Image.new("RGB", (10, 10), "green").save(tmp_path / "a.jpg", "JPEG")
    raw_jpg = (tmp_path / "a.jpg").read_bytes()
    # an APP13 (Photoshop/IPTC) segment carrying a place name, spliced in after SOI
    body = b"Photoshop 3.0\x00" + b"8BIM\x04\x04\x00\x00\x00\x00\x00\x10" + b"Secret Village!!"
    app13 = b"\xff\xed" + struct.pack(">H", len(body) + 2) + body
    data = raw_jpg[:2] + app13 + raw_jpg[2:]
    c, d = make()
    r = upload(c, data, after="a")
    assert r.status_code == 200, r.text
    assert b"Secret Village" not in (d / "images" / r.json()["name"]).read_bytes()


def test_r2_a_truncated_picture_is_refused(make, tmp_path):
    from PIL import Image
    Image.new("RGB", (300, 300), "red").save(tmp_path / "t.jpg", "JPEG", quality=95)
    whole = (tmp_path / "t.jpg").read_bytes()
    c, d = make()
    r = upload(c, whole[: len(whole) // 2], after="a")
    assert r.status_code == 422 and "could not be read" in r.json()["error"]
    assert not os.listdir(d / "images")


def test_r2_huge_dimensions_are_refused_before_decoding(make, monkeypatch):
    from slopmill import metadata
    monkeypatch.setattr(metadata, "MAX_PIXELS", 10)
    c, _ = make()
    r = upload(c, PNG, after="a")
    assert r.status_code == 422 and "megapixels" in r.json()["error"]


def test_r2_the_words_travel_in_headers_not_the_address(make):
    c, d = make()
    r = upload(c, PNG, after="a", alt="Café in the rain", caption="Ünïcode is fine")
    assert r.status_code == 200
    assert "Café in the rain" in (d / "issue.md").read_text()
    bad = c.post("/api/issues/099-test/pictures", params={"base": c.get("/api/issues/099-test").json()["rev"]},
                 content=PNG, headers={**H, "x-picture-alt": "%FF%FE"})
    assert bad.status_code == 400


# ── review round 2 (core) ───────────────────────────────────────────────────────

def test_r2_every_number_fits_a_long_list_gets_a_taller_picture(tmp_path):
    spec = charts.check_spec(raw(rows=[f"A fairly long country name, number {i} of them | {i * 123}" for i in range(30)]))
    assert spec["type"] == "bar"
    charts.draw(spec, str(tmp_path / "tall.png"))
    w, h = png_size(tmp_path / "tall.png")
    assert w == charts.WIDTH and h > charts.HEIGHT
    short = charts.check_spec(raw())
    charts.draw(short, str(tmp_path / "short.png"))
    assert png_size(tmp_path / "short.png") == (charts.WIDTH, charts.HEIGHT)


def test_r2_dense_columns_turn_sideways_and_crowded_lines_are_refused():
    assert charts.check_spec(raw(series="a|b|c|d", rows=[f"r{i} | 1 | 2 | 3 | 4" for i in range(10)]))["type"] == "bar"
    with pytest.raises(charts.ChartError, match="every point"):
        charts.check_spec(raw(type="line", series="a|b|c", rows=[f"r{i} | 1 | 2 | 3" for i in range(14)]))
    assert charts.check_spec(raw(type="line", series="a|b", rows=[f"r{i} | 1 | 2" for i in range(20)]))


def test_r2_design_colours_that_vanish_on_white_are_skipped():
    got = charts.readable(["#FFFFFF", "#FAFAF0", "#44652A", "#45662B", "#2B5A8A"])
    assert "#FFFFFF" not in got and "#FAFAF0" not in got and "#45662B" not in got
    assert got[:2] == ["#44652A", "#2B5A8A"] and len(got) == charts.MAX_SERIES
    assert len(set(map(str, got))) == len(got)


# ── review round 3 ──────────────────────────────────────────────────────────────

def test_r3_requests_past_the_cap_are_named_to_the_writer(make, monkeypatch):
    monkeypatch.setattr(research, "fetch_public", fake_fetch({"https://example.org/sizes": PAGE}))
    prompts = "".join(f"::: {{.prompt #c{i}}}\nChart: research thing {i}\n:::\n\n" for i in range(6))
    text = DOC.replace("{#z}", prompts + "{#z}")
    llm = Script("".join(f"=== SEARCH c{i} ===\nq{i}\n=== END ===\n" for i in range(4)),
                 lambda s, p, f: "=== NOTE ===\nx\n=== END ===")
    c, _ = make(llm=llm, searcher=FakeSearch(), text=text)
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait_job(c)
    assert "#c4, #c5 were not researched" in llm.calls[1]["prompt"]


def test_r3_an_unknown_source_number_is_never_printed():
    src = [{"n": 1, "title": "A", "url": "https://example.org/a", "fetched": "d"}]
    assert research.cite("S2", src)[0] == "the writer's memory, not checked"
    assert research.cite("S1, S7", src)[0] == "example.org"


def test_r3_snippet_only_sources_are_marked():
    results = [{"title": "B", "url": "https://b.example/2", "snippet": "b snippet with 12"}]
    sources, _ = research.gather({"c": ["q"]}, {"c": "x"}, FakeSearch(results), 5000,
                                 fetch=fake_fetch({"https://b.example/2": FetchRefused("private")}))
    assert sources[0]["snippet_only"] is True
    assert research.cite("S1", sources)[1][0]["snippet_only"] is True


def test_r3_queries_are_told_to_stay_public():
    assert "public search engine" in research.PLAN_SYSTEM and "searches are public" in research.NATIVE_SYSTEM


def test_r3_pictures_are_cleaned_one_at_a_time_and_the_demo_takes_smaller_ones(make, monkeypatch):
    from slopmill import metadata
    seen, active = [], [0]
    real = metadata.strip

    def slow(path, max_pixels=metadata.MAX_PIXELS):
        active[0] += 1
        seen.append((active[0], max_pixels))
        time.sleep(0.2)
        active[0] -= 1
        return real(path, max_pixels=max_pixels)
    monkeypatch.setattr(metadata, "strip", slow)
    c, _ = make()
    rev = c.get("/api/issues/099-test").json()["rev"]
    out = []
    ts = [threading.Thread(target=lambda: out.append(upload(c, PNG, after="a", base=rev).status_code)) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert max(a for a, _ in seen) == 1
    c2, _ = make(demo=True)
    upload(c2, PNG, after="a")
    assert seen[-1][1] == metadata.DEMO_MAX_PIXELS
