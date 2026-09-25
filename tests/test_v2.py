# SPDX-License-Identifier: MIT
"""SPEC-V2: pictures, voice packs, design files, the meter and the providers.
No real model, image model or network is ever reached."""
import base64
import json
import os
import re
import sys
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURE, FIXTURE_PACK, FRONT, HERE, ROOT, needs_022
from slopmill import agent, designs, doc, providers, render, source as src
from slopmill.pack import design_from_text, export_design, load_pack
from slopmill.server import app as app_mod
from slopmill.server.app import create_app
from slopmill.errors import EnvironmentProblem
from slopmill.voices import VoiceError, VoiceLibrary, clean_filename

H = {"x-slopmill": "1"}
JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")

DOC = FRONT + """{#a}
This week has found me on the road a lot more than usual.

::: {.prompt #p}
Talk about the voice file revisions. About 60 words.
:::

::: {.prompt #pic}
Image: a backyard at night with birds on a branch
:::

{#z}
Thank you readers for sticking with us.
"""


class Fake:
    """A writer that answers text prompts with text and picture prompts with IMAGE blocks."""

    def __init__(self, image=None, text=None, usage=None, delay=0.0, max_request_bytes=115_000):
        self.image = image or {"description": "A quiet backyard at night, birds on a branch",
                               "alt": "Three small birds on a branch under a porch light",
                               "caption": "The birds were **also** confused."}
        self.text = text or {}
        self.usage = usage
        self.delay = delay
        self.calls = []
        self.label = "fake-writer"
        self.max_request_bytes = max_request_bytes

    def __call__(self, system, prompt, files, cancel=None):
        self.calls.append({"system": system, "prompt": prompt, "files": files})
        end = time.time() + self.delay
        while time.time() < end:
            if cancel is not None and cancel.is_set():
                raise agent.Cancelled()
            time.sleep(0.02)
        ids = list(dict.fromkeys(re.findall(r"#([A-Za-z][\w-]*)", prompt.split("\n\nGeneral")[0])))
        out = []
        for i in ids:
            if i in self.text:
                out.append(f"=== BLOCK {i} ===\n{self.text[i]}\n=== END ===")
            elif i.startswith("pic") or i in self.image.get("_ids", []):
                img = self.image
                out.append(f"=== IMAGE {i} ===\nDESCRIPTION: {img['description']}\nALT: {img['alt']}\n"
                           f"CAPTION: {img['caption']}\n=== END ===")
            else:
                out.append(f"=== BLOCK {i} ===\nWritten for {i}. **A phrase**{{.green}} here.\n=== END ===")
        out.append("=== NOTE ===\nDone.\n=== END ===")
        return providers.Reply("\n".join(out), self.usage)


class FakeImages:
    label = "fake-images"

    def __init__(self, delay=0.0, fail=None, data=JPEG):
        self.delay, self.fail, self.data, self.calls = delay, fail, data, []

    def __call__(self, prompt, out_path, cancel=None):
        self.calls.append(prompt)
        end = time.time() + self.delay
        while time.time() < end:
            if cancel is not None and cancel.is_set():
                raise agent.Cancelled()
            time.sleep(0.02)
        if self.fail:
            raise agent.LLMError(self.fail)
        with open(out_path, "wb") as f:
            f.write(self.data)
        return out_path

    def describe(self):
        return {"provider": "fake", "label": self.label}


@pytest.fixture
def make(tmp_path):
    def go(llm=None, images=None, text=DOC, lint=None, plan=None):
        ws = tmp_path / "ws"
        d = ws / "issues" / "099-test"
        d.mkdir(parents=True, exist_ok=True)
        (d / "issue.md").write_text(text)
        app = create_app(workspace=str(ws), pack=load_pack(FIXTURE_PACK), llm=llm or Fake(),
                         model_label="fake", lint_argv=lint, token="tok", images=images, plan=plan)
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
    raise AssertionError("pass did not finish")


def blocks_of(d):
    return doc.parse((d / "issue.md").read_text())[1]


# ── pictures (1-5) ──────────────────────────────────────────────────────────────

def test_1_picture_prompt_draws_a_figure(make):
    imgs = FakeImages()
    c, d = make(images=imgs)
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    assert wait(c)["status"] == "done"
    drafts = {b.attrs["for"]: b for b in blocks_of(d) if b.type == "draft"}
    fig = drafts["pic"].text
    assert fig.startswith("::: {.figure}") and "![Three small birds" in fig
    name = re.search(r"\]\((img-pic-[^)]+\.jpg)\)", fig).group(1)
    assert (d / "images" / name).read_bytes() == JPEG
    side = json.loads((d / "images" / f".{name}.json").read_text())
    assert side["description"].startswith("A quiet backyard") and side["model"] == "fake-images"
    assert imgs.calls == [side["description"]]
    assert "Written for p" in drafts["p"].text            # the text prompt was written too
    prev = c.get("/api/issues/099-test/preview").json()
    assert f"/api/issues/099-test/asset/{name}" in prev["html"] and "<img" in prev["html"]
    state = c.get("/api/issues/099-test").json()
    assert any(b["attrs"].get("picture") == "1" for b in state["blocks"])


def test_1_picture_rules_only_when_images_are_configured(make):
    llm = Fake()
    c, d = make(llm=llm, images=FakeImages())
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    assert "=== IMAGE <id> ===" in llm.calls[0]["system"]
    assert "PICTURE prompts" in llm.calls[0]["prompt"]


