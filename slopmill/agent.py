# SPDX-License-Identifier: AGPL-3.0-or-later
"""The writing model's two jobs: resolve prompts into drafts, and revise against comments.

Rules the code enforces, whatever the model returns:
  - Prose is the author's. A model's change to a prose block is only ever a proposal the
    author accepts or rejects; it is never written straight into the file.
  - A draft is replaced only if what came back compiles cleanly through the design. A
    reply that uses markup the design cannot draw is reported and discarded.
  - A picture is drawn by the image provider from a description the writing model wrote.
    The figure around it is built here, not by the model, and validated like any draft.
  - The model is called through an injectable provider, so tests never reach a real one.
"""
import json
import os
import re
import tempfile
import time

from . import charts, doc, pandoc, render, research
from .errors import CompileError
from .providers import (COMMAND_MAX_REQUEST, Cancelled, CommandLLM, LLMError,  # noqa: F401
                        sniff_image, usage_of)

MAX_PAYLOAD = COMMAND_MAX_REQUEST      # kept for callers of the old name
MAX_IMAGE_BYTES = 50_000_000
FRAMING = 64                           # bytes a provider adds around each attached file
BLOCK_RE = re.compile(r"^=== BLOCK #?([A-Za-z][A-Za-z0-9_-]*) ===[ \t]*\n(.*?)\n=== END ===",
                      re.S | re.M)
IMAGE_RE = re.compile(r"^=== IMAGE #?([A-Za-z][A-Za-z0-9_-]*) ===[ \t]*\n(.*?)\n=== END ===",
                      re.S | re.M)
NOTE_RE = re.compile(r"^=== NOTE ===[ \t]*\n(.*?)\n=== END ===", re.S | re.M)
PICTURE_RE = re.compile(r"^\s*(image|photo|picture|illustration)\s*:", re.I)
CHART_PROMPT_RE = re.compile(r"^\s*(chart|graph|plot)\s*:", re.I)
IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(<?([^)\s>]+)>?\)")
# A box written on one line: ::: {.concept label="X"} text :::. Pandoc reads that as a
# paragraph that starts with colons, not a box, and the component lines the model is shown
# are written that way, so a model copies it. unfold_boxes() gives it the three lines it needs.
ONE_LINE_BOX_RE = re.compile(r"^([ \t]*)(:{3,})[ \t]*(\{[^{}\n]*\})[ \t]+(\S.*?)[ \t]+:{3,}[ \t]*$")
POS_RE = re.compile(r"(\d+):(\d+)-(\d+):(\d+)")

ENGINE_RULES = """
How this job works:
The attached DOCUMENT.md is the issue as it stands, block by block, in order:
  [PROSE #id]      the author's own words. Context: match them and continue from them.
  [PROMPT #id]     the author's instruction for what goes in that place.
  [DRAFT #id for #prompt]  text written earlier from that prompt.
  [COMPONENT #id]  an image, callout or other element. Context only.

Write Markdown using only this markup:
{vocabulary}
No HTML, no images, no {{#id}} lines, no "===" lines inside a block.

Answer in exactly this format and nothing else:
=== BLOCK <id> ===
<markdown>
=== END ===
(one for each block you were asked for, in document order)
=== NOTE ===
One to three plain sentences for the author: what you wrote, and anything you were unsure of.
=== END ===
""".strip()

PICTURE_RULES = """
Pictures:
A PROMPT marked PICTURE, or any PROMPT that asks for a photo, image or illustration, is
answered with an IMAGE block instead of a BLOCK. An image model draws it from your
description; you never write image markup yourself.
=== IMAGE <id> ===
DESCRIPTION: what to draw: subject, setting, light, mood, style, framing. Wide landscape
framing. No words, letters or logos in the picture. Real people only if the prompt names them.
ALT: one plain sentence saying what the finished picture shows, for someone who cannot see it.
CAPTION: a short caption in the author's voice, or nothing.
=== END ===
A PICTURE prompt that asks for a chart or graph of numbers is answered with a CHART block
(see Charts) instead: an image model cannot draw numbers or labels.
""".strip()


# ── what the model sees ──────────────────────────────────────────────────────────

def vocabulary(pack):
    lines = [
        "- paragraphs, separated by a blank line",
        "- ## Heading for a section heading",
        "- \"- item\" lists and \"> quote\" block quotes",
        "- [text](https://...) links and *italic*",
    ]
    if pack.accents:
        names = ", ".join(f"{{.{n}}}" for n in pack.accents)
        lines.append(f"- coloured emphasis on a short phrase: **phrase**{{.name}} where name is one of {names}")
    offered = [comp.describe for comp in pack.components.values() if comp.describe]
    lines += [f"- {d}" for d in offered]
    if any(":::" in d for d in offered):
        lines.append("- a ::: box is shown above on one line to save space; write it on its own "
                     "lines: the opening ::: {...} line, then the text, then ::: alone on the last line")
    return "\n".join(lines)


def is_picture_prompt(text):
    return bool(PICTURE_RE.match(text or ""))


def is_chart_prompt(text):
    return bool(CHART_PROMPT_RE.match(text or ""))


def image_refs(md):
    """Image names written inline as ![alt](name). Used to find a picture's note; for the
    safety check use media_refs, which sees every syntax the renderer accepts."""
    return set(IMAGE_REF_RE.findall(md or ""))


def media_refs(md, pack, top_level=False):
    """Every image, video or poster a block of Markdown points at, as the design would
    draw it: inline, reference-style or anything else the parser turns into an image.
    top_level: md is a block as it stands in the file (prose or a component, which may
    carry its ID), not a draft's inside."""
    probe = md + "\n" if top_level else ("::: {.prompt #probe-p}\nx\n:::\n\n"
                                          f"::: {{.draft #probe-d for=probe-p}}\n{md}\n:::\n")
    try:
        return set(render.compile_text(probe, pack, mode="preview").media or [])
    except Exception:
        return {"(unreadable)"}


