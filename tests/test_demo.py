# SPDX-License-Identifier: MIT
"""The public demo: a private copy per visitor, prepared writing, nothing that costs or
reaches out. Driven in-process; no model, no network."""
import asyncio
import json
import os
import shutil
import time

import httpx
import pytest

from conftest import HERE, ROOT
from slopmill import demo

TEMPLATE = os.path.join(ROOT, "packs", "storybook")


@pytest.fixture
def site(tmp_path):
    tpl = tmp_path / "template"
    d = tpl / "issues" / "001-welcome"
    d.mkdir(parents=True)
    (d / "images").mkdir()
    text = open(os.path.join(TEMPLATE, "welcome.md")).read().replace("trying the buttons", "trying teh buttons")
    (d / "issue.md").write_text(text)
    (d / "settings.json").write_text(json.dumps({"design": "storybook", "voice": "storybook"}))
    pic = tmp_path / "picture.jpg"
    pic.write_bytes(bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9"))
    www = tmp_path / "www"
    www.mkdir()
    (www / "index.html").write_text("<h1>intro</h1>")

    def go(**kw):
        opts = dict(prepared={"b-w1p1": "Prepared words for the first prompt."}, delay=0.05)
        opts.update(kw)
        return demo.build(str(tpl), str(pic), str(tmp_path / "root"), site_dir=str(www), **opts)
    return go, tmp_path


CF_EDGE = "162.158.1.1"          # inside Cloudflare's published ranges


def client(app, host="demo.example.org", ip="1.2.3.4", via=CF_EDGE):
    """A visitor at `ip`, arriving through Cloudflare (`via`) and the proxy in front, which
    sets X-Forwarded-For to the address that connected to it."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{host}",
                             headers={"cf-connecting-ip": ip, "x-forwarded-for": via},
                             follow_redirects=True)


def run(coro):
    return asyncio.run(coro)


async def start(c):
    r = await c.get("/")
    assert r.status_code == 200 and "slopmill" in r.text
    return r


async def wait(c, slug="001-welcome"):
    for _ in range(200):
        j = (await c.get(f"/api/issues/{slug}")).json()["job"]
        if j and j["status"] != "running":
            return j
        await asyncio.sleep(0.05)
    raise AssertionError("pass did not finish")


H = {"x-slopmill": "1", "content-type": "application/json"}


def test_the_intro_pages_are_served_on_the_main_host(site):
    build, _ = site
    app = build()

    async def go():
        async with client(app, host="slopmill.example") as c:
            r = await c.get("/")
            assert r.status_code == 200 and "<h1>intro</h1>" in r.text
            assert (await c.get("/nope.png")).status_code == 404
    run(go())


def test_each_visitor_gets_their_own_prefilled_copy(site):
    build, tmp = site
    app = build()

    async def go():
        async with client(app, ip="1.1.1.1") as a, client(app, ip="2.2.2.2") as b:
            await start(a)
            await start(b)
            st = (await a.get("/api/state")).json()
            assert st["demo"] is True and [i["slug"] for i in st["issues"]] == ["001-welcome"]
            assert st["providers"]["writer"]["provider"] == "demo"
            r = await a.post("/api/issues/001-welcome/generate", json={}, headers=H)
            assert r.status_code == 200
            assert (await wait(a))["status"] == "done"
            drafts = [x for x in (await a.get("/api/issues/001-welcome")).json()["blocks"] if x["type"] == "draft"]
            assert any(d["text"] == "Prepared words for the first prompt." for d in drafts)
            assert any("img-" in d["text"] for d in drafts)          # the prepared picture
            other = (await b.get("/api/issues/001-welcome")).json()["blocks"]
            assert not any(x["type"] == "draft" for x in other)     # b's copy is untouched
    run(go())
    assert len(os.listdir(tmp / "root")) == 2


def test_the_spelling_check_finds_the_prepared_typo(site):
    build, _ = site
    app = build()

    async def go():
        async with client(app) as c:
            await start(c)
            assert (await c.post("/api/issues/001-welcome/proof", json={}, headers=H)).status_code == 200
            await wait(c)
            fixes = (await c.get("/api/issues/001-welcome")).json()["review"]["proof"]["fixes"]
            assert [f["after"].strip()[-11:] for f in fixes] and "the" in fixes[0]["after"]
    run(go())


def test_nothing_costly_or_outward_is_allowed(site):
    build, _ = site
    app = build()

    async def go():
        async with client(app) as c:
            await start(c)
            r = await c.post("/api/designs", json={"text": "x"}, headers=H)
            assert r.status_code == 403 and "demo" in r.json()["error"]
            assert (await c.delete("/api/designs/starter", headers=H)).status_code == 403
            r = await c.get("/api/issues/001-welcome/asset/not-here.jpg")
            assert r.status_code == 404                              # never fetched from elsewhere
            for n in range(20):
                r = await c.post("/api/issues", json={"title": f"t{n}"}, headers=H)
                if r.status_code != 200:
                    break
            assert r.status_code == 422 and "at most" in r.json()["error"]
    run(go())


def test_no_session_means_a_clear_answer_not_a_new_copy(site):
    build, tmp = site
    app = build()

    async def go():
        async with client(app) as c:
            r = await c.get("/api/state")
            assert r.status_code == 401 and "reload" in r.json()["error"]
            r = await c.post("/api/issues", json={}, headers=H)
            assert r.status_code in (401, 404)
    run(go())
    assert not os.path.exists(tmp / "root") or os.listdir(tmp / "root") == []


def test_one_address_cannot_make_endless_copies(site):
    build, tmp = site
    app = build(per_ip=3)

    async def go():
        for n in range(3):
            async with client(app, ip="9.9.9.9") as c:
                await start(c)
        async with client(app, ip="9.9.9.9") as c:
            r = await c.get("/")
            assert r.status_code == 429 and "busy" in r.text
        async with client(app, ip="8.8.8.8") as c:
            await start(c)
    run(go())
    assert len(os.listdir(tmp / "root")) == 4


def test_old_and_excess_sessions_are_removed(site):
    build, tmp = site
    app = build(limit=2, idle=1)

    async def go():
        for ip in ("1.0.0.1", "1.0.0.2", "1.0.0.3"):
            async with client(app, ip=ip) as c:
                await start(c)
    run(go())
    assert len(os.listdir(tmp / "root")) == 2                         # the least recently used went
    time.sleep(1.2)
    app.sessions.sweep()
    assert os.listdir(tmp / "root") == []


def test_the_stand_in_writer_says_it_is_one():
    w = demo.DemoWriter(delay=0)
    reply = str(w("You write.", "Write these blocks: #b-x1 and #b-x2.", []))
    assert "=== BLOCK b-x1 ===" in reply and "=== BLOCK b-x2 ===" in reply
    assert "stand-in" in reply or "demo" in reply
    assert "no AI model was called" in reply


def test_a_faked_cloudflare_header_from_elsewhere_does_not_dodge_the_limit(site):
    build, tmp = site
    app = build(per_ip=2)

    async def go():
        for n in range(2):
            async with client(app, ip=f"198.51.100.{n}", via="203.0.113.7") as c:   # straight to the origin
                await start(c)
        async with client(app, ip="198.51.100.99", via="203.0.113.7") as c:
            assert (await c.get("/")).status_code == 429
    run(go())


def test_a_changed_prompt_gets_a_stand_in_not_the_old_answer(tmp_path):
    doc = tmp_path / "DOCUMENT.md"
    w = demo.DemoWriter({"b-a": "The old answer."}, delay=0, prompts={"b-a": "Write about bees."})
    doc.write_text("[PROMPT #b-a]\nWrite about bees.\n")
    assert "The old answer." in str(w("s", "Write these blocks: #b-a.", [str(doc)]))
    doc.write_text("[PROMPT #b-a]\nWrite about the sea instead.\n")
    assert "The old answer." not in str(w("s", "Write these blocks: #b-a.", [str(doc)]))


def test_a_full_copy_refuses_more_writes(site):
    build, tmp = site
    app = build(budget=1)                                   # everything is over one byte

    async def go():
        async with client(app) as c:
            await start(c)
            r = await c.post("/api/issues", json={"title": "x"}, headers=H)
            assert r.status_code == 413 and "full" in r.json()["error"]
            assert (await c.get("/api/state")).status_code == 200      # reading still works
    run(go())


def test_the_old_prompt_elsewhere_in_the_issue_does_not_count(tmp_path):
    doc = tmp_path / "DOCUMENT.md"
    w = demo.DemoWriter({"b-a": "The old answer."}, delay=0, prompts={"b-a": "Write about bees."})
    doc.write_text("[PROMPT #b-a]\nWrite about rain.\n\n[PROMPT #b-b]\nWrite about bees.\n")
    assert "The old answer." not in str(w("s", "Write these blocks: #b-a.", [str(doc)]))
