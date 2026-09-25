# SPDX-License-Identifier: MIT
"""Walk the Pandoc AST and draw each block through the pack's templates.

Two targets are rendered from the same tree: "web" (the site body) and "email". They
differ only where a template says so, which today is media: email clients strip <video>
and cannot switch themes, so a figure draws differently for each.
"""
import json
import os
from dataclasses import dataclass
from html.entities import codepoint2name

from markupsafe import Markup

from . import source as src
from .errors import CompileError, Problem

VIDEO_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}
# slopmill's own blocks. A pack cannot define components with these names.
RESERVED = ("prompt", "draft")
MODES = ("publish", "preview")
LINK_SCHEMES = ("https://", "http://", "mailto:")
TARGETS = ("web", "email")


@dataclass
class BlockOut:
    id: str
    kind: str
    line: int
    web: str
    email: str
    problems: list = None


@dataclass
class Result:
    meta: dict
    blocks: list
    body: str
    site: str
    email: str
    meta_json: str
    assets: list
    preview: str = ""        # web body with every block wrapped in data-block (preview mode)
    media: list = None       # every image/video/poster the source points at, local or remote
    problems: list = None    # preview mode collects problems instead of raising
    prompts: dict = None     # prompt id -> draft id (None when not written yet)


# ── escaping ──────────────────────────────────────────────────────────────────────