def sidecar_path(images_dir, fname):
    return os.path.join(images_dir, f".{fname}.json")


def read_sidecar(images_dir, fname):
    """The note a drawn picture was saved with. images_dir may be one folder or a list
    (an issue that changed design keeps its older pictures in the old design's folder)."""
    for d in ([images_dir] if isinstance(images_dir, str) else (images_dir or [])):
        try:
            with open(sidecar_path(d, fname), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            continue
    return None


def chart_of(b, images_dir):
    """The data a chart draft was drawn from (a checked spec), or None."""
    if b.type != "draft":
        return None
    for ref in image_refs(b.text):
        side = read_sidecar(images_dir, ref)
        if side and isinstance(side.get("chart"), dict):
            return side["chart"]
    return None


def drawn_from(b, images_dir):
    """The description a picture draft was drawn from, or None if it holds no drawn picture."""
    if b.type != "draft":
        return None
    for ref in image_refs(b.text):
        side = read_sidecar(images_dir, ref)
        if side and side.get("description"):
            return side["description"]
    return None


def document_for_model(meta, blocks, images_dir=None):
    head = []
    for k in ("title", "subject", "number"):
        if meta.get(k) not in (None, ""):
            head.append(f"{k.upper()}: {meta[k]}")
    parts = ["\n".join(head)] if head else []
    for b in blocks:
        if b.type == "comment":
            continue
        extra = ""
        if b.type == "draft":
            desc = drawn_from(b, images_dir)
            chart = None if desc else chart_of(b, images_dir)
            tag = f"[DRAFT #{b.id} for #{b.attrs.get('for', '?')}" + (
                " · PICTURE]" if desc else " · CHART]" if chart else "]")
            if desc:
                extra = f"(the picture was drawn from this description: {desc})\n"
            elif chart:
                try:
                    extra = f"(the chart was drawn from this data:\n{charts.as_block(chart, b.id)})\n"
                except (KeyError, TypeError):
                    extra = ""
        elif b.type == "component":
            tag = f"[COMPONENT #{b.id} {b.attrs.get('class', '')}]".replace(" ]", "]")
        elif b.type == "prompt" and is_picture_prompt(b.text):
            tag = f"[PROMPT #{b.id} · PICTURE]"
        elif b.type == "prompt" and is_chart_prompt(b.text):
            tag = f"[PROMPT #{b.id} · CHART]"
        else:
            tag = f"[{b.type.upper()} #{b.id}]"
        parts.append(f"{tag}\n{extra}{b.text.strip()}")
    return "\n\n".join(parts) + "\n"


def parse_reply(text):
    blocks = {m.group(1): m.group(2) for m in BLOCK_RE.finditer(text or "")}
    note = NOTE_RE.search(text or "")
    return blocks, (note.group(1).strip() if note else "")


def parse_images(text):
    """{id: {"description", "alt", "caption"}} from the reply's IMAGE blocks."""
    out = {}
    for m in IMAGE_RE.finditer(text or ""):
        fields, cur = {"description": [], "alt": [], "caption": []}, None
        for line in m.group(2).split("\n"):
            f = re.match(r"^\s*(DESCRIPTION|PROMPT|ALT|CAPTION)\s*:\s*(.*)$", line, re.I)
            if f:
                cur = {"prompt": "description"}.get(f.group(1).lower(), f.group(1).lower())
                fields[cur].append(f.group(2))
            elif cur:
                fields[cur].append(line)
        out[m.group(1)] = {k: " ".join(" ".join(v).split()) for k, v in fields.items()}
    return out


def clean_markdown(md):
    md = md.strip("\n")
    fence = re.match(r"^```[a-z]*\n(.*)\n```\s*$", md, re.S)
    if fence:
        md = fence.group(1)
    kept = [ln.rstrip() for ln in md.split("\n") if not doc.ID_LINE_RE.match(ln.strip())]
    return unfold_boxes("\n".join(kept).strip("\n"))


def unfold_boxes(md):
    """Rewrite each one-line box (::: {attrs} text :::) as opening line, text, closing line.
    A line Pandoc reads as code (fenced, indented, or raw HTML) is left alone: which lines
    those are depends on list items, quotes and fences closing implicitly, so Pandoc says."""
    lines = md.split("\n")
    if not any(ONE_LINE_BOX_RE.match(ln) for ln in lines):
        return md
    code = code_lines(md)
    out = []
    for n, ln in enumerate(lines, 1):
        m = None if n in code else ONE_LINE_BOX_RE.match(ln)
        if m:
            indent, colons, attrs, text = m.groups()
            out += [f"{indent}{colons} {attrs}", f"{indent}{text}", f"{indent}{colons}"]
        else:
            out.append(ln)
    return "\n".join(out)


def code_lines(md):
    """1-based numbers of the lines Pandoc reads as a code block or a raw HTML block."""
    found = set()

    def cover(attr):
        for k, v in attr[2]:
            if k == "data-pos":
                for a, _, c, d in POS_RE.findall(v):
                    # c:d is the position just past the block; column 1 means line c is not in it
                    found.update(range(int(a), int(c) + (0 if int(d) == 1 else 1)))

    def inside(attr):
        # An inline code span: only the lines wholly inside it, never its first or last
        # line, which a real box on one line may share with it (::: {.x} use `y` :::).
        for k, v in attr[2]:
            if k == "data-pos":
                for a, _, c, _ in POS_RE.findall(v):
                    found.update(range(int(a) + 1, int(c)))

    def walk(x):
        if isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, dict):
            if x.get("t") == "CodeBlock":
                cover(x["c"][0])
            elif x.get("t") == "Code":
                inside(x["c"][0])
            elif x.get("t") == "Div" and any(isinstance(b, dict) and b.get("t") == "RawBlock"
                                             for b in x["c"][1]):
                cover(x["c"][0])   # sourcepos puts a raw block's position on its wrapper
            walk(x.get("c"))

    walk(pandoc.to_ast(md, sourcepos=True)["blocks"])
    return found


