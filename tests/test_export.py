# SPDX-License-Identifier: MIT
"""SPEC-EXPORT: the finished issue as a web page, a Word document, or a zip of everything."""
import base64
import http.server
import io
import os
import re
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient

from conftest import FIXTURE_PACK, FRONT
from slopmill import export
from slopmill.pack import load_pack
from slopmill.server import app as app_mod
from slopmill.server.app import create_app

S = "/api/issues/099-test/export"


def jpeg(color=(200, 40, 40), size=(40, 30)):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "JPEG")
    return b.getvalue()


PIC = jpeg()
OLD = jpeg((20, 90, 200))

DONE = FRONT + """{#a}
This week has found me on the road a lot more than usual, and **the bridge**{.green} held.

::: {.prompt #p}
Talk about the voice file revisions.
:::

::: {.draft #d for=p prompt=00000000}
The voice file got shorter. [A link](https://example.org/read) and *a slant*.

## A heading here

> A quoted line.
:::

::: {.figure #f}
![A red box on the page](pic.jpg)

The caption under it.
:::

::: {.figure #g}
![A blue box from the older design](old.jpg)
:::

::: {.figure #m}
![A picture nobody has here](missing.jpg)
:::

{#z}
Thank you readers for sticking with us.
"""


FETCHED = []


@pytest.fixture
def make(tmp_path, monkeypatch):
    FETCHED.clear()
    monkeypatch.setattr(app_mod, "fetch_public", lambda url, **k: FETCHED.append(url))

    def go(text=DONE, demo=True):
        ws = tmp_path / "ws"
        d = ws / "issues" / "099-test"
        (d / "images").mkdir(parents=True)
        (d / "images" / "pic.jpg").write_bytes(PIC)
        (d / "art").mkdir()                          # a folder from a design used before
        (d / "art" / "old.jpg").write_bytes(OLD)
        (d / "issue.md").write_text(text)
        app = create_app(workspace=str(ws), pack=load_pack(FIXTURE_PACK), llm=lambda *a, **k: "",
                         model_label="fake", token="tok", demo=demo)
        c = TestClient(app)
        c.cookies.set("slopmill_token", "tok")
        return c, d
    return go


def b64(data):
    return base64.b64encode(data).decode("ascii")


# ── 2, 3, 9: the web page ──────────────────────────────────────────────────────
def test_2_3_9_web_page_is_the_proof_in_one_file(make):
    c, _ = make()
    r = c.get(f"{S}/html")
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"] == 'attachment; filename="099-test.html"'
    page = r.text
    assert "<title>Test</title>" in page
    # both pictures slopmill holds are inside, byte for byte, once each
    assert f"data:image/jpeg;base64,{b64(PIC)}" in page
    assert f"data:image/jpeg;base64,{b64(OLD)}" in page
    # one it does not hold keeps the design's published address (and nothing was fetched)
    assert 'src="https://example.com/newsletter/missing.jpg"' in page and FETCHED == []
    # the words, and none of the editor's own marks
    for words in ("the road a lot more", "The voice file got shorter", "A heading here",
                  "Thank you readers", "The caption under it"):
        assert words in page
    for mark in ("data-block", "cmp-", "data-kind", "Talk about the voice file"):
        assert mark not in page
    # the design's page CSS and head, after a policy that allows no script
    csp = page.index("script-src 'none'")
    assert csp < page.index(".issue-body { max-width: 640px") < page.index(".issue-body img { max-width: 100%")
    assert '<div class="issue-body">' in page


def test_2_blocks_match_the_proof_preview(make):
    c, _ = make()
    page = c.get(f"{S}/html").text
    prev = c.get("/api/issues/099-test/preview").json()["html"]
    strip = lambda h: re.sub(r' data-(block|kind|problems)="[^"]*"', "", h)   # noqa: E731
    prev_text = re.sub(r"<[^>]+>", "", strip(prev)).split()
    page_body = page.split('<div class="issue-body">', 1)[1]
    page_text = re.sub(r"<[^>]+>", "", page_body).split()
    assert prev_text == page_text


# ── 4: one file has a limit ───────────────────────────────────────────────────────
def encoded(*blobs):
    return sum(len(base64.b64encode(b)) for b in blobs)


def test_4_the_limit_is_what_the_pictures_come_to_inside_the_file(make, monkeypatch):
    c, _ = make()
    monkeypatch.setattr(export, "MAX_INLINE", encoded(PIC, OLD))
    assert c.get(f"{S}/html").status_code == 200
    # raw bytes are well under this; embedded, they are one byte over
    monkeypatch.setattr(export, "MAX_INLINE", encoded(PIC, OLD) - 1)
    assert len(PIC) + len(OLD) < export.MAX_INLINE
    assert c.get(f"{S}/html").status_code == 413