def test_2_no_image_provider_fails_the_picture_but_writes_text(make):
    llm = Fake()
    c, d = make(llm=llm, images=None)
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    assert wait(c)["status"] == "done"
    drafts = {b.attrs["for"]: b for b in blocks_of(d) if b.type == "draft"}
    assert "pic" not in drafts and "p" in drafts
    assert "#pic" not in llm.calls[0]["prompt"]           # never asked for
    assert "IMAGE <id>" not in llm.calls[0]["system"]
    job = c.get("/api/issues/099-test").json()["job"]
    assert "no image generator" in job["blocks"]["pic"]["detail"]


def test_3_a_failed_picture_fails_only_itself(make):
    c, d = make(images=FakeImages(fail="moderation: refused"))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    drafts = {b.attrs["for"]: b for b in blocks_of(d) if b.type == "draft"}
    assert "pic" not in drafts and "p" in drafts
    job = c.get("/api/issues/099-test").json()["job"]
    assert "refused" in job["blocks"]["pic"]["detail"]
    assert not [f for f in os.listdir(d / "images")]       # nothing left behind


def test_3_not_an_image_is_refused(make):
    c, d = make(images=FakeImages(data=b"<html>not a picture</html>"))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    job = c.get("/api/issues/099-test").json()["job"]
    assert "not a JPEG" in job["blocks"]["pic"]["detail"]
    assert not os.listdir(d / "images")


def test_3_stop_while_drawing_writes_nothing(make):
    c, d = make(images=FakeImages(delay=5))
    before = (d / "issue.md").read_text()
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    end = time.time() + 5
    while time.time() < end:
        job = c.get("/api/issues/099-test").json()["job"]
        if job and job["blocks"].get("pic", {}).get("status") == "drawing":
            break
        time.sleep(0.05)
    assert c.post("/api/issues/099-test/stop", json={}, headers=H).json()["stopped"]
    assert wait(c)["status"] == "stopped"
    assert (d / "issue.md").read_text() == before
    assert not os.listdir(d / "images")


def test_4_regenerate_draws_a_new_file_and_keeps_the_old(make):
    c, d = make(images=FakeImages())
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    first = sorted(f for f in os.listdir(d / "images") if not f.startswith("."))
    time.sleep(1.1)
    c.post("/api/issues/099-test/generate", json={"targets": ["pic"]}, headers=H)
    wait(c)
    second = sorted(f for f in os.listdir(d / "images") if not f.startswith("."))
    assert len(first) == 1 and len(second) == 2 and first[0] in second
    draft = next(b for b in blocks_of(d) if b.type == "draft" and b.attrs["for"] == "pic")
    assert first[0] not in draft.text


def test_4_a_comment_can_redraw_and_the_model_sees_the_description(make):
    llm = Fake()
    c, d = make(llm=llm, images=FakeImages())
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    draft = next(b for b in blocks_of(d) if b.type == "draft" and b.attrs["for"] == "pic")
    llm.image = {**llm.image, "description": "The same yard at dawn", "_ids": [draft.id]}
    c.post("/api/issues/099-test/comments", json={"block": draft.id, "note": "make it dawn"}, headers=H)
    c.post("/api/issues/099-test/revise", json={}, headers=H)
    assert wait(c)["status"] == "done"
    doc_sent = open(llm.calls[-1]["files"][-1]).read()
    assert f"[DRAFT #{draft.id} for #pic · PICTURE]" in doc_sent
    assert "drawn from this description: A quiet backyard" in doc_sent
    new = next(b for b in blocks_of(d) if b.id == draft.id)
    assert new.text != draft.text and new.type == "draft" and new.attrs["for"] == "pic"


def test_5_alt_and_caption_cannot_break_the_document(make):
    nasty = {"description": "x", "alt": "a [link](x) *star* ::: {#id} <b> ![img](y) \\ `c`",
             "caption": "fine caption"}
    c, d = make(llm=Fake(image=nasty), images=FakeImages())
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    draft = next(b for b in blocks_of(d) if b.type == "draft" and b.attrs["for"] == "pic")
    res = render.compile_text((d / "issue.md").read_text(), load_pack(FIXTURE_PACK), mode="preview")
    assert not [p for p in res.problems if not str(p).startswith("front matter")]
    alt = re.search(r'alt="([^"]*)"', res.body).group(1)
    assert "[link](x)" in alt and "<b>" not in alt and "&lt;b&gt;" in alt


def test_5_caption_that_breaks_out_is_refused(make):
    bad = {"description": "x", "alt": "ok", "caption": "text\n:::\n\n::: {.prompt #evil}\nx"}
    c, d = make(llm=Fake(image=bad), images=FakeImages())
    before = (d / "issue.md").read_text()
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    assert "evil" not in (d / "issue.md").read_text()
    job = c.get("/api/issues/099-test").json()["job"]
    assert job["blocks"]["pic"]["status"] == "failed"
    assert os.listdir(d / "images") == []               # the drawn file was not kept


def test_5_text_reply_cannot_point_at_a_new_image(make):
    llm = Fake(text={"p": "::: {.figure}\n![x](secret.jpg)\n:::"})
    c, d = make(llm=llm, images=FakeImages())
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    assert "secret.jpg" not in (d / "issue.md").read_text()


def test_5_design_without_a_figure_fails_the_picture(tmp_path):
    minimal = load_pack(os.path.join(HERE, "packs", "minimal"))
    cfg = dict(minimal.config)
    cfg["components"] = {}
    from slopmill.pack import Pack
    nofig = Pack(cfg, minimal.templates, origin="nofig")
    _, blocks = doc.parse("{#a}\nHi.\n\n::: {.prompt #pic}\nImage: a cat\n:::\n")
    out, note, failures = agent.generate(nofig, {}, blocks, ["pic"], Fake(), workdir=str(tmp_path),
                                         images=FakeImages(), images_dir=str(tmp_path / "img"))
    assert "no figure component" in failures["pic"][0]