def check_markdown(md, pack):
    """Problems with a block of model-written Markdown, as the design would draw it.
    Empty list means it can be written into the file."""
    if not md.strip():
        return ["the reply for this block was empty"]
    probe = ("::: {.prompt #probe-p}\nx\n:::\n\n"
             f"::: {{.draft #probe-d for=probe-p}}\n{md}\n:::\n")
    try:
        _, blocks = doc.parse(probe)
    except (doc.DocError, CompileError) as e:
        return [str(e)]
    if [b.type for b in blocks] != ["prompt", "draft"] or blocks[1].text.strip() != md.strip():
        return ["the reply breaks out of its block (an unbalanced ::: line)"]
    result = render.compile_text(probe, pack, mode="preview")
    real = [str(p) for p in result.problems if not str(p).startswith("front matter")]
    return real


def escape_alt(text):
    """Alt text as Markdown that reads back as exactly these words."""
    text = " ".join(str(text).split())
    return re.sub(r"([\\`*_{}\[\]<>#!|~^$@&])", r"\\\1", text)


def figure_markdown(pack, fname, alt, caption):
    comp = pack.figure_component()
    md = f"::: {{.{comp}}}\n![{escape_alt(alt)}]({fname})\n"
    if caption.strip():
        md += f"\n{caption.strip()}\n"
    return md + ":::"


# ── the passes ───────────────────────────────────────────────────────────────────

def _system(pack, voice, images):
    voice_system, files = voice.voice() if voice is not None else pack.voice()
    rules = ENGINE_RULES.format(vocabulary=vocabulary(pack))
    if images is not None and pack.figure_component():
        rules += "\n\n" + PICTURE_RULES
    if pack.figure_component():
        rules += "\n\n" + charts.RULES
    return (voice_system.strip() + "\n\n" + rules).strip(), files


def request_size(llm, pack, voice, images, meta, blocks, ask, images_dir=None):
    system, files = _system(pack, voice, images)
    return (len(system.encode()) + len(ask.encode())
            + sum(os.path.getsize(p) + FRAMING for _, p in files)
            + len(document_for_model(meta, blocks, images_dir).encode()) + FRAMING)


def _call(llm, pack, voice, images, meta, blocks, ask, workdir, cancel, log, on_usage, images_dir,
          extra_files=(), extra_rules=""):
    system, voice_files = _system(pack, voice, images)
    if extra_rules:
        system += "\n\n" + extra_rules
    tmp, docfile = _write_document(workdir, meta, blocks, images_dir)
    files = [p for _, p in voice_files] + [docfile] + [p for p in extra_files if p]
    return _send(llm, system, ask, files, tmp, cancel, log, on_usage,
                 f"voice pack: {len(voice_files)} files")


def _write_document(workdir, meta, blocks, images_dir):
    os.makedirs(workdir, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=workdir, prefix="run-")
    docfile = os.path.join(tmp, "DOCUMENT.md")
    with open(docfile, "w", encoding="utf-8") as f:
        f.write(document_for_model(meta, blocks, images_dir))
    return tmp, docfile


def _send(llm, system, ask, files, tmp, cancel, log, on_usage, what):
    """One model call with its size checked, its reply kept in tmp/REPLY.md and its usage
    recorded, whatever happens."""
    size = len(system.encode()) + len(ask.encode()) + sum(os.path.getsize(p) + FRAMING for p in files)
    limit = getattr(llm, "max_request_bytes", MAX_PAYLOAD)
    log("info", f"{what}; request {size // 1024}KB of {limit // 1024}KB")
    if size > limit:
        raise LLMError(f"the request is {size // 1024}KB and this model takes at most "
                       f"{limit // 1024}KB: turn off some voice files or shorten the issue")
    chars = len(system) + len(ask)
    for p in files:
        with open(p, encoding="utf-8", errors="replace") as f:
            chars += len(f.read())
    try:
        reply = llm(system, ask, files, cancel=cancel)
    except (LLMError, Cancelled) as e:
        # The request went out and may have been billed: record what was sent.
        on_usage({"kind": "text", **usage_of("", chars), "out": 0,
                  "outcome": "stopped" if isinstance(e, Cancelled) else "failed"})
        raise
    with open(os.path.join(tmp, "REPLY.md"), "w", encoding="utf-8") as f:
        f.write(str(reply))
    used = usage_of(reply, chars)
    on_usage({"kind": "text", **used})
    mark = "" if used["exact"] else "≈"
    log("info", f"tokens: {mark}{used['in']:,} in, {mark}{used['out']:,} out"
        + ("" if used["exact"] else " (estimated from characters)"))
    return str(reply)


def _research(llm, items, searcher, room, meta, blocks, workdir, cancel, log, on_usage, images_dir):
    """Look things up for items ({id: the request's words}) before the writing call.
    Returns (sources, path of SOURCES.md or None). room: bytes the writing call can still
    take. Nothing here fails the pass: without sources the writer is told nothing was found."""
    if searcher is None or not items:
        return [], None, "off"
    if len(items) > research.MAX_ITEMS:
        log("warn", f"researching the first {research.MAX_ITEMS} of {len(items)} requests; the rest "
                    f"are written without research, and the writer is told so")
    items = dict(list(items.items())[:research.MAX_ITEMS])
    if room < research.MIN_ROOM:
        log("warn", f"no room left in the request for research ({max(room, 0) // 1024}KB): "
                    f"turn off some voice files to make room")
        return [], None, "failed"
    tmp, docfile = _write_document(workdir, meta, blocks, images_dir)
    if getattr(searcher, "native", False):
        return _native_research(searcher, items, room, docfile, tmp, cancel, log, on_usage)
    log("info", f"researching {', '.join('#' + i for i in items)}: asking the writer what to search for")

    def send(system, prompt, files):
        return _send(llm, system, prompt, files, tmp, cancel, log, on_usage, "planning the research")
    try:
        plan = {k: v for k, v in research.plan_searches(send, items, docfile).items() if k in items and v}
    except LLMError as e:
        log("warn", f"planning the research failed, so this is written without it: {e}")
        return [], None, "failed"
    if not plan:
        log("info", "the writer says the text already has what it needs; no searches")
        return [], None, "not-needed"
    sources, text = research.gather(plan, items, searcher, room - 1000, log, cancel)
    if not sources:
        log("warn", "the research found nothing readable; writing without it")
        return [], None, "failed"
    path = _write_sources(tmp, text)
    log("ok", f"research: {len(sources)} source(s), {len(text.encode()) // 1024 or 1}KB for the writer")
    return sources, path, "found"


