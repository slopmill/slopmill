# SPDX-License-Identifier: MIT
"""Read an issue source file: front matter, then the top-level blocks and their IDs."""
import copy
import re
from collections import Counter
from dataclasses import dataclass, field

import yaml

from . import pandoc
from .errors import CompileError, Problem

ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
# One or more complete comments and nothing else. "(?!-->)" stops a match running from
# the first comment's opening to the last one's close across real HTML in between.
COMMENT_RE = re.compile(r"^\s*(?:<!--(?:(?!-->).)*-->\s*)+$", re.S)
DETACHED_ID_RE = re.compile(r"^\{\s*#[^}]*\}$")
# An attribute set written in the source: {...} not preceded by a backslash.
ATTR_SET_RE = re.compile(r"(?<!\\)\{([^{}\n]*)\}")
COMMENT_SPAN_RE = re.compile(r"<!--.*?-->", re.S)
WRITTEN_ID_RE = re.compile(r"(?:^|\s)#([A-Za-z][A-Za-z0-9_-]*)")


@dataclass
class Source:
    path: str
    text: str
    meta: dict
    body: str          # the text Pandoc sees: front matter blanked, line numbers intact


@dataclass
class TopBlock:
    node: dict         # the Pandoc block, with any {#id} wrapper removed
    id: str = None
    line: int = None   # 1-based source line where the block starts, when Pandoc says
    problems: list = field(default_factory=list)