def esc_text(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esc_attr(s):
    # Apostrophes stay literal, as they are in hand-written HTML.
    return esc_text(s).replace('"', "&quot;")


def to_entities(s):
    """Non-ASCII characters as named entities where HTML has a name, numeric otherwise.

    This is the convention hand-written issues use, and the one the linter reads: it
    matches '—' and '&mdash;' but would miss '&#8212;'. Emitting anything else would
    hide a character from the check that exists to catch it.
    """
    out = []
    for ch in s:
        o = ord(ch)
        if o < 128:
            out.append(ch)
        elif o in codepoint2name:
            out.append(f"&{codepoint2name[o]};")
        else:
            out.append(f"&#{o};")
    return "".join(out)


# ── inline ────────────────────────────────────────────────────────────────────────

class Ctx:
    def __init__(self, pack, meta, read_url, source_dir, asset_url=None):
        self.asset_url = asset_url
        self.pack = pack
        self.meta = meta
        self.read_url = read_url
        self.source_dir = source_dir
        self.problems = []
        self.assets = []       # local asset names referenced, in order
        self.media = []        # every media reference, however it was written
        self.line = None

    def problem(self, msg):
        self.problems.append(Problem(msg, self.line))


def _attrs(raw):
    """Pack-supplied attribute text, e.g. style="color:#fff" or class="accent". The pack
    decides whether inline elements are styled inline (email) or by class."""
    return f" {raw.strip()}" if raw and raw.strip() else ""


def _accent_attrs(ctx, name):
    colour = ctx.pack.accents.get(name)
    if colour is None:
        known = ", ".join(sorted(ctx.pack.accents)) or "none"
        ctx.problem(f"unknown accent .{name} (this pack has: {known})")
        return None
    fmt = ctx.pack.inline.get("accent")
    if fmt is None:
        ctx.problem("this pack defines accents but no inline.accent attribute")
        return None
    return fmt.format(colour=colour, name=name)


def _one_accent(ctx, attr, what):
    ident, classes, kvs = attr
    if ident:
        ctx.problem(f"an ID (#{ident}) is attached to {what} instead of a block: put a "
                    f"blank line above the {{#{ident}}} line")
    if kvs:
        ctx.problem(f"{what} takes no attributes, got {', '.join(k for k, _ in kvs)}")
    if len(classes) > 1:
        ctx.problem(f"{what} takes at most one accent, got .{' .'.join(classes)}")
    return classes[0] if classes else None


def inlines(nodes, ctx):
    return "".join(inline(n, ctx) for n in nodes)


def inline(n, ctx):
    t, c = n["t"], n.get("c")
    if t == "Str":
        return esc_text(c)
    if t == "Space":
        return " "
    if t == "SoftBreak":
        return "\n"
    if t == "LineBreak":
        return "<br>"
    if t == "Emph":
        return f"<em{_attrs(ctx.pack.inline.get('emphasis'))}>{inlines(c, ctx)}</em>"
    if t == "Strong":
        return f"<strong{_attrs(ctx.pack.inline.get('strong'))}>{inlines(c, ctx)}</strong>"
    if t == "Quoted":
        q, body = c
        l, r = ("“", "”") if q["t"] == "DoubleQuote" else ("‘", "’")
        return f"{l}{inlines(body, ctx)}{r}"
    if t == "Span":
        attr, body = c
        accent = _one_accent(ctx, attr, "text")
        if accent is None:
            return inlines(body, ctx)
        attrs = _attrs(_accent_attrs(ctx, accent))
        if len(body) == 1 and body[0]["t"] == "Strong":
            return f"<strong{attrs}>{inlines(body[0]['c'], ctx)}</strong>"
        return f"<span{attrs}>{inlines(body, ctx)}</span>"
    if t == "Link":
        attr, body, (url, title) = c
        accent = _one_accent(ctx, attr, "a link")
        if title:
            ctx.problem(f"link titles are not used: {url}")
        if not url.startswith(LINK_SCHEMES):
            ctx.problem(f"link {url!r} is not an absolute http(s) or mailto address; "
                        f"a relative link breaks in the email")
        raw = _accent_attrs(ctx, accent) if accent else ctx.pack.inline.get("link")
        return f'<a href="{esc_attr(url)}"{_attrs(raw)}>{inlines(body, ctx)}</a>'
    if t == "Code":
        attr, text = c
        if "code" not in ctx.pack.inline:
            ctx.problem(f"inline code `{text}` is not styled by this pack")
            return esc_text(text)
        return f"<code{_attrs(ctx.pack.inline['code'])}>{esc_text(text)}</code>"
    if t == "Image":
        ctx.problem("an image outside a figure: wrap it in a ::: {.figure} block")
        return ""
    if t == "RawInline":
        fmt, text = c
        if fmt == "html" and src.COMMENT_RE.match(text):
            return ""
        ctx.problem(f"raw HTML is not allowed in the source: {text.strip()[:60]!r}")
        return ""
    ctx.problem(f"{t} is not something this pack can render")
    return ""


def plain(nodes, ctx):
    """Inline content as plain text for an attribute (alt text). Smart quotes are
    straightened: an attribute is read aloud or shown bare, never typeset. Anything that
    cannot be flattened to text is reported, not dropped."""
    out = []
    for n in nodes:
        t, c = n["t"], n.get("c")
        if t == "Str":
            out.append(c.replace("’", "'").replace("‘", "'"))
        elif t in ("Space", "SoftBreak", "LineBreak"):
            out.append(" ")
        elif t == "Quoted":
            q = '"' if c[0]["t"] == "DoubleQuote" else "'"
            out.append(q + plain(c[1], ctx) + q)
        elif t in ("Emph", "Strong", "Underline", "SmallCaps"):
            out.append(plain(c, ctx))
        elif t in ("Span", "Link"):
            # Flattened, but checked as strictly as in prose: an unknown accent or a
            # stray ID must not vanish just because it sat in alt text.
            accent = _one_accent(ctx, c[0], "alt text")
            if accent:
                _accent_attrs(ctx, accent)
            out.append(plain(c[1], ctx))
        elif t == "Code":
            out.append(c[1])
        elif t == "RawInline":
            ctx.problem(f"raw HTML is not allowed in alt text: {c[1].strip()[:60]!r}")
        else:
            ctx.problem(f"{t} cannot be part of alt text")
    return "".join(out)


# ── blocks ────────────────────────────────────────────────────────────────────────

def _render(ctx, template, **kw):
    tpl = ctx.pack.template(template)
    out = {}
    for target in TARGETS:
        out[target] = tpl.render(target=target, read_url=ctx.read_url, meta=ctx.meta, **kw)
    return out["web"], out["email"]


def _asset(ctx, ref, what):
    """An absolute URL passes through; a bare filename lives in the pack's asset base."""
    ctx.media.append(ref)
    if ref.startswith(("https://", "http://")):
        return ref, None
    if not ref or "/" in ref or "\\" in ref or ref.startswith(".") or ".." in ref:
        ctx.problem(f"{what} {ref!r} must be a bare filename (it is served from the "
                    f"asset directory) or an absolute https URL")
        return ref, None
    ctx.assets.append(ref)
    if ctx.asset_url:
        local = ctx.asset_url(ref)
        if local:
            return local, ref
    return ctx.pack.asset_base + ref, ref


def _component_attrs(ctx, comp, kvs):
    attrs = {}
    for k, v in kvs:
        if k == "data-pos":
            continue
        if k not in comp.attrs:
            allowed = ", ".join(comp.attrs) or "none"
            ctx.problem(f"::: {{.{comp.name}}} does not take {k}= (allowed: {allowed})")
            continue
        attrs[k] = v
    for k, need in comp.attrs.items():
        if need == "required" and not attrs.get(k):
            ctx.problem(f"::: {{.{comp.name}}} needs {k}=\"...\"")
        attrs.setdefault(k, None)
    return attrs


def _figure(ctx, comp, children, attrs):
    if not children:
        ctx.problem("an empty figure")
        return None
    if len(children) > 2:
        ctx.problem("a figure holds one image or video, then at most one caption paragraph")
    first = children[0]
    items = [n for n in first.get("c", []) if n["t"] not in ("Space", "SoftBreak")] \
        if first["t"] in ("Para", "Plain") else []
    if len(items) != 1 or items[0]["t"] != "Image":
        ctx.problem("a figure must start with a paragraph holding exactly one image: "
                    "![alt text](file.jpg)")
        return None
    (iid, iclasses, ikvs), alt_nodes, (ref, title) = items[0]["c"]
    if iid or iclasses:
        ctx.problem("an image takes no ID or class; put the ID on the figure")
    if title:
        ctx.problem(f"image titles are not used: {ref}")
    alt = plain(alt_nodes, ctx).strip()
    ext = os.path.splitext(ref.split("?")[0])[1].lower()
    kind = "video" if ext in VIDEO_TYPES else "image"
    if not alt:
        ctx.problem(f"{kind} {ref} has no alt text: ![describe it here]({ref})")
    allowed = {"image": {"light"}, "video": {"poster"}}[kind]
    kv = {}
    for k, v in ikvs:
        if k not in allowed:
            ctx.problem(f"{kind} {ref} does not take {k}= (allowed: {', '.join(sorted(allowed))})")
        else:
            kv[k] = v
    url, name = _asset(ctx, ref, kind)
    media = {"kind": kind, "src": url, "name": name, "alt": alt,
             "light": None, "poster": None, "poster_name": None, "mime": None}
    if "light" in kv:
        media["light"], _ = _asset(ctx, kv["light"], "light image")
    if kind == "video":
        media["mime"] = VIDEO_TYPES[ext]
        if not kv.get("poster"):
            ctx.problem(f"video {ref} needs a poster=still.jpg: email clients cannot play "
                        f"video, so the email shows the still, linked to the issue page")
        else:
            media["poster"], media["poster_name"] = _asset(ctx, kv["poster"], "poster")
    media = {k: (Markup(esc_attr(v)) if isinstance(v, str) else v) for k, v in media.items()}
    caption = None
    if len(children) > 1:
        cap = children[1]
        if cap["t"] not in ("Para", "Plain"):
            ctx.problem("a figure caption must be a plain paragraph")
        else:
            caption = Markup(inlines(cap["c"], ctx))
    return _render(ctx, comp.template, media=media, caption=caption, attrs=attrs,
                   params=comp.params)


def _container(ctx, comp, children, attrs):
    paras = []
    for ch in children:
        if ch["t"] not in ("Para", "Plain"):
            ctx.problem(f"::: {{.{comp.name}}} holds paragraphs only, found {ch['t']}")
            continue
        paras.append(Markup(inlines(ch["c"], ctx)))
    n = len(paras)
    if not comp.min_paragraphs <= n <= comp.max_paragraphs:
        want = (f"exactly {comp.min_paragraphs}" if comp.min_paragraphs == comp.max_paragraphs
                else f"{comp.min_paragraphs} to {comp.max_paragraphs}")
        ctx.problem(f"::: {{.{comp.name}}} holds {want} paragraph(s), found {n}")
    return _render(ctx, comp.template, paragraphs=paras, attrs=attrs, params=comp.params)


def _draft(tb, ctx, children):
    """A draft renders its blocks as if they stood at the top level. They carry no IDs of
    their own: the draft is the unit a comment or a revision addresses."""
    webs, emails = [], []
    for child in children:
        attr = src.attr_of(child)
        if attr and child["t"] == "Div" and set(attr[1]) & set(RESERVED):
            ctx.problem("a draft cannot hold a prompt or another draft")
            continue
        if attr and attr[0]:
            ctx.problem(f"blocks inside a draft take no ID (found #{attr[0]})")
            continue
        out = block(src.TopBlock(node=child, id=None, line=tb.line), ctx)
        if out:
            webs.append(out[1])
            emails.append(out[2])
    return "draft", "\n\n".join(webs), "\n\n".join(emails)


def _prompts(tops):
    """prompt id -> the draft written from it, and problems with the pairing."""
    prompts, problems = {}, []
    for tb in tops:
        attr = src.attr_of(tb.node)
        if tb.node["t"] == "Div" and attr[1][:1] == ["prompt"]:
            prompts[tb.id] = None
    for tb in tops:
        attr = src.attr_of(tb.node)
        if tb.node["t"] == "Div" and attr[1][:1] == ["draft"]:
            target = dict(attr[2]).get("for")
            if not target:
                problems.append(Problem(f"draft #{tb.id} needs for=PROMPT-ID", tb.line))
            elif target not in prompts:
                problems.append(Problem(
                    f"draft #{tb.id} was written from #{target}, which is not a prompt here", tb.line))
            elif prompts[target]:
                problems.append(Problem(
                    f"prompt #{target} has two drafts: #{prompts[target]} and #{tb.id}", tb.line))
            else:
                prompts[target] = tb.id
    return prompts, problems


def block(tb, ctx):
    """Render one top-level block. Returns (kind, web, email) or None on a problem."""
    ctx.line = tb.line
    n = tb.node
    t, c = n["t"], n.get("c")
    pack = ctx.pack
    if t == "Para":
        web, email = _render(ctx, pack.block_templates["paragraph"],
                             content=Markup(inlines(c, ctx)))
        return "paragraph", web, email
    if t == "Header":
        level, (_, classes, kvs), body = c
        tpl = pack.heading_templates.get(level)
        if classes or [k for k, _ in kvs if k != "data-pos"]:
            ctx.problem("a heading takes an ID only, no classes or attributes")
        if not tpl:
            have = ", ".join(f"h{k}" for k in sorted(pack.heading_templates)) or "none"
            ctx.problem(f"this pack has no template for a level-{level} heading (has: {have})")
            return None
        web, email = _render(ctx, tpl, content=Markup(inlines(body, ctx)), level=level)
        return "heading", web, email
    if t == "BlockQuote":
        tpl = pack.block_templates.get("quote")
        if not tpl:
            ctx.problem("this pack has no quote template")
            return None
        if len(c) != 1 or c[0]["t"] not in ("Para", "Plain"):
            ctx.problem("a quote holds exactly one paragraph")
            return None
        web, email = _render(ctx, tpl, content=Markup(inlines(c[0]["c"], ctx)))
        return "quote", web, email
    if t in ("BulletList", "OrderedList"):
        tpl = pack.block_templates.get("list")
        if not tpl:
            ctx.problem("this pack has no list template")
            return None
        items_ast = c if t == "BulletList" else c[1]
        start = 1 if t == "BulletList" else c[0][0]
        items = []
        for item in items_ast:
            if len(item) != 1 or item[0]["t"] not in ("Para", "Plain"):
                kinds = ", ".join(b["t"] for b in item)
                ctx.problem(f"a list item holds one line of text (nested lists and "
                            f"multi-paragraph items are not supported), found {kinds}")
                continue
            items.append(Markup(inlines(item[0]["c"], ctx)))
        web, email = _render(ctx, tpl, items=items, ordered=(t == "OrderedList"), start=start)
        return "list", web, email
    if t == "Div":
        (ident, classes, kvs), children = c
        if len(classes) != 1:
            if not classes:
                ctx.problem("a fenced div needs a component class, e.g. ::: {.figure}")
            else:
                ctx.problem(f"one component class per block, got .{' .'.join(classes)}")
            return None
        if classes[0] == "prompt":
            return "prompt", "", ""
        if classes[0] == "draft":
            return _draft(tb, ctx, children)
        comp = pack.components.get(classes[0])
        if not comp:
            known = ", ".join(sorted(pack.components)) or "none"
            ctx.problem(f"unknown component .{classes[0]} (this pack has: {known})")
            return None
        attrs = _component_attrs(ctx, comp, kvs)
        out = (_figure if comp.shape == "figure" else _container)(ctx, comp, children, attrs)
        if out is None:
            return None
        return comp.name, out[0], out[1]
    if t == "RawBlock":
        ctx.problem(f"raw HTML is not allowed in the source: {c[1].strip()[:60]!r}")
        return None
    ctx.problem(f"{t} blocks are not supported")
    return None


# ── the whole issue ──────────────────────────────────────────────────────────────

NULLS = ("", "~", "null", "Null", "NULL")


def _meta(ctx_problems, pack, meta):
    """Front matter arrives as strings (see source.split_front_matter). Convert what the
    pack types; an absent or null value is None."""
    out = {}
    for k in meta:
        if k not in pack.meta_fields:
            ctx_problems.append(Problem(
                f"unknown front matter key {k!r} (this pack takes: {', '.join(pack.meta_fields)})"))
    for k in pack.meta_fields:
        v = meta.get(k)
        if v is not None and not isinstance(v, str):
            ctx_problems.append(Problem(f"front matter {k} must be a single value"))
            v = None
        if v is not None and v.strip() in NULLS:
            v = None
        if v is None:
            if k in pack.meta_required:
                ctx_problems.append(Problem(f"front matter is missing {k}"))
        elif pack.meta_types.get(k) == "int":
            try:
                v = int(v, 10)
            except ValueError:
                ctx_problems.append(Problem(f"front matter {k} must be a whole number, got {v!r}"))
        out[k] = v
    return out


def compile_text(text, pack, *, path="issue.md", mode="preview", check_assets=False,
                 asset_url=None):
    """Compile source text that is not (or not yet) a file on disk."""
    meta, body = src.split_front_matter(text, path)
    return compile_source(src.Source(path=path, text=text, meta=meta, body=body), pack,
                          mode=mode, check_assets=check_assets, asset_url=asset_url)


def compile_source(source, pack, *, check_assets=True, mode="publish", asset_url=None):
    """mode="publish": any problem raises CompileError, and every prompt must have a draft.
    mode="preview": problems are collected on the result and the blocks that did render
    are returned, so an editor can show the issue while it is still being written."""
    if mode not in MODES:
        raise ValueError(mode)
    problems, notes = [], []
    meta = _meta(problems if mode == "publish" else notes, pack, source.meta)
    try:
        read_url = pack.read_url.format(**{k: ("" if v is None else v) for k, v in meta.items()})
    except (KeyError, IndexError) as e:
        problems.append(Problem(f"pack read_url needs front matter {e}"))
        read_url = ""
    source_dir = os.path.dirname(os.path.abspath(source.path))
    ctx = Ctx(pack, meta, read_url, source_dir, asset_url=asset_url)

    ast, pos_ast = src.parse(source)
    tops = src.top_blocks(ast, pos_ast)
    for tb in tops:
        problems.extend(tb.problems)
    problems.extend(src.check_ids(tops))
    problems.extend(src.lost_ids(source, ast))
    prompts, pairing = _prompts(tops)
    problems.extend(pairing)
    if mode == "publish":
        for pid, did in prompts.items():
            if not did:
                line = next((t.line for t in tops if t.id == pid), None)
                problems.append(Problem(f"prompt #{pid} has not been written yet", line))

    blocks = []
    for tb in tops:
        before = len(ctx.problems)
        out = block(tb, ctx)
        mine = ctx.problems[before:]
        if out:
            kind, web, email = out
            blocks.append(BlockOut(tb.id, kind, tb.line, to_entities(web), to_entities(email),
                                   mine or None))
        else:
            blocks.append(BlockOut(tb.id, "error", tb.line, "", "", mine or None))
    problems.extend(ctx.problems)
    blocks_out = [b for b in blocks if b.kind not in ("prompt", "error")]

    if check_assets:
        adir = os.path.join(source_dir, pack.assets_dir)
        for name in dict.fromkeys(ctx.assets):
            if not os.path.isfile(os.path.join(adir, name)):
                problems.append(Problem(
                    f"{name} is referenced but not in {os.path.relpath(adir)}/ "
                    f"(pass --no-asset-check to build without the files)"))
    if problems and mode == "publish":
        raise CompileError(problems, source.path)

    banner = (f"<!-- Generated by slopmill from {os.path.basename(source.path)}. "
              f"Edit the source, not this file. -->\n\n")
    body = banner + "\n\n".join(b.web for b in blocks_out if b.web) + "\n"
    email = "\n\n".join(b.email for b in blocks_out if b.email) + "\n"
    site = pack.site_head + body
    meta_json = json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
    preview = ""
    if mode == "preview":
        preview = "\n".join(_preview_block(b, prompts) for b in blocks) + "\n"
    return Result(meta=meta, blocks=blocks_out if mode == "publish" else blocks, body=body,
                  site=site, email=email, meta_json=meta_json,
                  assets=list(dict.fromkeys(ctx.assets)), preview=preview,
                  media=list(dict.fromkeys(ctx.media)),
                  problems=problems + notes, prompts=prompts)


def _preview_block(b, prompts):
    """Each block wrapped so an editor can map a selection back to its ID. The wrapper
    exists only in preview output; the site and email bodies never carry it."""
    ident = esc_attr(b.id or "")
    if b.kind == "prompt":
        if prompts.get(b.id):
            return ""
        return (f'<div class="cmp-block cmp-pending" data-block="{ident}" data-kind="prompt">'
                f'</div>')
    if b.kind == "error":
        msgs = "".join(f"<li>{esc_text(str(p))}</li>" for p in (b.problems or []))
        return (f'<div class="cmp-block cmp-error" data-block="{ident}" data-kind="error">'
                f'<ul>{msgs}</ul></div>')
    extra = ' data-problems="1"' if b.problems else ""
    return annotate(b.web, f' data-block="{ident}" data-kind="{b.kind}"{extra}')


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "source", "track", "wbr"}


def annotate(html, attrs):
    """Add attributes to every top-level element of a block's HTML, in place. Wrapping
    the block in a <div> would change the page's structure (a `.body > p` rule would stop
    matching), so the preview marks the elements the block already has instead."""
    from html.parser import HTMLParser

    starts = []

    class P(HTMLParser):
        depth = 0

        def handle_starttag(self, tag, a):
            if self.depth == 0:
                starts.append((self.getpos(), tag))
            if tag not in VOID:
                self.depth += 1

        def handle_startendtag(self, tag, a):
            if self.depth == 0:
                starts.append((self.getpos(), tag))

        def handle_endtag(self, tag):
            if tag not in VOID:
                self.depth = max(0, self.depth - 1)

    P(convert_charrefs=True).feed(html)
    line_starts = [0]
    for i, ch in enumerate(html):
        if ch == "\n":
            line_starts.append(i + 1)
    out, last = [], 0
    for (line, col), tag in starts:
        at = line_starts[line - 1] + col + 1 + len(tag)
        out.append(html[last:at])
        out.append(attrs)
        last = at
    out.append(html[last:])
    return "".join(out)