def _write_sources(tmp, text):
    """SOURCES.md, headed so that nothing copied from a page can pass for an instruction."""
    path = os.path.join(tmp, "SOURCES.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(research.SOURCES_HEAD + text)
    return path


def _native_research(searcher, items, room, docfile, tmp, cancel, log, on_usage):
    """Research through the writer's own web search: one call, its sources checked against
    the pages the provider says it saw."""
    log("info", f"researching {', '.join('#' + i for i in items)} with {searcher.label}")
    with open(docfile, encoding="utf-8") as f:
        issue = f.read()
    prompt = ("The requests, each with its id:\n\n"
              + "\n\n".join(f"[{i}] {t.strip()}" for i, t in items.items())
              + "\n\nThe issue they are for, as context (do not search for its own words):\n"
              + issue[:30_000])
    chars = len(research.NATIVE_SYSTEM) + len(prompt)
    try:
        reply, seen = searcher.writer.web_research(research.NATIVE_SYSTEM, prompt, cancel=cancel)
    except LLMError as e:
        on_usage({"kind": "text", **usage_of("", chars), "out": 0, "outcome": "failed"})
        log("warn", f"the web search failed, so this is written without it: {e}")
        return [], None, "failed"
    with open(os.path.join(tmp, "RESEARCH-REPLY.md"), "w", encoding="utf-8") as f:
        f.write(str(reply))
    on_usage({"kind": "text", **usage_of(reply, chars)})
    log("info", f"the search saw {len(seen)} page(s)")
    sources, text = research.from_native(reply, seen, items, room - 1000, log)
    if not sources:
        log("warn", "the research found nothing it could stand behind; writing without it")
        return [], None, "failed"
    path = _write_sources(tmp, text)
    log("ok", f"research: {len(sources)} source(s), {len(text.encode()) // 1024 or 1}KB for the writer")
    return sources, path, "found"


def _room(llm, pack, voice, images, meta, blocks, ask, images_dir):
    limit = getattr(llm, "max_request_bytes", MAX_PAYLOAD)
    return limit - request_size(llm, pack, voice, images, meta, blocks, ask, images_dir) \
        - len(research.USE_SOURCES.encode()) - 2 * FRAMING


def _draw(images, pack, target_id, spec, images_dir, cancel, log, block_event, on_usage, created):
    """Draw one picture and return the figure Markdown for it. Raises ValueError with the
    reason the author sees if anything is wrong."""
    if images is None:
        raise ValueError("no image generator is configured on this server (see docs/PROVIDERS.md)")
    if not pack.figure_component():
        raise ValueError(f"the design {pack.name} has no figure component to hold a picture")
    desc, alt, caption = spec["description"], spec["alt"], spec["caption"]
    if not desc or not alt:
        raise ValueError("the model's picture request had no description or no alt text")
    if len(desc) > 4000 or len(alt) > 400 or len(caption) > 600:
        raise ValueError("the model's picture request was too long")
    block_event(target_id, "drawing")
    log("info", f"#{target_id}: drawing a picture with {getattr(images, 'label', 'the image model')}")
    os.makedirs(images_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fd, tmp = tempfile.mkstemp(dir=images_dir, prefix=".drawing-", suffix=".img")
    os.close(fd)
    created.append(tmp)
    stem = os.path.splitext(tmp)[0]
    try:
        try:
            got = images(desc, tmp, cancel=cancel)
        except LLMError as e:
            on_usage({"kind": "image", "images": 0, "in": 0, "out": 0, "exact": True, "outcome": "failed"})
            raise ValueError(str(e))
        except Cancelled:
            on_usage({"kind": "image", "images": 0, "in": 0, "out": 0, "exact": True, "outcome": "stopped"})
            raise
    finally:
        # A provider may write beside tmp under another extension; every such file is
        # this pass's to clean up if the picture is not kept.
        for f in os.listdir(images_dir):
            p = os.path.join(images_dir, f)
            if os.path.splitext(p)[0] == stem and p not in created:
                created.append(p)
    got = got if isinstance(got, str) and got else tmp
    if os.path.realpath(os.path.dirname(got)) != os.path.realpath(images_dir):
        raise ValueError("the image provider wrote its picture somewhere else")
    ext = sniff_image(got)
    big = os.path.getsize(got) > MAX_IMAGE_BYTES if os.path.isfile(got) else False
    if not ext or big:
        on_usage({"kind": "image", "images": 0, "in": 0, "out": 0, "exact": True, "outcome": "failed"})
        raise ValueError(f"the image is over {MAX_IMAGE_BYTES // 1_000_000}MB" if ext else
                         "the image model returned something that is not a JPEG, PNG or WebP")
    on_usage({"kind": "image", "images": 1, "in": 0, "out": 0, "exact": True})
    tmp = got
    n, fname = 1, f"img-{target_id}-{stamp}{ext}"
    while os.path.exists(os.path.join(images_dir, fname)):
        n += 1
        fname = f"img-{target_id}-{stamp}-{n}{ext}"
    final = os.path.join(images_dir, fname)
    os.replace(tmp, final)
    created.remove(tmp)
    created.append(final)
    side = sidecar_path(images_dir, fname)
    with open(side, "w", encoding="utf-8") as f:
        json.dump({"description": desc, "alt": alt, "caption": caption, "for": target_id,
                   "model": getattr(images, "label", ""), "ts": time.time()}, f, indent=1)
    created.append(side)
    log("ok", f"#{target_id}: picture saved as {fname} ({os.path.getsize(final) // 1024}KB)")
    md = figure_markdown(pack, fname, alt, caption)
    problems = check_markdown(md, pack)
    if problems:
        raise ValueError("; ".join(problems))
    return md


def _draw_chart(pack, target_id, raw, images_dir, log, block_event, created, sources=()):
    """Draw one chart from the writer's CHART block and return the figure Markdown for it.
    S-numbers in its SOURCE line become the sites they stand for. Raises ValueError with
    the reason the author sees if anything is wrong."""
    if not pack.figure_component():
        raise ValueError(f"the design {pack.name} has no figure component to hold a chart")
    raw = dict(raw)
    raw["source"], used = research.cite(raw.get("source", ""), sources)
    if "memory, not checked" in raw["source"] and sources:
        log("warn", f"#{target_id}: the chart cited a source number research never gave; "
                    f"its Source line says the numbers are unchecked")
    try:
        spec = charts.check_spec(raw)
    except charts.ChartError as e:
        raise ValueError(f"the chart's data was not usable: {e}")
    spec["sources"] = used
    block_event(target_id, "drawing")
    log("info", f"#{target_id}: drawing a {spec['type']} chart ({len(spec['labels'])} rows)")
    os.makedirs(images_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    n, fname = 1, f"chart-{target_id}-{stamp}.png"
    while os.path.exists(os.path.join(images_dir, fname)):
        n += 1
        fname = f"chart-{target_id}-{stamp}-{n}.png"
    final = os.path.join(images_dir, fname)
    fd, tmp = tempfile.mkstemp(dir=images_dir, prefix=".drawing-", suffix=".png")
    os.close(fd)
    created.append(tmp)
    try:
        charts.draw(spec, tmp, list((pack.accents or {}).values()))
    except ImportError:
        raise ValueError("drawing charts needs matplotlib: run  uv sync  (or pip install matplotlib)")
    except Exception as e:
        raise ValueError(f"the chart could not be drawn: {type(e).__name__}: {e}")
    os.replace(tmp, final)
    created.remove(tmp)
    created.append(final)
    side = sidecar_path(images_dir, fname)
    with open(side, "w", encoding="utf-8") as f:
        json.dump({"chart": spec, "alt": spec["alt"], "caption": spec["caption"], "for": target_id,
                   "sources": spec["sources"], "model": "slopmill chart", "ts": time.time()}, f, indent=1)
    created.append(side)
    log("ok", f"#{target_id}: chart saved as {fname} ({os.path.getsize(final) // 1024}KB)")
    md = figure_markdown(pack, fname, spec["alt"], spec["caption"])
    problems = check_markdown(md, pack)
    if problems:
        raise ValueError("; ".join(problems))
    return md


def generate(pack, meta, blocks, targets, llm, *, workdir, direction="", cancel=None,
             log=lambda level, text: None, block_event=lambda bid, status, detail="": None,
             voice=None, images=None, images_dir=None, on_usage=lambda u: None, created=None,
             image_dirs=None, searcher=None):
    """Write a draft for each prompt in targets. Returns (new blocks, note, failures).
    New pictures are drawn into images_dir; notes of earlier ones are looked up in
    image_dirs (default: images_dir alone).

    `created` (a list) collects every file drawn during the pass, so a caller that does not
    commit can remove them."""
    created = created if created is not None else []
    by_id = {b.id: b for b in blocks}
    prompts = [by_id[t] for t in targets if t in by_id and by_id[t].type == "prompt"]
    failures = {}
    if images is None or not pack.figure_component():
        why = ("no image generator is configured on this server (see docs/PROVIDERS.md)"
               if images is None else f"the design {pack.name} has no figure component")
        for p in [p for p in prompts if is_picture_prompt(p.text)]:
            failures[p.id] = [why]
            block_event(p.id, "failed", why)
            log("warn", f"#{p.id}: not drawn: {why}")
    if not pack.figure_component():
        why = f"the design {pack.name} has no figure component to hold a chart"
        for p in [p for p in prompts if is_chart_prompt(p.text)]:
            failures[p.id] = [why]
            block_event(p.id, "failed", why)
            log("warn", f"#{p.id}: not drawn: {why}")
    prompts = [p for p in prompts if p.id not in failures]
    if not prompts:
        return blocks, "", failures
    ids = ", ".join(f"#{p.id}" for p in prompts)
    ask = (f"Write the text for these PROMPT blocks: {ids}. Each answer replaces its "
           f"prompt in the issue, so it must read on from the block before it and into the "
           f"block after it. Leave every PROSE block exactly as it is.")
    pics = [p.id for p in prompts if is_picture_prompt(p.text)]
    if pics:
        ask += f"\nThese are PICTURE prompts: answer each with an IMAGE block: {', '.join('#' + i for i in pics)}."
    graphs = [p.id for p in prompts if is_chart_prompt(p.text)]
    if graphs:
        ask += f"\nThese are CHART prompts: answer each with a CHART block: {', '.join('#' + i for i in graphs)}."
    if direction.strip():
        ask += f"\n\nGeneral direction from the author:\n{direction.strip()}"
    for p in prompts:
        block_event(p.id, "writing")
    wanted = {p.id: p.text for p in prompts
              if research.wants_research(p.text, chart=is_chart_prompt(p.text))}
    sources, found, status = [], None, "off"
    if wanted and searcher is not None:
        room = _room(llm, pack, voice, images, meta, blocks, ask, image_dirs or images_dir)
        sources, found, status = _research(llm, wanted, searcher, room, meta, blocks, workdir, cancel,
                                           log, on_usage, image_dirs or images_dir)
    if found:
        ask += (f"\n\nRESEARCHED: {', '.join('#' + i for i in wanted if any(i in s['for'] for s in sources))}."
                f" SOURCES.md holds what was found for them.")
    ask += research.nothing_found(wanted, status)
    ask += research.not_researched(list(wanted)[research.MAX_ITEMS:] if searcher is not None else [])
    reply = _call(llm, pack, voice, images, meta, blocks, ask, workdir, cancel, log, on_usage,
                  image_dirs or images_dir, extra_files=[found] if found else (),
                  extra_rules=research.USE_SOURCES if found else "")
    got, note = parse_reply(reply)
    pictures = parse_images(reply)
    graphs = charts.parse_charts(reply)
    log("info", f"reply: {len(reply) // 1024 or 1}KB, {len(got)} block(s)"
        + (f", {len(pictures)} picture request(s)" if pictures else "")
        + (f", {len(graphs)} chart(s)" if graphs else ""))
    new_blocks = blocks
    for p in prompts:
        want_picture = is_picture_prompt(p.text)
        want_chart = is_chart_prompt(p.text)
        if p.id in graphs and pack.figure_component() and (want_chart or want_picture or p.id not in got):
            try:
                md = _draw_chart(pack, p.id, graphs[p.id], images_dir, log, block_event, created,
                                 sources)
            except ValueError as e:
                failures[p.id] = [str(e)]
                block_event(p.id, "failed", str(e))
                log("warn", f"#{p.id}: chart not drawn: {e}")
                continue
            new_blocks = _put_draft(new_blocks, p, md)
            block_event(p.id, "done")
            continue
        if want_chart:
            why = ("the model wrote text instead of a chart" if p.id in got
                   else "the reply had no chart for it")
            failures[p.id] = [why]
            block_event(p.id, "failed", why)
            log("warn", f"#{p.id}: chart not drawn: {why}")
            continue
        if p.id in pictures and (want_picture or p.id not in got):
            try:
                md = _draw(images, pack, p.id, pictures[p.id], images_dir, cancel, log,
                           block_event, on_usage, created)
            except ValueError as e:
                failures[p.id] = [str(e)]
                block_event(p.id, "failed", str(e))
                log("warn", f"#{p.id}: not drawn: {e}")
                continue
            new_blocks = _put_draft(new_blocks, p, md)
            block_event(p.id, "done")
            continue
        if want_picture:
            why = ("the model wrote text instead of a picture request" if p.id in got
                   else "the reply had no picture request for it")
            failures[p.id] = [why]
            block_event(p.id, "failed", why)
            log("warn", f"#{p.id}: not drawn: {why}")
            continue
        md = clean_markdown(got.get(p.id, ""))
        problems = ["the reply had no block for it"] if p.id not in got else check_markdown(md, pack)
        if not problems and media_refs(md, pack):
            problems = ["the reply used image markup; ask for a picture with an Image: prompt"]
        if problems:
            failures[p.id] = problems
            block_event(p.id, "failed", "; ".join(problems))
            log("warn", f"#{p.id}: not written: {'; '.join(problems)}")
            continue
        new_blocks = _put_draft(new_blocks, p, md)
        block_event(p.id, "done")
        log("ok", f"#{p.id}: {len(md.split())} words")
    return new_blocks, note, failures


def _put_draft(blocks, prompt, md):
    h = doc.prompt_hash(prompt.text)
    out, placed = [], False
    existing = next((b for b in blocks if b.type == "draft" and b.attrs.get("for") == prompt.id), None)
    for b in blocks:
        if existing is not None and b is existing:
            out.append(doc.Block("draft", b.id, md, {"for": prompt.id, "prompt": h}))
            placed = True
            continue
        out.append(b)
        if existing is None and b is prompt:
            taken = {x.id for x in blocks if x.id}
            out.append(doc.Block("draft", new_id(taken), md, {"for": prompt.id, "prompt": h}))
            placed = True
    assert placed
    return out


def new_id(taken):
    from .ids import new_id as _new
    return _new(set(taken))


def revise(pack, meta, blocks, comments, general, llm, *, workdir, cancel=None,
           log=lambda level, text: None, block_event=lambda bid, status, detail="": None,
           voice=None, images=None, images_dir=None, on_usage=lambda u: None, created=None,
           image_dirs=None, searcher=None):
    """One pass over every queued comment.
    Returns (new blocks, proposals, note, failures, ids of blocks that got a valid answer).

    Drafts are replaced in place. Prose and components come back as proposals: the text
    the model would put there, for the author to accept or reject.
    """
    created = created if created is not None else []
    by_id = {b.id: b for b in blocks}
    named = [c["block"] for c in comments if c.get("block") in by_id]
    targets = []
    for bid in named:
        if bid not in targets:
            targets.append(bid)
    lines = []
    for i, c in enumerate(comments, 1):
        b = by_id.get(c.get("block"))
        where = f"#{c['block']} ({b.type})" if b else "the issue"
        quote = f' on the words "{c["quote"].strip()}"' if c.get("quote") else ""
        if c.get("quote") and c.get("nth"):
            quote += f" (where they appear for the {_ordinal(int(c['nth']) + 1)} time in that block)"
        lines.append(f"{i}. {where}{quote}: {c['note'].strip()}")
    ask = "The author marked up the issue. Revise it.\n"
    if lines:
        ask += "\nComments:\n" + "\n".join(lines) + "\n"
    if general.strip():
        ask += f"\nGeneral direction:\n{general.strip()}\n"
    if targets:
        ask += (f"\nReturn a block for each of: {', '.join('#' + t for t in targets)}."
                f" You may also return other DRAFT blocks if the direction needs it.")
    else:
        ask += "\nReturn only the blocks you change. Prefer DRAFT blocks."
    ask += ("\nFor a DRAFT block, return the whole revised draft. For a PROSE or COMPONENT "
            "block (the author's own words) return your replacement for that one block; the "
            "author will accept or reject it, so change only what the comment asks. "
            "Do not return PROMPT blocks.")
    if images is not None and pack.figure_component():
        ask += ("\nA DRAFT marked PICTURE holds a drawn picture. To change the picture itself, "
                "answer with an IMAGE block for that draft's id. To change only its caption, "
                "return a BLOCK and keep the image line exactly as it is.")
    if pack.figure_component():
        ask += ("\nA DRAFT marked CHART holds a chart slopmill drew from the data shown with it. "
                "To change the chart (its numbers, type, title or caption), answer with a whole "
                "CHART block for that draft's id.")
    for t in targets:
        block_event(t, "writing")
    wanted = {}
    for c in comments:
        if c.get("block") in by_id and research.wants_research(c.get("note", "")):
            wanted[c["block"]] = (wanted.get(c["block"], "") + " " + c["note"].strip()).strip()
    if research.wants_research(general):
        wanted["issue"] = general.strip()
    sources, found, status = [], None, "off"
    if wanted and searcher is not None:
        room = _room(llm, pack, voice, images, meta, blocks, ask, image_dirs or images_dir)
        sources, found, status = _research(llm, wanted, searcher, room, meta, blocks, workdir, cancel,
                                           log, on_usage, image_dirs or images_dir)
    if found:
        ask += ("\nRESEARCHED: " + ", ".join("#" + i if i != "issue" else "the general direction"
                                             for i in wanted) + ". SOURCES.md holds what was found.")
    ask += research.nothing_found(wanted, status)
    ask += research.not_researched(list(wanted)[research.MAX_ITEMS:] if searcher is not None else [])
    reply = _call(llm, pack, voice, images, meta, blocks, ask, workdir, cancel, log, on_usage,
                  image_dirs or images_dir, extra_files=[found] if found else (),
                  extra_rules=research.USE_SOURCES if found else "")
    got, note = parse_reply(reply)
    pictures = parse_images(reply)
    graphs = charts.parse_charts(reply)
    log("info", f"reply: {len(reply) // 1024 or 1}KB, {len(got)} block(s)"
        + (f", {len(pictures)} picture request(s)" if pictures else "")
        + (f", {len(graphs)} chart(s)" if graphs else ""))
    new_blocks, proposals, failures, answered = blocks, [], {}, set()
    for bid, raw in graphs.items():
        b = by_id.get(bid)
        if b is None or b.type != "draft":
            log("warn", f"ignored a chart for #{bid}: only a draft can be redrawn")
            continue
        try:
            md = _draw_chart(pack, bid, raw, images_dir, log, block_event, created, sources)
        except ValueError as e:
            failures[bid] = [str(e)]
            block_event(bid, "failed", str(e))
            log("warn", f"#{bid}: chart not redrawn: {e}")
            continue
        new_blocks = [doc.Block("draft", x.id, md, dict(x.attrs)) if x.id == bid else x
                      for x in new_blocks]
        block_event(bid, "done")
        answered.add(bid)
    for bid, spec in pictures.items():
        b = by_id.get(bid)
        if bid in answered or bid in failures:
            continue
        if b is None or b.type != "draft":
            log("warn", f"ignored a picture request for #{bid}: only a draft can be redrawn")
            continue
        try:
            md = _draw(images, pack, bid, spec, images_dir, cancel, log, block_event, on_usage,
                       created)
        except ValueError as e:
            failures[bid] = [str(e)]
            block_event(bid, "failed", str(e))
            log("warn", f"#{bid}: not redrawn: {e}")
            continue
        new_blocks = [doc.Block("draft", x.id, md, dict(x.attrs)) if x.id == bid else x
                      for x in new_blocks]
        block_event(bid, "done")
        answered.add(bid)
    for bid, raw in got.items():
        b = by_id.get(bid)
        if bid in answered or bid in failures:
            continue
        if b is None or b.type == "prompt" or b.type == "comment":
            log("warn", f"ignored a reply for #{bid}: not a block that can be revised")
            continue
        md = clean_markdown(raw)
        problems = check_markdown(md, pack)
        if not problems and not media_refs(md, pack) <= media_refs(b.text, pack, top_level=b.type != "draft"):
            problems = ["the reply points at an image file this block does not have"]
        if problems:
            failures[bid] = problems
            block_event(bid, "failed", "; ".join(problems))
            log("warn", f"#{bid}: discarded: {'; '.join(problems)}")
            continue
        if b.type == "draft":
            new_blocks = [doc.Block("draft", x.id, md, dict(x.attrs)) if x.id == bid else x
                          for x in new_blocks]
            block_event(bid, "done")
            answered.add(bid)
            log("ok", f"#{bid}: draft revised")
        else:
            if md.strip() == b.text.strip():
                block_event(bid, "done", "no change")
                answered.add(bid)
                log("info", f"#{bid}: the model left it as it was")
                continue
            proposals.append({"block": bid, "base": b.text, "base_type": b.type,
                              "base_attrs": dict(b.attrs), "text": md})
            answered.add(bid)
            block_event(bid, "proposed")
            log("ok", f"#{bid}: change proposed, waiting for you")
    for t in targets:
        if t not in got and t not in pictures and t not in graphs:
            log("warn", f"#{t}: the reply did not include it")
            block_event(t, "failed", "no reply for this block")
    return new_blocks, proposals, note, failures, answered


ASK_MARK = "You are answering the author in the chat beside the editor."
ASK_RULES = ASK_MARK + """
Right now you are not writing the issue. Nothing you write here goes into it.

The attached DOCUMENT.md is the issue as it stands, block by block:
  [PROSE #id] the author's own words · [PROMPT #id] an instruction for what goes there ·
  [DRAFT #id for #prompt] text written earlier from that prompt · [COMPONENT #id] a picture
  or box.

Answer what the author asks, directly and plainly, in friendly sentences. Keep it short
unless they ask for more. The voice described above is for the issue, not for this chat;
but if they ask you for words to use in the issue (a headline, a paragraph, a title),
write those in that voice.

You cannot browse the web or open links. If you give numbers or facts from memory, say
they are from memory and may be out of date.

When a prompt the author could add to their plan would help (they ask for one, or ask for
a picture, a chart or a section), give it on its own like this, one per prompt:
=== PROMPT ===
the prompt, exactly as it should go into the plan
=== END ===
A picture prompt starts with "Image:". A chart prompt starts with "Chart:" and gives the
numbers. At most five.

If they are asking you to change the issue itself (rewrite, shorten, add, remove, fix),
do not write the change here. Say in one sentence what you would change, then end your
answer with this line on its own:
=== CHANGE ===
The editor then offers them a button that sends the request to be done.

Plain text only: no HTML, no "===" lines other than the ones above.
"""
ASK_PROMPT_RE = re.compile(r"^=== PROMPT ===[ \t]*\n(.*?)\n=== END ===[ \t]*$", re.S | re.M)
ASK_CHANGE_RE = re.compile(r"^=== CHANGE ===[ \t]*$", re.M)
MAX_ASK_ANSWER = 8000
MAX_ASK_PROMPTS = 5
MAX_ASK_PROMPT_CHARS = 2000


def parse_answer(text):
    """(answer, [suggested prompts], change?) from a chat reply."""
    text = str(text or "")
    prompts = [p.strip()[:MAX_ASK_PROMPT_CHARS] for p in ASK_PROMPT_RE.findall(text)
               if p.strip()][:MAX_ASK_PROMPTS]
    change = bool(ASK_CHANGE_RE.search(text))
    answer = ASK_CHANGE_RE.sub("", ASK_PROMPT_RE.sub("", text))
    answer = re.sub(r"\n{3,}", "\n\n", answer).strip()
    if len(answer) > MAX_ASK_ANSWER:
        answer = answer[:MAX_ASK_ANSWER].rstrip() + "…"
    if not answer and not prompts:
        answer = "(the model sent an empty answer)"
    return answer, prompts, change


def ask(pack, meta, blocks, question, history, llm, *, workdir, cancel=None,
        log=lambda level, text: None, voice=None, on_usage=lambda u: None, image_dirs=None,
        searcher=None):
    """Answer the author's chat message about the issue. Writes nothing to the issue.
    history: earlier chat entries ({role, text}), oldest first. Returns (answer, prompts,
    change, sources)."""
    voice_system, _ = voice.voice() if voice is not None else pack.voice()
    system = (voice_system.strip() + "\n\n" + ASK_RULES).strip()
    lines = []
    for m in history[-12:]:
        who = {"user": "Author", "model": "You"}.get(m.get("role"))
        if who and str(m.get("text", "")).strip():
            lines.append(f"{who}: {str(m['text']).strip()[:2000]}")
    prompt = ""
    if lines:
        prompt += "The chat so far, oldest first:\n" + "\n\n".join(lines) + "\n\n"
    prompt += "The author now says:\n" + question.strip()
    sources, found = [], None
    if searcher is not None and research.wants_research(question):
        limit = getattr(llm, "max_request_bytes", MAX_PAYLOAD)
        doc_bytes = len(document_for_model(meta, blocks, image_dirs).encode())
        room = limit - len(system.encode()) - len(prompt.encode()) - doc_bytes - 3 * FRAMING \
            - len(research.USE_SOURCES.encode())
        sources, found, status = _research(llm, {"question": question.strip()}, searcher, room, meta,
                                           blocks, workdir, cancel, log, on_usage, image_dirs)
        prompt += research.nothing_found({"question": 1}, status).replace("#question", "this question")
    if found:
        system += "\n\n" + research.USE_SOURCES.replace("the items marked RESEARCHED", "this question")
        prompt += "\n\n(SOURCES.md holds what slopmill found on the web for this question.)"
    tmp, docfile = _write_document(workdir, meta, blocks, image_dirs)
    reply = _send(llm, system, prompt, [docfile] + ([found] if found else []), tmp, cancel, log,
                  on_usage, "answering in the chat")
    answer, prompts, change = parse_answer(reply)
    return answer, prompts, change, [{"n": s["n"], "title": s["title"], "url": s["url"]} for s in sources]


def _ordinal(n):
    return {1: "first", 2: "second", 3: "third"}.get(n, f"{n}th")


def accept_proposal(blocks, proposal):
    """Put an accepted proposal into the file. Refused if the block changed since the
    proposal was made (the author edited it meanwhile): accepting would clobber that."""
    target = next((b for b in blocks if b.id == proposal["block"]), None)
    if target is None:
        raise ValueError("that block no longer exists")
    if (target.text.strip() != proposal["base"].strip()
            or target.type != proposal.get("base_type", target.type)
            or dict(target.attrs) != proposal.get("base_attrs", dict(target.attrs))):
        raise ValueError("that block changed after the proposal was made")
    _, parsed = doc.parse(proposal["text"] + "\n")
    taken = {b.id for b in blocks if b.id}
    replacement = []
    for i, p in enumerate(parsed):
        if p.type == "comment":
            continue
        ident = target.id if not replacement else new_id(taken)
        taken.add(ident)
        kind = "prose" if p.type == "prose" else p.type
        replacement.append(doc.Block(kind, ident, p.text, dict(p.attrs)))
    if not replacement:
        raise ValueError("the proposal is empty")
    out = []
    for b in blocks:
        out.extend(replacement if b is target else [b])
    return out