def test_4_too_big_for_one_file_points_at_the_zip(make, monkeypatch):
    monkeypatch.setattr(export, "MAX_INLINE", encoded(PIC, OLD) - 1)
    c, _ = make()
    for kind in ("html", "docx"):
        r = c.get(f"{S}/{kind}")
        assert r.status_code == 413 and "All files" in r.json()["error"], kind
    z = zipfile.ZipFile(io.BytesIO(c.get(f"{S}/zip").content))
    assert z.read("099-test/images/pic.jpg") == PIC
    # the zip still has its Word file: each picture as its description, the files beside it
    text, dz = docx_text(z.read("099-test/099-test.docx"))
    assert "[A red box on the page]" in text and "[A blue box from the older design]" in text
    assert not [n for n in dz.namelist() if n.startswith("word/media/")]


# ── 5: Word ───────────────────────────────────────────────────────────────────────
def docx_text(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    xml = z.read("word/document.xml").decode()
    return "".join(re.findall(r"<w:t[^>]*>([^<]*)", xml)), z


def test_5_word_keeps_the_words_in_order_and_the_pictures(make):
    c, _ = make()
    r = c.get(f"{S}/docx")
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"] == 'attachment; filename="099-test.docx"'
    text, z = docx_text(r.content)
    order = ["Test", "the road a lot more", "the bridge", "The voice file got shorter", "A link",
             "a slant", "A heading here", "A quoted line", "The caption under it",
             "A picture nobody has here", "Thank you readers"]
    at = [text.find(w) for w in order]
    assert all(i >= 0 for i in at) and at == sorted(at), dict(zip(order, at))
    media = sorted(z.read(n) for n in z.namelist() if n.startswith("word/media/"))
    assert media == sorted([PIC, OLD])
    rels = z.read("word/_rels/document.xml.rels").decode()
    assert "https://example.org/read" in rels                   # the link survives
    # a picture slopmill does not hold: its description, linked to where it is published
    assert "[A picture nobody has here]" in text
    assert "https://example.com/newsletter/missing.jpg" in rels


# ── 6, 7: the zip and its email version ─────────────────────────────────────────────
def test_6_7_zip_is_laid_out_plainly(make):
    c, _ = make()
    r = c.get(f"{S}/zip")
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"] == 'attachment; filename="099-test.zip"'
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == sorted([
        "099-test/099-test.html", "099-test/099-test-email.html", "099-test/099-test.md",
        "099-test/099-test.docx", "099-test/images/pic.jpg", "099-test/images/old.jpg"])
    assert z.read("099-test/images/pic.jpg") == PIC and z.read("099-test/images/old.jpg") == OLD
    page = z.read("099-test/099-test.html").decode()
    assert 'src="images/pic.jpg"' in page and 'src="images/old.jpg"' in page and "base64" not in page
    md = z.read("099-test/099-test.md").decode()
    assert md.startswith("# Test\n")
    assert "](images/pic.jpg)" in md and "](images/old.jpg)" in md
    assert "[A link](https://example.org/read)" in md and "<" not in md
    assert "![" not in md.split("](images/old.jpg)")[1]          # no picture from the web
    assert "[A picture nobody has here](https://example.com/newsletter/missing.jpg)" in md
    mail = z.read("099-test/099-test-email.html").decode()
    assert "<title>Test subject</title>" in mail
    assert re.search(r'<div style="display:none[^"]*">Preview</div>', mail)
    assert "max-width:640px" in mail and 'src="images/pic.jpg"' in mail
    assert "script-src 'none'" in mail and "<video" not in mail


# ── 8: unfinished work is not exported silently ─────────────────────────────────────
def test_8_an_unwritten_prompt_refuses_with_what_to_do(make):
    c, _ = make(text=DONE.replace("::: {.draft #d for=p prompt=00000000}", "::: {.draft #d for=q prompt=00000000}")
                .replace("::: {.prompt #p}", "::: {.prompt #p}\nx\n:::\n\n::: {.prompt #q}"))
    for kind in ("html", "docx", "zip"):
        r = c.get(f"{S}/{kind}")
        assert r.status_code == 422, (kind, r.text)
        assert r.json()["error"] == ("Not downloaded. 1 prompt is not written yet: write it on "
                                     "Draft, or delete it."), r.json()


def test_8_a_block_that_does_not_draw_refuses(make):
    c, _ = make(text=DONE.replace("{#z}\n", "::: {.figure #bad}\nNo picture in this one.\n:::\n\n{#z}\n"))
    r = c.get(f"{S}/html")
    assert r.status_code == 422 and "1 block does not draw" in r.json()["error"]


def test_8_front_matter_trouble_does_not_block(make):
    c, _ = make(text=DONE.replace("title: Test\n", "title: Test\nbogus_field: from another design\n")
                .replace("og_image: x.jpg\n", ""))
    assert c.get(f"{S}/html").status_code == 200


# ── 10: pandoc is handed nothing it could fetch ─────────────────────────────────────
class Hits(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        Hits.seen.append(self.path)
        self.send_response(200)
        self.send_header("content-type", "image/png")
        self.end_headers()

    def log_message(self, *a):
        pass


def test_10_pandoc_never_fetches():
    srv = http.server.HTTPServer(("127.0.0.1", 0), Hits)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        dirty = (f'<p>Hi <img src="{base}/img.png" alt="far away"></p>'
                 f'<iframe src="{base}/frame"></iframe><object data="{base}/obj"></object>'
                 f'<video poster="{base}/poster.jpg"><source src="{base}/film.mp4"></video>'
                 f'<link rel="stylesheet" href="{base}/x.css"><script src="{base}/s.js"></script>'
                 f'<p><a href="javascript:alert(1)">bad link</a> <a href="{base}/ok">ok link</a></p>'
                 f'<svg><image href="{base}/svg.png"/></svg><p>after</p>')
        clean = export.clean_html(dirty, "T", heading=False, img_ok=lambda s: s.startswith("data:image/"))
        assert base + "/ok" in clean               # a link is a link, not a fetch
        # the picture and the video's still became descriptions, linked; nothing else points there
        assert f'<em>[<a href="{base}/img.png">far away</a>]</em>' in clean
        assert f'<em>[<a href="{base}/poster.jpg">video</a>]</em>' in clean
        assert clean.count(base) == 3 and "<img" not in clean
        assert "javascript" not in clean and "after" in clean
        for to in ("docx", "gfm-raw_html"):
            export.convert(clean, to)
        assert Hits.seen == []
    finally:
        srv.shutdown()


def test_10_clean_html_keeps_structure_and_drops_attributes():
    out = export.clean_html('<p style="color:red" onclick="x()"><strong class="k">B</strong> '
                            '<span style="color:#f00">red</span></p><table><tr><td colspan="2" '
                            'style="x">c</td></tr></table><noscript><video></video><p>gone</p></noscript>'
                            '<p>kept</p>', "T", heading=True, img_ok=lambda s: False)
    body = out.split("<body>", 1)[1]
    assert "<h1>T</h1>" in body and "<p><strong>B</strong> <span>red</span></p>" in body
    assert "<table" not in body and "<td" not in body and ">c<" in body       # layout: contents only
    assert "style" not in body and "onclick" not in body
    assert "gone" not in body and "<p>kept</p>" in body


# ── 11: the author's own ────────────────────────────────────────────────────────────
def test_11_needs_the_token_and_a_real_issue(make):
    c, _ = make()
    assert TestClient(c.app).get(f"{S}/html").status_code == 401
    assert c.get("/api/issues/nope/export/html").status_code == 404
    assert c.get(f"{S}/pdf").status_code == 404


def test_3_outside_the_demo_a_published_picture_is_fetched_like_the_preview(make, monkeypatch):
    c, d = make(demo=False)
    got = []

    class R:
        status_code = 200
        content = jpeg((0, 200, 0))

    def fetch(url, allow_private=False):
        got.append(url)
        return R()
    monkeypatch.setattr(app_mod, "fetch_public", fetch)
    page = c.get(f"{S}/html").text
    assert got == ["https://example.com/newsletter/missing.jpg"]
    assert f"data:image/jpeg;base64,{b64(R.content)}" in page
    # the preview reads the same cached copy: no second fetch
    assert c.get("/api/issues/099-test/asset/missing.jpg").content == R.content and len(got) == 1


# ── round 2 ─────────────────────────────────────────────────────────────────────────
def test_4_a_picture_used_twice_counts_twice(make, monkeypatch):
    twice = DONE.replace("{#z}\n", "::: {.figure #f2}\n![The red box again](pic.jpg)\n:::\n\n{#z}\n")
    c, _ = make(text=twice)
    monkeypatch.setattr(export, "MAX_INLINE", encoded(PIC, PIC, OLD))
    page = c.get(f"{S}/html").text
    assert page.count(f"data:image/jpeg;base64,{b64(PIC)}") == 2
    monkeypatch.setattr(export, "MAX_INLINE", encoded(PIC, PIC, OLD) - 1)
    assert c.get(f"{S}/html").status_code == 413


def test_8_a_block_that_draws_but_leaves_something_out_refuses(make):
    extra = DONE.replace("The caption under it.\n", "The caption under it.\n\nA third paragraph.\n")
    c, _ = make(text=extra)
    prev = c.get("/api/issues/099-test/preview").json()
    assert "A third paragraph" not in prev["html"]                  # Proof drew it without it
    assert any("at most one caption" in p["text"] for p in prev["problems"])
    r = c.get(f"{S}/zip")
    assert r.status_code == 422
    assert r.json()["error"] == "Not downloaded. 1 problem in the text: Proof lists it at the top."


def test_3_a_design_that_rewrites_picture_addresses_is_refused_not_broken(make, tmp_path):
    import shutil
    from slopmill.pack import load_pack as lp
    d = tmp_path / "rewrites"
    shutil.copytree(FIXTURE_PACK, d)
    fig = (d / "templates" / "figure.html").read_text()
    _, issue = make()
    # dropping the scheme, or rewriting the start of the host as round 3 suggested
    for rewrite in ("replace('https://', '//')", "replace('https://x', 'https://cdn')"):
        (d / "templates" / "figure.html").write_text(
            fig.replace('<img src="{{ media.src }}"', '<img src="{{ media.src | ' + rewrite + ' }}"'))
        with pytest.raises(export.ExportRefused) as e:
            export.make("html", (issue / "issue.md").read_text(), str(issue / "issue.md"), lp(str(d)),
                        "099-test", lambda n: PIC if n == "pic.jpg" else None)
        assert "changes picture addresses" in str(e.value), rewrite


def test_12_a_picture_that_cannot_be_read_says_which(make):
    c, d = make()
    os.chmod(d / "images" / "pic.jpg", 0)
    try:
        if os.access(d / "images" / "pic.jpg", os.R_OK):
            pytest.skip("running as a user who can read anything")
        r = c.get(f"{S}/html")
        assert r.status_code == 500 and r.json()["error"].startswith(
            "Not downloaded. The picture pic.jpg could not be read"), r.text
    finally:
        os.chmod(d / "images" / "pic.jpg", 0o644)


def test_6_names_that_differ_only_in_case_stay_apart_in_the_zip(make):
    two = DONE.replace("{#z}\n", "::: {.figure #f2}\n![A capital one](Pic.jpg)\n:::\n\n{#z}\n")
    c, d = make(text=two)
    (d / "images" / "Pic.jpg").write_bytes(OLD)
    z = zipfile.ZipFile(io.BytesIO(c.get(f"{S}/zip").content))
    imgs = sorted(n for n in z.namelist() if "/images/" in n)
    assert imgs == ["099-test/images/Pic-2.jpg", "099-test/images/old.jpg", "099-test/images/pic.jpg"]
    assert len({n.lower() for n in imgs}) == 3
    assert z.read("099-test/images/Pic-2.jpg") == OLD
    page = z.read("099-test/099-test.html").decode()
    assert 'src="images/Pic-2.jpg" alt="A capital one"' in page and 'src="images/pic.jpg"' in page


def test_unique_names():
    taken = set()
    assert [export.unique(n, taken) for n in ("a.jpg", "A.jpg", "a.JPG", "b", "B")] == \
        ["a.jpg", "A-2.jpg", "a-3.JPG", "b", "B-2"]


def test_5_6_a_design_s_layout_table_keeps_its_words(tmp_path):
    """The example design sets a poem in an email layout table; Markdown printed "[TABLE]"."""
    html = ('<table role="presentation"><tr><td><p style="x">A Poem</p><p>Monday is for Thinking,</p>'
            '<p>And Tuesday is for Tea,</p></td></tr></table><p>\xa0</p><p>After.</p>')
    clean = export.clean_html(html, "T", heading=False, img_ok=lambda s: False)
    assert "<p>\xa0</p>" not in clean
    md = export.convert(clean, "gfm-raw_html").decode()
    assert "[TABLE]" not in md and "Tuesday is for Tea" in md and "After." in md
    text, _ = docx_text(export.convert(clean, "docx"))
    assert "Monday is for Thinking," in text