# ── voice packs (6-10) ──────────────────────────────────────────────────────────

def test_6_seeded_byte_identical(make):
    c, d = make()
    v = c.app.state.voices.get("fixture")
    src_dir = os.path.join(FIXTURE_PACK, "voice")
    assert open(v.path("VOICE.md"), "rb").read() == open(os.path.join(src_dir, "VOICE.md"), "rb").read()
    assert open(v.path("001.md"), "rb").read() == open(os.path.join(src_dir, "specimens", "001.md"), "rb").read()
    roles = {f["path"]: f["role"] for f in v.files()}
    assert roles["writer.md"] == "brief" and roles["001.md"] == "sample" and roles["VOICE.md"] == "rules"


def test_6_issue_uses_its_own_voice(make):
    llm = Fake()
    c, d = make(llm=llm)
    c.post("/api/voices", json={"name": "plain"}, headers=H)
    c.post("/api/voices/plain/files", json={"files": [{"name": "mine.md", "text": "MY SAMPLE"}]}, headers=H)
    assert c.put("/api/issues/099-test/settings", json={"voice": "plain"}, headers=H).status_code == 200
    c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    wait(c)
    sent = [os.path.basename(f) for f in llm.calls[0]["files"]]
    assert sent == ["mine.md", "DOCUMENT.md"]
    assert llm.calls[0]["system"].startswith("You are ghost-writing part of a newsletter")


def test_7_upload_roles_order_and_confinement(tmp_path):
    lib = VoiceLibrary(str(tmp_path))
    v = lib.create("me")
    assert v.add("../../etc/My Sample.md", "one") == "My-Sample.md"
    for bad in ("x.py", "notes", "a/../../b.sh", "....md", ""):
        with pytest.raises(VoiceError):
            v.add(bad, "x")
    assert clean_filename(".hidden.md") == "hidden.md"      # made safe, not refused

    with pytest.raises(VoiceError, match="at most"):
        v.add("big.md", "x" * 60_001)
    with pytest.raises(VoiceError):
        v.add("bin.md", "a\x00b")
    v.add("rules.txt", "never say delve", role="rules")
    files = [f["path"] for f in v.files()]
    assert files == ["writer.md", "My-Sample.md", "rules.txt"]
    v.arrange([{"path": "rules.txt", "role": "rules", "on": True},
               {"path": "My-Sample.md", "role": "sample", "on": False},
               {"path": "writer.md", "role": "brief", "on": True}])
    system, attached = v.voice()
    assert system.startswith("You are") and [n for n, _ in attached] == ["rules.txt"]
    with pytest.raises(VoiceError, match="at most one brief"):
        v.arrange([{"path": "rules.txt", "role": "brief"}, {"path": "My-Sample.md", "role": "sample"},
                   {"path": "writer.md", "role": "brief"}])
    with pytest.raises(VoiceError, match="does not match"):
        v.arrange([{"path": "rules.txt", "role": "rules"}])
    secret = tmp_path / "secret.md"
    secret.write_text("SECRET")
    os.symlink(secret, os.path.join(v.dir, "link.md"))
    with pytest.raises(VoiceError):
        v.path("link.md")
    with pytest.raises(VoiceError):
        v.read("voice.json")


def test_7_api_upload_and_refusals(make):
    c, _ = make()
    c.post("/api/voices", json={"name": "me"}, headers=H)
    r = c.post("/api/voices/me/files", json={"files": [
        {"name": "a.md", "text": "A"}, {"name": "evil.sh", "text": "rm"}, {"name": "b.txt", "text": "B"}]},
        headers=H).json()
    assert r["added"] == ["a.md", "b.txt"] and len(r["refused"]) == 1
    assert c.post("/api/voices", json={"name": "../x"}, headers=H).status_code == 422
    assert c.get("/api/voices/nope").status_code == 404


def test_8_over_the_limit_is_refused_before_any_call(make):
    llm = Fake(max_request_bytes=6_000)          # the test voice alone is about 9KB
    c, _ = make(llm=llm)
    r = c.post("/api/issues/099-test/generate", json={}, headers=H)
    assert r.status_code == 422 and "takes at most 5KB" in r.json()["error"]
    assert llm.calls == []
    nr = c.get("/api/issues/099-test").json()["next_request"]
    assert nr["bytes"] > nr["limit"] == 6_000


def test_9_history_kept_on_replace_and_delete(tmp_path):
    v = VoiceLibrary(str(tmp_path)).create("me")
    v.add("s.md", "first")
    v.write("s.md", "second")
    v.delete("s.md")
    hist = sorted(os.listdir(os.path.join(v.dir, ".history")))
    texts = sorted(open(os.path.join(v.dir, ".history", h)).read() for h in hist)
    assert texts == ["first", "second"]


def test_10_guides_are_served(make):
    c, _ = make()
    for name in ("voice-packs", "design-files", "providers"):
        r = c.get(f"/api/docs/{name}")
        assert r.status_code == 200 and len(r.json()["text"]) > 500, name
    assert c.get("/api/docs/..%2FSPEC").status_code == 404


# ── design files (11-15) ────────────────────────────────────────────────────────

@needs_022
def test_11_exported_file_compiles_022_identically(tmp_path):
    folder = load_pack(FIXTURE_PACK)
    text = export_design(folder)
    one = design_from_text(text, trusted=False)
    s = src.read_source(os.path.join(FIXTURE, "issue.md"))
    a = render.compile_source(s, folder, check_assets=False)
    b = render.compile_source(s, one, check_assets=False)
    assert (a.body, a.site, a.email, a.meta_json) == (b.body, b.site, b.email, b.meta_json)
    assert "voice" not in text.split("[templates]")[0]
    assert one.page_css == folder.page_css and one.page_css


