# SPDX-License-Identifier: MIT
"""The issue file as a list of blocks the editor can work with, and back again.

issue.md stays the one source of truth. The editor never holds a second copy of the
document's structure: it loads blocks parsed from the file and saves by writing the file,
and every save is re-parsed and must come back as exactly the blocks that were sent.

Block types:
  prose      the author's words: a paragraph, heading, list or quote, with a {#id} line
  prompt     ::: {.prompt #id}   an instruction; never rendered, never consumed
  draft      ::: {.draft #id for=PROMPT prompt=HASH}   text a model wrote from a prompt
  component  any other fenced div (figure, callout...), kept as raw Markdown
  comment    an HTML comment between blocks; no ID, dropped from the output

Pandoc reports where each block starts reliably, but its end positions run on into the
next block's blank line and ID line. So a block's text is cut at the next block's start.
"""
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field

import yaml

from . import pandoc
from . import source as src

FENCE_RE = re.compile(r"^:{3,}\s*$")
ID_LINE_RE = re.compile(r"^\{\s*#[A-Za-z][A-Za-z0-9_-]*\s*\}\s*$")
TYPES = ("prose", "prompt", "draft", "component", "comment")


class DocError(Exception):
    pass


@dataclass
class Block:
    type: str
    id: str = None
    text: str = ""
    attrs: dict = field(default_factory=dict)
    line: int = None

    def to_json(self):
        return asdict(self)


def prompt_hash(text):
    """Fingerprint of a prompt's instruction, so a draft can tell when its prompt changed.
    Whitespace-insensitive: re-wrapping a line is not a new instruction."""
    norm = " ".join(text.split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:8]


def _pos_start(node):
    attr = src.attr_of(node)
    if attr:
        for k, v in attr[2]:
            if k == "data-pos":
                m = re.search(r"@(\d+):\d+-", v) or re.match(r"(\d+):\d+-", v)
                if m:
                    return int(m.group(1))
    return None


def _trim_tail(lines):
    """Drop trailing blank lines and the next block's {#id} line."""
    out = list(lines)
    while out and not out[-1].strip():
        out.pop()
    while out and ID_LINE_RE.match(out[-1]):
        out.pop()
        while out and not out[-1].strip():
            out.pop()
    return out


def _strip_heading_id(text, ident):
    first, _, rest = text.partition("\n")
    m = re.search(r"\s*\{[^{}]*#" + re.escape(ident) + r"(?![A-Za-z0-9_-])[^{}]*\}\s*$", first)
    if m and first.lstrip().startswith("#"):
        first = first[:m.start()]
        return first + ("\n" + rest if rest else "")
    return text


def _inner(lines, what, line):
    if len(lines) < 2 or not FENCE_RE.match(lines[-1]):
        raise DocError(f"line {line}: a {what} block must end with a ::: line")
    body = lines[1:-1]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(body)


def parse_meta(text):
    meta, body = src.split_front_matter(text)
    return meta, body


def parse(text):
    """issue.md text -> (meta, [Block])."""
    meta, body = src.split_front_matter(text)
    ast = pandoc.to_ast(body)
    pos = pandoc.to_ast(body, sourcepos=True)
    if len(ast["blocks"]) != len(pos["blocks"]):
        raise DocError("Pandoc did not report where every block starts")
    lines = body.split("\n")
    starts = [_pos_start(p) for p in pos["blocks"]]
    if any(s is None for s in starts):
        raise DocError("Pandoc did not report where every block starts")
    blocks = []
    for i, node in enumerate(ast["blocks"]):
        start = starts[i]
        stop = starts[i + 1] - 1 if i + 1 < len(starts) else len(lines)
        chunk = _trim_tail(lines[start - 1:stop])
        if src.is_comment(node):
            blocks.append(Block("comment", None, "\n".join(chunk).strip(), {}, start))
            continue
        ident, block, _ = src._unwrap(node)
        attr = src.attr_of(block)
        classes = attr[1] if attr and block["t"] == "Div" else []
        kvs = {k: v for k, v in attr[2] if k != "data-pos"} if attr else {}
        if block["t"] == "Div" and classes[:1] == ["prompt"]:
            blocks.append(Block("prompt", ident, _inner(chunk, "prompt", start), {}, start))
        elif block["t"] == "Div" and classes[:1] == ["draft"]:
            blocks.append(Block("draft", ident, _inner(chunk, "draft", start),
                                {k: kvs[k] for k in ("for", "prompt") if k in kvs}, start))
        elif block["t"] == "Div" and classes:
            blocks.append(Block("component", ident, "\n".join(chunk),
                                {"class": classes[0]}, start))
        else:
            text_ = "\n".join(chunk)
            if ident:
                text_ = _strip_heading_id(text_, ident)
            blocks.append(Block("prose", ident, text_, {}, start))
    return meta, blocks