def split_front_matter(text, path=None):
    """Return (meta, body). The front matter lines become blank lines in the body so
    Pandoc's line numbers still match the file."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            raw = "\n".join(lines[1:i])
            try:
                # BaseLoader: every value is a string. The pack's meta.types converts the
                # ones that are not. SafeLoader would read "number: 023" as octal 19 and
                # "issue_date: 2026-09-26" as a date object.
                meta = yaml.load(raw, Loader=yaml.BaseLoader) or {}
            except yaml.YAMLError as e:
                raise CompileError([Problem(f"front matter is not valid YAML: {e}")], path)
            if not isinstance(meta, dict):
                raise CompileError([Problem("front matter must be a mapping of keys")], path)
            body = "\n" * (i + 1) + "\n".join(lines[i + 1:])
            return meta, body
    raise CompileError([Problem("front matter opened with --- but never closed", 1)], path)


def read_source(path):
    # newline="" keeps \r\n intact, so a stamp can write the file back as it was.
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    if text.startswith("﻿"):
        text = text[1:]
    meta, body = split_front_matter(text, path)
    return Source(path=path, text=text, meta=meta, body=body)


def attr_of(node):
    """(id, classes, [[k, v], ...]) for blocks that carry attributes, else None."""
    t, c = node["t"], node.get("c")
    if t in ("Div", "CodeBlock", "Table", "Figure"):
        return c[0]
    if t == "Header":
        return c[1]
    return None


def _pos_line(node):
    attr = attr_of(node)
    if not attr:
        return None
    for k, v in attr[2]:
        if k == "data-pos":
            m = re.search(r"@(\d+):\d+-", v) or re.match(r"(\d+):\d+-", v)
            if m:
                return int(m.group(1))
    return None


def is_comment(node):
    return (node["t"] == "RawBlock" and node["c"][0] == "html"
            and COMMENT_RE.match(node["c"][1]) is not None)


def _is_id_wrapper(node):
    if node["t"] != "Div":
        return False
    (ident, classes, kvs), children = node["c"]
    return bool(ident) and not classes and not kvs and len(children) == 1


def _plain_text(inlines):
    out = []
    for n in inlines:
        if n["t"] == "Str":
            out.append(n["c"])
        elif n["t"] in ("Space", "SoftBreak"):
            out.append(" ")
        else:
            return None
    return "".join(out)


def _is_inline_comment(node):
    return (node.get("t") == "RawInline" and node["c"][0] == "html"
            and COMMENT_RE.match(node["c"][1]) is not None)


def drop_comments(node):
    """Remove HTML comments at every depth, so a note inside a component is dropped the
    same way as one between blocks. Any other raw HTML stays, to be rejected."""
    if isinstance(node, list):
        return [drop_comments(n) for n in node
                if not (isinstance(n, dict) and (is_comment(n) or _is_inline_comment(n)))]
    if isinstance(node, dict):
        return {k: drop_comments(v) for k, v in node.items()}
    return node


def _unwrap(node):
    """Split an {#id} wrapper off its block. Returns (id, block, problem-or-None)."""
    if _is_id_wrapper(node):
        ident = node["c"][0][0]
        child = node["c"][1][0]
        inner = attr_of(child)
        if inner and inner[0]:
            return ident, child, (
                f"two IDs on one block: #{ident} and #{inner[0]}; keep one")
        if inner is not None:
            # A fenced div or heading can hold the ID itself; move it in.
            child = _with_id(child, ident)
        return ident, child, None
    attr = attr_of(node)
    return (attr[0] or None) if attr else None, node, None


def _with_id(node, ident):
    node = copy.deepcopy(node)
    attr = attr_of(node)
    attr[0] = ident
    return node


def top_blocks(ast, pos_ast=None):
    """Top-level blocks with their IDs and source lines. HTML comments are dropped.

    pos_ast is the same document parsed with +sourcepos. It is only used for line
    numbers, and only when it has the same number of top-level blocks as ast.
    """
    blocks = ast["blocks"]
    pos = pos_ast["blocks"] if pos_ast and len(pos_ast["blocks"]) == len(blocks) else None
    out = []
    for i, node in enumerate(blocks):
        if is_comment(node):
            continue
        line = _pos_line(pos[i]) if pos else None
        ident, block, problem = _unwrap(node)
        block = drop_comments(block)
        tb = TopBlock(node=block, id=ident, line=line)
        if problem:
            tb.problems.append(Problem(problem, line))
        if block["t"] == "Para":
            text = _plain_text(block["c"])
            if text is not None and DETACHED_ID_RE.match(text.strip()):
                tb.problems.append(Problem(
                    f"the ID line {text.strip()} is not attached to anything: put it "
                    f"directly above the block it names, with no blank line between", line))
        out.append(tb)
    return out


def check_ids(tops):
    """Every top-level block needs a unique, well-formed ID."""
    problems, seen = [], {}
    missing = [t for t in tops if not t.id]
    for t in tops:
        if not t.id:
            continue
        if not ID_RE.match(t.id):
            problems.append(Problem(
                f"block ID #{t.id} is not a valid ID (letters, digits, - and _, "
                f"starting with a letter)", t.line))
        if t.id in seen:
            where = f" (first used on line {seen[t.id]})" if seen[t.id] else ""
            problems.append(Problem(f"duplicate block ID #{t.id}{where}", t.line))
        else:
            seen[t.id] = t.line
    if missing:
        lines = [str(t.line) for t in missing if t.line]
        where = f" (lines {', '.join(lines)})" if lines else ""
        problems.append(Problem(
            f"{len(missing)} block{'s have' if len(missing) != 1 else ' has'} no ID{where}: "
            f"run `slopmill ids FILE` to stamp them"))
    return problems


def _all_ids(node, out):
    if isinstance(node, dict):
        attr = attr_of(node) if "t" in node else None
        if attr is None and node.get("t") in ("Span", "Link", "Image", "Code"):
            attr = node["c"][0]
        if attr and attr[0]:
            out[attr[0]] += 1
        for v in node.values():
            _all_ids(v, out)
    elif isinstance(node, list):
        for v in node:
            _all_ids(v, out)
    return out


def lost_ids(source, ast):
    """IDs written in the source that Pandoc attached to nothing.

    Pandoc drops some without a word: of two ID lines in a row only the first counts; an
    ID line above a fenced div beats the div's own #id; an ID line at the very end of the
    file vanishes. A comment anchored to a dropped ID would be orphaned, so every ID
    written must be found in the tree, as many times as it was written.
    """
    # Blank out comments first, keeping their newlines so line numbers hold: an ID that
    # appears inside a comment was never written as an ID.
    body = COMMENT_SPAN_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), source.body)
    written = {}
    for i, line in enumerate(body.split("\n"), 1):
        for m in ATTR_SET_RE.finditer(line):
            for idm in WRITTEN_ID_RE.finditer(m.group(1)):
                written.setdefault(idm.group(1), []).append(i)
    found = _all_ids(ast["blocks"], Counter())
    problems = []
    for ident, lines in written.items():
        lost = len(lines) - found[ident]
        if lost <= 0:
            continue
        where = ", ".join(map(str, lines))
        times = f"written {len(lines)} times (lines {where}) but" if len(lines) > 1 else "written but"
        problems.append(Problem(
            f"the ID #{ident} was {times} is attached to "
            f"{'nothing' if not found[ident] else f'only {found[ident]} block(s)'}: two ID "
            f"lines in a row, an ID line above a block that has its own, or an ID line "
            f"with no block after it", lines[-1]))
    return problems


def parse(source):
    ast = pandoc.to_ast(source.body)
    pos_ast = pandoc.to_ast(source.body, sourcepos=True)
    return ast, pos_ast
