# SPDX-License-Identifier: AGPL-3.0-or-later
"""The finished issue as files to take away (SPEC-EXPORT): a web page with its pictures
inside, a Word document, and a zip of everything.

The issue is compiled once, with each picture slopmill holds replaced by a stand-in
address; each file then puts the picture back the way it needs it (inside the page, or
next to it in images/).
"""
import base64
import io
import mimetypes
import re
import secrets
import subprocess
import threading
import zipfile
from html.parser import HTMLParser

from . import render
from .errors import CompileError
from .pandoc import _found as found_pandoc
from .render import VIDEO_TYPES, esc_attr, esc_text

MAX_INLINE = 50_000_000         # pictures and videos inside one file
PANDOC_TIMEOUT = 90
CONVERTING = threading.BoundedSemaphore(2)


class ExportRefused(Exception):
    """The issue cannot be exported as it is; the message says what to do."""

    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


class Picture:
    def __init__(self, name, data, file):
        self.name = name
        self.data = data
        self.file = file          # its name in images/: unique even where case is not kept

    @property
    def mime(self):
        ext = ("." + self.name.rsplit(".", 1)[-1].lower()) if "." in self.name else ""
        return VIDEO_TYPES.get(ext) or mimetypes.guess_type(self.name)[0] or "application/octet-stream"

    def data_uri(self):
        return f"data:{self.mime};base64," + base64.b64encode(self.data).decode("ascii")