def _meta_value(v):
    if v is None:
        return None
    if isinstance(v, str) and v.strip() in ("", "~", "null", "Null", "NULL"):
        return None
    if isinstance(v, str) and re.fullmatch(r"0|[1-9][0-9]{0,15}", v):
        return int(v)        # reads back as the same string; written without quotes
    return v


def front_matter_text(text):
    """The front matter exactly as written (delimiters and following blank lines), or ""."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return ""
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            return "\n".join(lines[:j]) + "\n"
    return ""


def serialize(meta, blocks, field_order=(), front=None):
    """(meta, [Block]) -> issue.md text. `front`, when given, is used verbatim as the
    front matter (so saving an unchanged header does not reformat it)."""
    ordered = {}
    for k in field_order:
        if k in meta:
            ordered[k] = _meta_value(meta[k])
    for k, v in meta.items():
        if k not in ordered:
            ordered[k] = _meta_value(v)
    parts = []
    for b in blocks:
        if b.type not in TYPES:
            raise DocError(f"unknown block type {b.type!r}")
        text = (b.text or "").strip("\n")
        if b.type == "comment":
            parts.append(text)
            continue
        if not b.id:
            raise DocError(f"a {b.type} block has no ID")
        if b.type == "prose":
            parts.append(f"{{#{b.id}}}\n{text}")
        elif b.type == "prompt":
            parts.append(f"::: {{.prompt #{b.id}}}\n{text}\n:::")
        elif b.type == "draft":
            extra = "".join(f" {k}={b.attrs[k]}" for k in ("for", "prompt") if b.attrs.get(k))
            parts.append(f"::: {{.draft #{b.id}{extra}}}\n{text}\n:::")
        else:
            first = text.split("\n", 1)[0]
            has_id = re.search(r"#" + re.escape(b.id) + r"(?![A-Za-z0-9_-])", first)
            parts.append(text if has_id else f"{{#{b.id}}}\n{text}")
    if front is None:
        front = ""
        if ordered:
            front = "---\n" + yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True,
                                             width=10_000) + "---\n\n"
    return front + "\n\n".join(parts) + "\n"


def _norm(b):
    return (b.type, b.id, b.text.strip(), {k: v for k, v in b.attrs.items() if v})


def save_text(meta, blocks, field_order=(), front=None):
    """Serialize, then prove the file reads back as exactly these blocks. Text that would
    change the document's structure (a stray ::: line, a blank line inside a paragraph,
    a {#id} typed into prose) is refused here rather than written."""
    text = serialize(meta, blocks, field_order, front)
    try:
        _, back = parse(text)
    except (DocError, src.CompileError) as e:
        raise DocError(f"this change would break the document: {e}")
    want = [_norm(b) for b in blocks]
    got = [_norm(b) for b in back]
    if want != got:
        for i, (w, g) in enumerate(zip(want, got)):
            if w != g:
                raise DocError(
                    f"block {i + 1} ({w[0]} #{w[1]}) would read back as {g[0]} #{g[1]}: "
                    f"check it for a stray ::: line, a {{#...}} line or a blank line")
        raise DocError(f"{len(want)} blocks would read back as {len(got)}: a blank line or "
                       f"a ::: line inside a block splits it")
    return text


def blocks_json(blocks):
    return [b.to_json() for b in blocks]


def blocks_from_json(items):
    out = []
    for it in items:
        if it.get("type") not in TYPES:
            raise DocError(f"unknown block type {it.get('type')!r}")
        attrs = it.get("attrs") or {}
        if not isinstance(attrs, dict):
            raise DocError("attrs must be an object")
        out.append(Block(it["type"], it.get("id"), it.get("text") or "",
                         {str(k): str(v) for k, v in attrs.items() if v is not None}, None))
    return out


def revision(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def dumps(meta, blocks):
    return json.dumps({"meta": meta, "blocks": blocks_json(blocks)})