def test_11_download(make):
    c, _ = make()
    r = c.get("/api/designs/fixture/download")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert design_from_text(r.content).name == "fixture"


def _minimal_file(**edit):
    text = export_design(load_pack(os.path.join(HERE, "packs", "minimal")))
    text = text.replace('name = "minimal"', 'name = "mine"')
    for a, b in edit.items():
        text = text.replace(a, b)
    return text


def test_12_upload_validates_and_stores(make):
    c, _ = make()
    r = c.post("/api/designs", json={"text": _minimal_file()}, headers=H)
    assert r.status_code == 200, r.text
    assert {"name": "mine", "source": "uploaded", "default": False} in r.json()["designs"]
    assert c.put("/api/issues/099-test/settings", json={"design": "mine"}, headers=H).status_code == 200
    st = c.get("/api/issues/099-test").json()
    assert st["design"] == "mine" and st["lint"] is False


@pytest.mark.parametrize("edit,reason", [
    ({'format = "slopmill-design/1"': 'format = "x"'}, "format"),
    ({"[pack]\n": "[pack]\ncommand = \"rm -rf /\"\n"}, "does not take command"),
    ({'asset_base = "https://cdn.example.org/"': 'asset_base = "http://127.0.0.1/"'}, "https"),
    ({"[templates]\n": "[templates]\n\"../x.html\" = \"x\"\n"}, "name.html"),
    ({'name = "mine"': 'name = "starter"'}, "ships with slopmill"),
    ({"{{ content }}": "{{ content.__class__.__mro__ }}"}, "template failed"),
    ({"{{ content }}": "{{ nosuchvar }}"}, "template failed"),
])
def test_12_upload_refusals(make, edit, reason):
    c, _ = make()
    r = c.post("/api/designs", json={"text": _minimal_file(**edit)}, headers=H)
    assert r.status_code == 422 and reason in r.json()["error"], r.text
    assert not [f for f in os.listdir(c.app.state.designs.root) if f.endswith(".toml")]


def test_12_a_template_that_never_finishes_is_refused(make, monkeypatch):
    monkeypatch.setattr(designs, "CHECK_SECONDS", 3)
    c, _ = make()
    loop = ("{% for i in range(99999) %}{% for j in range(99999) %}{% for k in range(99999) %}"
            "{% endfor %}{% endfor %}{% endfor %}{{ content }}")
    t0 = time.time()
    r = c.post("/api/designs", json={"text": _minimal_file(**{"{{ content }}": loop})}, headers=H)
    assert r.status_code == 422 and "took over" in r.json()["error"]
    assert time.time() - t0 < 10


def test_12_image_proxy_refuses_private_addresses():
    for host in ("127.0.0.1", "192.168.0.10", "10.0.0.1", "169.254.169.254", "localhost"):
        with pytest.raises(app_mod.FetchRefused):
            app_mod.fetch_public(f"https://{host}/x.jpg")
    with pytest.raises(app_mod.FetchRefused, match="https"):
        app_mod.fetch_public("http://example.com/x.jpg")


def test_13_csp_forbids_inline_script(make):
    c, _ = make()
    csp = c.get("/").headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    html = c.get("/").text
    assert not re.search(r"<script>(?!\s*</script>)", html), "inline script in index.html"


def test_13_preview_carries_the_designs_page(make):
    c, _ = make()
    p = c.get("/api/issues/099-test/preview").json()
    assert p["design"] == "fixture" and ".issue-body" in p["page_css"] and "<style>" in p["site_head"]


def test_14_new_issue_gets_the_chosen_design(make):
    c, _ = make()
    c.post("/api/designs", json={"text": _minimal_file()}, headers=H)
    slug = c.post("/api/issues", json={"title": "Other", "design": "mine"}, headers=H).json()["slug"]
    st = c.get(f"/api/issues/{slug}").json()
    assert st["design"] == "mine" and set(st["meta"]) == {"title", "slug"}


def test_14_missing_design_falls_back_and_says_so(make):
    c, d = make()
    (d / "settings.json").write_text(json.dumps({"design": "gone"}))
    st = c.get("/api/issues/099-test").json()
    assert st["design"] == "fixture" and st["design_missing"] is True


def test_12_sample_issue_uses_every_component():
    pack = load_pack(FIXTURE_PACK)
    text = designs.sample_issue(pack)
    for name in pack.components:
        assert f"{{.{name}" in text
    res = render.compile_text(text, pack, mode="publish")
    assert res.body


# ── meter (18-20) ───────────────────────────────────────────────────────────────

def test_18_usage_estimated_for_a_command_and_exact_when_reported(make):
    c, d = make(llm=Fake(), images=FakeImages())
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    u = c.get("/api/usage", params={"slug": "099-test"}).json()
    assert u["issue"]["exact"] is False and u["issue"]["in"] > 1000 and u["issue"]["images"] == 1
    c2, _ = make(llm=Fake(usage={"in": 1234, "out": 56, "exact": True}))
    c2.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    wait(c2)
    rec = c2.get("/api/usage", params={"slug": "099-test"}).json()["recent"][0]
    assert rec["in"] == 1234 and rec["out"] == 56 and rec["exact"] is True