class Compiled:
    """The issue compiled once, with a stand-in address for every picture slopmill holds."""

    def __init__(self, text, path, pack, picture):
        self.pack = pack
        self.pictures = []                # in first-use order, each once
        index = {}
        nonce = secrets.token_hex(8)

        taken = set()

        def asset_url(name):
            if name not in index:
                data = picture(name)
                index[name] = None if data is None else len(self.pictures)
                if data is not None:
                    self.pictures.append(Picture(name, data, unique(name, taken)))
            if index[name] is None:
                return None               # the design's published address stands
            return f"https://x{nonce}.invalid/{index[name]}/"
        self._token = re.compile(rf"https://x{nonce}\.invalid/(\d+)/")
        self._nonce = nonce           # random: any address a template kept part of carries it
        self.design = pack.name
        try:
            result = render.compile_text(text, pack, path=path, mode="preview",
                                         asset_url=asset_url)
        except CompileError as e:
            raise ExportRefused(f"the issue does not read cleanly: {e}")
        pending = [p for p, d in result.prompts.items() if not d]
        broken = [b for b in result.blocks if b.kind == "error"]
        # Anything else Proof lists as a problem: a block can draw and still leave something
        # out (a figure's third paragraph). Front matter is another design's business.
        counted = {id(p) for b in broken for p in (b.problems or [])}
        other = [p for p in result.problems
                 if id(p) not in counted and "front matter" not in p.message]
        why = []
        if pending:
            why.append(f"{len(pending)} prompt{'s are' if len(pending) > 1 else ' is'} not "
                       f"written yet: write {'them' if len(pending) > 1 else 'it'} on Draft, "
                       f"or delete {'them' if len(pending) > 1 else 'it'}")
        if broken:
            why.append(f"{len(broken)} block{'s do' if len(broken) > 1 else ' does'} not "
                       f"draw: Proof shows {'them' if len(broken) > 1 else 'it'} in red")
        if other:
            why.append(f"{len(other)} problem{'s' if len(other) > 1 else ''} in the text: Proof "
                       f"lists {'them' if len(other) > 1 else 'it'} at the top")
        if why:
            raise ExportRefused("Not downloaded. " + "; ".join(why) + ".")
        shown = [b for b in result.blocks if b.kind not in ("prompt", "error")]
        self.web = "\n\n".join(b.web for b in shown if b.web) + "\n"
        self.email = "\n\n".join(b.email for b in shown if b.email) + "\n"
        self.meta = result.meta

    def title(self, slug):
        return str(self.meta.get("title") or "").strip() or slug

    def inline_bytes(self):
        """What the pictures come to inside the page, each time one is used: base64 is four
        bytes for every three."""
        return sum(4 * ((len(self.pictures[int(m.group(1))].data) + 2) // 3)
                   for m in self._token.finditer(self.web))

    def fill(self, html, how):
        """Put each picture back: how = "inline" (data: URIs) or "files" (images/NAME)."""
        def one(m):
            p = self.pictures[int(m.group(1))]
            return esc_attr(p.data_uri() if how == "inline" else f"images/{p.file}")
        out = self._token.sub(one, html)
        if self._nonce in out:        # a template changed the address it was given
            raise ExportRefused(f"Not downloaded. The design {self.design} changes picture "
                                f"addresses, so its pictures cannot be put in the file.")
        return out


def unique(name, taken):
    """`name`, or name-2, name-3... so that no two files in images/ differ only in case."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    out, n = name, 1
    while out.lower() in taken:
        n += 1
        out = f"{stem}-{n}.{ext}" if dot else f"{stem}-{n}"
    taken.add(out.lower())
    return out


# ── the files ─────────────────────────────────────────────────────────────────
NO_SCRIPT = ('<meta http-equiv="Content-Security-Policy" '
             'content="script-src \'none\'; object-src \'none\'; base-uri \'none\'">')


def web_page(c, slug, how):
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"{NO_SCRIPT}\n"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
            f"<title>{esc_text(c.title(slug))}</title>\n"
            f"<style>{c.pack.page_css}</style>\n{c.pack.site_head}\n</head>\n<body>\n"
            f"<div class=\"issue-body\">\n{c.fill(c.web, how)}</div>\n</body>\n</html>\n")


def email_page(c, slug):
    subject = str(c.meta.get("subject") or "").strip() or c.title(slug)
    pre = str(c.meta.get("preview_text") or "").strip()
    preheader = (f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">'
                 f'{esc_text(pre)}</div>\n' if pre else "")
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"{NO_SCRIPT}\n"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
            f"<title>{esc_text(subject)}</title>\n</head>\n"
            "<body style=\"margin:0;padding:24px 12px\">\n" + preheader +
            "<div style=\"max-width:640px;margin:0 auto\">\n"
            f"{c.fill(c.email, 'files')}</div>\n</body>\n</html>\n")


def check_size(c):
    n = c.inline_bytes()
    if n > MAX_INLINE:
        raise ExportRefused(f"The pictures come to {n / 1e6:.0f} MB, too many for one file: "
                            f"download All files (.zip) instead.", status=413)


def word(c, slug, pictures=True):
    """pictures=False: each picture is its description in [brackets], for the zip's copy when
    the pictures are too many for one file (they are next to it in images/)."""
    if pictures:
        check_size(c)
    html = c.fill(c.web, "inline" if pictures else "files")
    return convert(clean_html(html, c.title(slug), heading=False,
                              img_ok=lambda s: pictures and s.startswith("data:image/")), "docx")


def markdown(c, slug):
    return convert(clean_html(c.fill(c.web, "files"), c.title(slug), heading=True,
                              img_ok=lambda s: s.startswith("images/")),
                   "gfm-raw_html").decode("utf-8")


def zip_all(c, slug):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{slug}/{slug}.html", web_page(c, slug, "files"))
        z.writestr(f"{slug}/{slug}-email.html", email_page(c, slug))
        z.writestr(f"{slug}/{slug}.md", markdown(c, slug))
        z.writestr(f"{slug}/{slug}.docx", word(c, slug, pictures=c.inline_bytes() <= MAX_INLINE),
                   compress_type=zipfile.ZIP_STORED)
        for p in c.pictures:
            z.writestr(f"{slug}/images/{p.file}", p.data, compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


# ── pandoc, fed only what it cannot fetch with ─────────────────────────────────
# No tables: an issue's text cannot make one (pipe tables are off), so every table in the
# page is a design's layout for email clients, and Markdown would print it as "[TABLE]".
# Its cells' contents stay; the grid goes.
KEEP = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "em", "i", "u", "a", "ul",
        "ol", "li", "blockquote", "br", "hr", "img", "figure", "figcaption", "div", "span",
        "pre", "code", "sup", "sub", "small"}
DROP = {"script", "style", "head", "title", "template", "iframe", "object", "embed",
        "noscript", "svg", "math", "audio", "canvas", "select", "textarea", "button"}
VOID_TAGS = {"br", "hr", "img", "source", "track", "wbr", "input", "meta", "link", "area",
             "base", "col", "embed", "param"}
SKIPPED = DROP | {"video"}
HREF_OK = re.compile(r"^(https?:|mailto:|#)", re.I)


class _Clean(HTMLParser):
    def __init__(self, img_ok):
        super().__init__(convert_charrefs=True)
        self.img_ok = img_ok
        self.out = []
        self.skip = 0          # inside a DROP element
        self.open = []         # the tags we kept, to close in order

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.skip:
            if tag in SKIPPED and tag not in VOID_TAGS:
                self.skip += 1
            return
        if tag in DROP:
            self.skip = 1
            return
        if tag == "video":
            self.picture(a.get("poster") or "", "video")
            self.skip = 1          # its <source>s point at the film itself
            return
        if tag == "img":
            self.picture(a.get("src") or "", a.get("alt") or "")
            return
        if tag not in KEEP:
            return                 # unwrapped: its text stays
        kept = ""
        if tag == "a" and HREF_OK.match(a.get("href") or ""):
            kept = f' href="{esc_attr(a["href"])}"'
        self.out.append(f"<{tag}{kept}>")
        if tag not in VOID_TAGS:
            self.open.append(tag)

    def picture(self, src, alt):
        """A picture pandoc may embed, or its description in [brackets]: linked when the
        picture is on the web, since a link is never fetched."""
        if self.img_ok(src):
            self.out.append(f'<img src="{esc_attr(src)}" alt="{esc_attr(alt)}">')
        elif alt and src.startswith(("https://", "http://")):
            self.out.append(f'<em>[<a href="{esc_attr(src)}">{esc_text(alt)}</a>]</em>')
        elif alt:
            self.out.append(f"<em>[{esc_text(alt)}]</em>")

    def handle_startendtag(self, tag, attrs):
        if tag in VOID_TAGS or tag == "img":
            self.handle_starttag(tag, attrs)
        elif not self.skip and tag in KEEP:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if tag in SKIPPED:
                self.skip -= 1
            return
        if tag in self.open:
            while self.open:
                t = self.open.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(esc_text(data))

    def close(self):
        super().close()
        while self.open:
            self.out.append(f"</{self.open.pop()}>")


def clean_html(html, title, *, heading, img_ok):
    """Only the tags and attributes a document needs, and only pictures that `img_ok`
    accepts: nothing in what pandoc reads points anywhere it could fetch."""
    p = _Clean(img_ok)
    p.feed(html)
    p.close()
    head = f"<h1>{esc_text(title)}</h1>\n" if heading else ""
    body = re.sub(r"<(p|div|span)>[\s\xa0]*</\1>", "", "".join(p.out))     # spacers
    return (f"<!doctype html>\n<html><head><title>{esc_text(title)}</title></head>"
            f"<body>\n{head}{body}\n</body></html>\n")


def convert(html, to):
    """pandoc from HTML to `to`, with a memory cap and a time limit. --sandbox too, where
    this pandoc can use it: 3.1 cannot find its own docx template inside the sandbox, so
    for it the clean HTML (nothing fetchable in it) is what keeps pandoc local."""
    exe, _ = found_pandoc()
    base = [exe, "+RTS", "-M512m", "-RTS", "-f", "html", "-t", to, "--wrap=none", "-o", "-"]
    if not CONVERTING.acquire(timeout=60):
        raise ExportRefused("Another download is being made: try again in a moment.", status=503)
    try:
        proc = _run(base[:4] + ["--sandbox"] + base[4:], html)
        if proc.returncode != 0 and b"Could not find data file" in proc.stderr:
            proc = _run(base, html)
    finally:
        CONVERTING.release()
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"]
        raise ExportRefused(f"pandoc could not make the {to.split('-')[0]} file: {tail[0][:300]}",
                            status=500)
    return proc.stdout


def _run(argv, html):
    try:
        return subprocess.run(argv, input=html.encode("utf-8"), capture_output=True,
                              timeout=PANDOC_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ExportRefused("pandoc took too long to make the file.", status=500)
    except OSError as e:
        raise ExportRefused(f"pandoc did not run: {e}", status=500)


KINDS = {
    "html": ("text/html; charset=utf-8", "html"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "zip": ("application/zip", "zip"),
}


def make(kind, text, path, pack, slug, picture):
    """(bytes, media type, file name) for one download. `picture(name)` gives the bytes of
    a picture slopmill holds, or None."""
    if kind not in KINDS:
        raise ExportRefused("no such download", status=404)
    c = Compiled(text, path, pack, picture)
    if kind == "html":
        check_size(c)
        body = web_page(c, slug, "inline").encode("utf-8")
    elif kind == "docx":
        body = word(c, slug)
    else:
        body = zip_all(c, slug)
    media, ext = KINDS[kind]
    return body, media, f"{slug}.{ext}"