def test_19_plan_windows_in_the_background(make, tmp_path):
    fake = tmp_path / "plan.py"
    fake.write_text("import json,time; time.sleep(0.3); print('noise'); print(json.dumps({'usage': "
                    "{'providers': [{'provider': 'openai', 'plan': 'team', 'windows': ["
                    "{'label': '5h', 'usedPercent': 2, 'resetAt': 1790220484000}]}]}}))")
    plan = providers.CommandPlan([sys.executable, str(fake)])
    c, _ = make(plan=plan)
    t0 = time.time()
    first = c.get("/api/usage").json()["plan"]
    assert time.time() - t0 < 0.3 and first["windows"] is None      # never waits for it
    end = time.time() + 5
    while time.time() < end and not c.get("/api/usage").json()["plan"]["windows"]:
        time.sleep(0.1)
    w = c.get("/api/usage").json()["plan"]["windows"][0]
    assert w == {"label": "5h", "used": 2, "reset_at": 1790220484.0}


def test_19_plan_failure_is_unavailable_not_an_error(make):
    plan = providers.CommandPlan([sys.executable, "-c", "import sys; sys.exit(3)"])
    c, _ = make(plan=plan)
    c.get("/api/usage")
    end = time.time() + 5
    while time.time() < end and not c.get("/api/usage").json()["plan"]["error"]:
        time.sleep(0.1)
    r = c.get("/api/usage")
    assert r.status_code == 200 and r.json()["plan"]["error"]


# ── providers (21-24) ───────────────────────────────────────────────────────────

def test_21_openai_compatible_request_and_exact_usage(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_KEY", "sk-supersecretvalue123456")
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}],
                                         "usage": {"prompt_tokens": 11, "completion_tokens": 2}})
    f = tmp_path / "v.md"
    f.write_text("VOICE")
    w = providers.OpenAIText("m1", key_env="TEST_KEY", base_url="https://llm.example/v1",
                             transport=httpx.MockTransport(handler))
    r = w("SYS", "PROMPT", [str(f)])
    assert str(r) == "hello" and r.usage == {"in": 11, "out": 2, "exact": True}
    assert seen["url"] == "https://llm.example/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-supersecretvalue123456"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "SYS"}
    assert "=== ATTACHED FILE: v.md ===\nVOICE" in seen["body"]["messages"][1]["content"]
    assert "sk-supersecret" not in json.dumps(w.describe())


def test_21_key_never_in_an_error(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-supersecretvalue123456")

    def handler(req):
        return httpx.Response(401, json={"error": {"message": "bad key sk-supersecretvalue123456"}})
    w = providers.OpenAIText("m", key_env="TEST_KEY", transport=httpx.MockTransport(handler))
    with pytest.raises(providers.LLMError) as e:
        w("s", "p", [])
    assert "supersecret" not in str(e.value) and "401" in str(e.value)
    monkeypatch.delenv("TEST_KEY")
    with pytest.raises(providers.LLMError, match="TEST_KEY is not set"):
        w("s", "p", [])


def test_21_anthropic_through_the_sdk_shape(monkeypatch):
    monkeypatch.setenv("AK", "sk-ant-secretsecret")
    calls = {}

    class Msg:
        stop_reason = "end_turn"
        content = [type("B", (), {"type": "text", "text": "hi there"})()]
        usage = type("U", (), {"input_tokens": 100, "output_tokens": 7,
                               "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0})()

    class Stream:
        text_stream = iter(["hi", " there"])
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_final_message(self): return Msg()

    class Api:
        def stream(self, **kw):
            calls.update(kw)
            return Stream()

    client = type("C", (), {"messages": Api(), "beta": type("Beta", (), {"messages": Api()})()})()
    w = providers.AnthropicText(key_env="AK", client_factory=lambda key: client)
    r = w("SYS", "PROMPT", [])
    assert str(r) == "hi there" and r.usage == {"in": 105, "out": 7, "exact": True}
    assert calls["model"] == "claude-opus-5" and calls["system"] == "SYS"
    assert calls["betas"] == ["server-side-fallback-2026-07-01"]
    assert calls["extra_body"] == {"fallbacks": "default"}
    Msg.stop_reason = "refusal"
    with pytest.raises(providers.LLMError, match="declined"):
        providers.AnthropicText(key_env="AK", client_factory=lambda key: client)("s", "p", [])


def test_21_config_parsing():
    with pytest.raises(providers.ConfigError, match="setup"):     # no writer: set one up first
        providers.from_config({})
    w, i, p = providers.from_config({}, llm_cmd="my-writer")
    assert w.kind == "command" and i is None and p is None
    w, i, p = providers.from_config({"writer": {"provider": "openai", "model": "gpt-x"},
                                     "images": {"provider": "openai"},
                                     "plan": {"command": "openclaw status --usage --json"}})
    assert w.kind == "openai" and i.kind == "openai" and p.provider == "openai"
    for bad in ({"writer": {"provider": "nope"}}, {"writer": {"provider": "openai"}},
                {"images": {"provider": "command", "command": "draw --out x"}},
                {"writer": {"max_request_bytes": -1}}):
        with pytest.raises(providers.ConfigError):
            providers.from_config(bad)


def test_22_state_shows_providers_without_keys(make, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shouldneverappear0000")
    w = providers.OpenAIText("gpt-x")
    c, _ = make(llm=w, images=providers.OpenAIImages())
    st = c.get("/api/state").json()
    assert st["providers"]["writer"]["key_set"] is True and st["providers"]["writer"]["key_env"] == "OPENAI_API_KEY"
    assert "shouldneverappear" not in json.dumps(st)


def test_23_stop_ends_an_http_call(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    release = threading.Event()

    def handler(req):
        release.wait(10)
        return httpx.Response(200, json={"choices": [{"message": {"content": "late"}}]})
    w = providers.OpenAIText("m", key_env="TEST_KEY", transport=httpx.MockTransport(handler))
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()
    t0 = time.time()
    with pytest.raises(providers.Cancelled):
        w("s", "p", [], cancel=ev)
    assert time.time() - t0 < 2
    release.set()


def test_24_cli_flags_without_a_config(tmp_path):
    w, i, p = providers.from_config(providers.load_config(str(tmp_path / "none.toml")),
                                    llm_cmd="my-writer --thinking high", model_label="gpt-5.6-sol")
    assert w.argv == ["my-writer", "--thinking", "high"] and w.label == "gpt-5.6-sol" and i is None


def test_command_images_substitutes_single_arguments(tmp_path):
    out = tmp_path / "o.jpg"
    script = tmp_path / "draw.py"
    script.write_text("import sys; open(sys.argv[2],'wb').write(bytes.fromhex('ffd8ffe0')); "
                      "open(sys.argv[2]+'.prompt','w').write(sys.argv[1])")
    im = providers.CommandImages([sys.executable, str(script), "{prompt}", "{output}"])
    im("a prompt; rm -rf / $(whoami)", str(out))
    assert open(str(out) + ".prompt").read() == "a prompt; rm -rf / $(whoami)"


def test_3_command_that_changes_the_extension_is_followed(tmp_path):
    """openclaw writes x.jpg when asked for x.img and reports the real path; found on the
    first live run. The picture is kept and nothing else is left behind."""
    script = tmp_path / "draw.py"
    script.write_text(
        "import sys, os, json\n"
        "out = os.path.splitext(sys.argv[2])[0] + '.jpg'\n"
        "open(out, 'wb').write(bytes.fromhex('ffd8ffe000104a464946'))\n"
        "print(json.dumps({'outputs': [{'path': out}]}))\n")
    im = providers.CommandImages([sys.executable, str(script), "{prompt}", "{output}"], label="x")
    imgdir = tmp_path / "images"
    created = []
    md = agent._draw(im, load_pack(FIXTURE_PACK), "b-x", {"description": "d", "alt": "a", "caption": ""},
                     str(imgdir), None, lambda *a: None, lambda *a: None, lambda u: None, created)
    name = re.search(r"\((img-b-x-[^)]+\.jpg)\)", md).group(1)
    assert (imgdir / name).is_file()
    leftovers = [c for c in created if os.path.exists(c) and os.path.basename(c) not in (name, f".{name}.json")]
    assert all(os.path.getsize(c) == 0 for c in leftovers)   # only the empty placeholder, removed by the pass


def test_3_image_command_cannot_point_elsewhere(tmp_path):
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(bytes.fromhex("ffd8ffe000104a464946"))
    script = tmp_path / "draw.py"
    script.write_text("import json; print(json.dumps({'outputs': [{'path': %r}]}))\n" % str(secret))
    im = providers.CommandImages([sys.executable, str(script), "{prompt}", "{output}"])
    (tmp_path / "images").mkdir()
    with pytest.raises(providers.LLMError, match="wrote no picture"):
        im("p", str(tmp_path / "images" / ".drawing-1.img"))


# ── review round 1 fixes ────────────────────────────────────────────────────────

def test_r1_anthropic_stop_closes_a_stalled_stream(monkeypatch):
    monkeypatch.setenv("AK", "k")
    closed = threading.Event()

    class Stream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        @property
        def text_stream(self):
            closed.wait(20)                      # stalls until closed from the other thread
            raise RuntimeError("connection closed")
            yield ""
        def close(self): closed.set()

    class Api:
        def stream(self, **kw): return Stream()
    client = type("C", (), {"messages": Api(), "beta": type("B", (), {"messages": Api()})(),
                            "close": lambda self: closed.set()})()
    w = providers.AnthropicText(key_env="AK", client_factory=lambda key: client)
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()
    t0 = time.time()
    with pytest.raises(providers.Cancelled):
        w("s", "p", [], cancel=ev)
    assert time.time() - t0 < 2 and closed.wait(2)


def test_r1_estimate_counts_characters_not_bytes():
    assert providers.usage_of("éééé", 8) == {"in": 2, "out": 1, "exact": False}


def test_r1_http_writers_refuse_over_their_limit(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    called = []
    w = providers.OpenAIText("m", key_env="TEST_KEY", max_request_bytes=100,
                             transport=httpx.MockTransport(lambda r: called.append(r)))
    with pytest.raises(providers.LLMError, match="capped"):
        w("s" * 60, "p" * 60, [])
    assert called == []


def test_r1_history_link_is_refused(tmp_path):
    lib = VoiceLibrary(str(tmp_path))
    v = lib.create("me")
    v.add("s.md", "one")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, os.path.join(v.dir, ".history"))
    with pytest.raises(VoiceError, match="link"):
        v.write("s.md", "two")
    assert list(outside.iterdir()) == []


def test_r1_case_only_differences_are_the_same_voice_file(tmp_path):
    v = VoiceLibrary(str(tmp_path)).create("me")
    assert v.add("Sample.md", "one") == "Sample.md"
    assert v.add("sample.md", "two") == "Sample.md"
    assert [f["path"] for f in v.files()].count("Sample.md") == 1 and v.read("Sample.md") == "two"


def test_r1_ledger_totals_stay_exact_as_it_grows(tmp_path):
    from slopmill.usage import Ledger
    led = Ledger(str(tmp_path))
    for i in range(3000):
        led.record(slug="a" if i % 2 else "b", **{"in": 10, "out": 1, "exact": True})
    assert led.summary("a")["issue"]["in"] == 15000
    with open(led.path, "a") as f:
        f.write('{"slug": "a", "in": 5')           # a half-written line is not counted yet
    assert led.summary("a")["issue"]["calls"] == 1500
    with open(led.path, "a") as f:
        f.write(', "out": 0, "exact": true}\n')
    assert led.summary("a")["issue"]["calls"] == 1501


def test_r1_failed_and_stopped_calls_are_recorded(make):
    class Failing(Fake):
        def __call__(self, *a, **k):
            raise agent.LLMError("HTTP 500")
    c, _ = make(llm=Failing())
    c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    assert wait(c)["status"] == "failed"
    rec = c.get("/api/usage", params={"slug": "099-test"}).json()["recent"][0]
    assert rec["outcome"] == "failed" and rec["in"] > 1000 and rec["exact"] is False
    c2, _ = make(images=FakeImages(fail="refused"))
    c2.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c2)
    recs = c2.get("/api/usage", params={"slug": "099-test"}).json()["recent"]
    assert any(r["call"] == "image" and r["outcome"] == "failed" for r in recs)


def test_r1_a_pass_does_not_force_the_plan_meter(make, tmp_path):
    calls = tmp_path / "calls"
    script = tmp_path / "plan.py"
    script.write_text(f"open({str(calls)!r}, 'a').write('x'); print('{{}}')")
    plan = providers.CommandPlan([sys.executable, str(script)])
    c, _ = make(plan=plan)
    c.get("/api/usage")
    end = time.time() + 5
    while time.time() < end and not (calls.exists() and not plan.busy):
        time.sleep(0.05)
    for _ in range(2):
        c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
        wait(c)
    time.sleep(0.5)
    assert calls.read_text() == "x"


def test_r1_upload_over_forty_names_the_rest(make):
    c, _ = make()
    c.post("/api/voices", json={"name": "me"}, headers=H)
    files = [{"name": f"f{i}.md", "text": "x"} for i in range(41)]
    r = c.post("/api/voices/me/files", json={"files": files}, headers=H).json()
    assert len(r["added"]) == 39 and any("f40.md" in x for x in r["refused"])   # 40-file cap on the pack


def test_r1_pictures_survive_a_change_of_design(make):
    c, d = make(images=FakeImages())
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    name = next(f for f in os.listdir(d / "images") if f.startswith("img-"))
    other = _minimal_file(**{'assets_dir = "images"': 'assets_dir = "art"'})
    if 'assets_dir = "art"' not in other:
        other = other.replace('[pack]\n', '[pack]\nassets_dir = "art"\n', 1)
    assert c.post("/api/designs", json={"text": other}, headers=H).status_code == 200
    c.put("/api/issues/099-test/settings", json={"design": "mine"}, headers=H)
    assert c.get(f"/api/issues/099-test/asset/{name}").content == JPEG
    st = c.get("/api/issues/099-test").json()
    assert any(b["attrs"].get("picture") == "1" for b in st["blocks"])


def test_r1_image_proxy_stops_reading_past_the_cap(monkeypatch):
    import http.server
    import socketserver

    class Big(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            for _ in range(200):
                self.wfile.write(b"x" * 10_000)
        def log_message(self, *a): pass
    srv = socketserver.TCPServer(("127.0.0.1", 0), Big)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from slopmill import net          # the fetcher moved to slopmill/net.py (research uses it too)
    monkeypatch.setattr(net, "MAX_ASSET_BYTES", 50_000)
    try:
        with pytest.raises(app_mod.FetchRefused, match="over"):
            app_mod.fetch_public(f"http://127.0.0.1:{srv.server_address[1]}/x.jpg", allow_private=True)
    finally:
        srv.shutdown()


# ── review round 2 fixes ────────────────────────────────────────────────────────

def test_r2_nested_unknown_keys_are_refused():
    text = _minimal_file(**{'[components.figure]\n': '[components.figure]\ncommand = "curl x"\n'})
    with pytest.raises(EnvironmentProblem, match="does not take command"):
        design_from_text(text)
    with pytest.raises(EnvironmentProblem, match="does not take"):
        design_from_text(_minimal_file(**{"[meta]\n": "[meta]\nhook = \"x\"\n"}))


def test_r2_linked_manifest_is_refused(tmp_path):
    v = VoiceLibrary(str(tmp_path)).create("me")
    outside = tmp_path / "evil.json"
    outside.write_text(json.dumps({"files": [{"path": "brief.md", "role": "brief"}]}))
    os.remove(os.path.join(v.dir, "voice.json"))
    os.symlink(outside, os.path.join(v.dir, "voice.json"))
    with pytest.raises(VoiceError, match="link"):
        v.manifest()


def test_r2_plan_command_with_an_odd_shape_is_unavailable(make):
    plan = providers.CommandPlan([sys.executable, "-c", "print('[]')"])
    c, _ = make(plan=plan)
    c.get("/api/usage")
    end = time.time() + 5
    while time.time() < end and plan.busy:
        time.sleep(0.05)
    snap = c.get("/api/usage").json()["plan"]
    assert snap["busy"] is False and snap["error"]


def test_r2_reference_style_image_in_a_reply_is_refused(make):
    llm = Fake(text={"p": "::: {.figure}\n![x][pic]\n:::\n\n[pic]: private.jpg"})
    c, d = make(llm=llm, images=FakeImages())
    c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    wait(c)
    assert "private.jpg" not in (d / "issue.md").read_text()
    job = c.get("/api/issues/099-test").json()["job"]
    assert "image" in job["blocks"]["p"]["detail"]


def test_r2_the_authors_own_figure_can_still_be_revised(make):
    text = DOC + '\n::: {.figure #fig}\n![A dog](dog.jpg)\n\nOld caption.\n:::\n'
    llm = Fake(text={"fig": "::: {.figure}\n![A dog](dog.jpg)\n\nNew caption.\n:::"})
    c, d = make(llm=llm, text=text)
    c.post("/api/issues/099-test/comments", json={"block": "fig", "note": "better caption"}, headers=H)
    c.post("/api/issues/099-test/revise", json={}, headers=H)
    wait(c)
    props = c.get("/api/issues/099-test").json()["review"]["proposals"]
    assert props and "New caption." in props[0]["text"]


def test_r2_something_that_is_not_a_picture_is_not_counted_as_one(make):
    c, _ = make(images=FakeImages(data=b"<html>error</html>"))
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    u = c.get("/api/usage", params={"slug": "099-test"}).json()
    assert u["issue"]["images"] == 0
    assert any(r["call"] == "image" and r.get("outcome") == "failed" for r in u["recent"])


def test_r2_a_refused_start_leaves_no_chat_message(make):
    c, _ = make(llm=Fake(delay=2))
    c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    r = c.post("/api/issues/099-test/generate", json={"targets": ["p"], "direction": "make it shorter"}, headers=H)
    assert r.status_code == 409
    assert not any(m["text"] == "make it shorter" for m in c.get("/api/issues/099-test").json()["review"]["chat"])
    wait(c)


def test_r2_undo_puts_the_review_back_too(make):
    llm = Fake(text={"a": "A changed opening."})
    c, d = make(llm=llm)
    c.post("/api/issues/099-test/generate", json={"targets": ["p"]}, headers=H)
    wait(c)
    draft = next(b for b in blocks_of(d) if b.type == "draft")
    llm.text[draft.id] = "A shorter draft."
    c.post("/api/issues/099-test/comments", json={"block": draft.id, "note": "shorter"}, headers=H)
    c.post("/api/issues/099-test/comments", json={"block": "a", "note": "change it"}, headers=H)
    c.post("/api/issues/099-test/revise", json={}, headers=H)
    wait(c)
    rv = c.get("/api/issues/099-test").json()["review"]
    assert all(x["status"] == "sent" for x in rv["comments"]) and rv["proposals"][0]["status"] == "pending"
    assert c.post("/api/issues/099-test/undo", json={}, headers=H).status_code == 200
    rv = c.get("/api/issues/099-test").json()["review"]
    assert all(x["status"] == "queued" for x in rv["comments"])
    assert rv["proposals"][0]["status"] == "undone"


def test_r2_a_save_returns_the_new_request_size(make):
    c, _ = make()
    st = c.get("/api/issues/099-test").json()
    before = st["next_request"]["bytes"]
    blocks = st["blocks"] + [{"type": "prose", "id": "b-zzzz", "text": "x" * 5000, "attrs": {}}]
    r = c.put("/api/issues/099-test/doc", json={"base": st["rev"], "meta": st["meta"], "blocks": blocks}, headers=H)
    assert r.json()["next_request"]["bytes"] >= before + 5000


def test_r2_keeping_a_picture_draft_keeps_the_picture(make):
    c, d = make(images=FakeImages())
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    st = c.get("/api/issues/099-test").json()
    draft = next(b for b in st["blocks"] if b["type"] == "draft" and b["attrs"]["for"] == "pic")
    name = re.search(r"\((img-[^)]+)\)", draft["text"]).group(1)
    assert c.post("/api/issues/099-test/keep", json={"draft": draft["id"], "base": st["rev"]}, headers=H).status_code == 200
    kept = next(b for b in blocks_of(d) if b.id == draft["id"])
    assert kept.type == "component" and name in kept.text
    assert c.get(f"/api/issues/099-test/asset/{name}").content == JPEG


def test_r2_a_replaced_design_is_not_served_from_memory(make):
    c, _ = make()
    c.post("/api/designs", json={"text": _minimal_file()}, headers=H)
    lib = c.app.state.designs
    first = lib.get("mine")
    c.post("/api/designs", json={"text": _minimal_file(**{'green = "#00aa00"': 'green = "#00bb00"'})}, headers=H)
    assert lib.get("mine") is not first and lib.get("mine").accents["green"] == "#00bb00"


def test_r2_request_size_counts_file_framing(monkeypatch, tmp_path):
    files = []
    for i in range(10):
        p = tmp_path / f"f{i}.md"
        p.write_text("x" * 5)
        files.append(str(p))
    with pytest.raises(providers.LLMError):
        providers._check_size(500, "", "", files)      # 50 bytes of text, 640 with framing


def test_steps_editing_a_picture_drafts_caption_keeps_the_figure(make):
    """SPEC-STEPS 7 for a drawn picture: the edited draft stays its figure, now locked."""
    c, d = make(images=FakeImages())
    c.post("/api/issues/099-test/generate", json={}, headers=H)
    wait(c)
    st = c.get("/api/issues/099-test").json()
    draft = next(b for b in st["blocks"] if b["type"] == "draft" and b["attrs"]["for"] == "pic")
    new = re.sub(r"(\n)([^\n]*)(\n:::\s*)$", r"\1A caption I wrote myself.\3", draft["text"].rstrip() + "\n")
    r = c.post(f"/api/issues/099-test/blocks/{draft['id']}/edit", json={"orig": draft["text"], "text": new}, headers=H)
    assert r.status_code == 200, r.text
    kept = next(b for b in blocks_of(d) if b.id == draft["id"])
    assert kept.type == "component" and "A caption I wrote myself." in kept.text
    assert not any(b.type == "prompt" and b.id == "pic" for b in blocks_of(d))
