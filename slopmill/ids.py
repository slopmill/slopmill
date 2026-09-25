# SPDX-License-Identifier: MIT
"""Stamp an ID onto every top-level block that lacks one.

IDs are what a comment anchors to, so they must survive edits and reordering. That rules
out anything computed from position or content: an ID is a name, written into the source
once, and never recomputed.

The stamp is a text edit (a `{#id}` line above the block), placed using Pandoc's source
positions. It is verified before anything is written: re-parsed, every block must carry
the ID it was given and the document must be otherwise identical.
"""
import copy
import os
import secrets
import tempfile

from . import source as src
from .errors import CompileError, Problem

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o, 1/l/i


def new_id(taken):
    while True:
        ident = "b-" + "".join(secrets.choice(ALPHABET) for _ in range(4))
        if ident not in taken:
            taken.add(ident)
            return ident


def _strip_ids(tops):
    """The document with every ID removed, for comparing before and after."""
    out = []
    for tb in tops:
        node = copy.deepcopy(tb.node)
        attr = src.attr_of(node)
        if attr is not None:
            attr[0] = ""
        out.append(node)
    return out


def stamp(path, *, write=True):
    """Returns the list of IDs added. Raises CompileError if it cannot stamp safely."""
    source = src.read_source(path)
    ast, pos_ast = src.parse(source)
    tops = src.top_blocks(ast, pos_ast)
    problems = [p for tb in tops for p in tb.problems] + src.lost_ids(source, ast)
    if problems:
        raise CompileError(problems + [Problem("fix these before stamping IDs")], path)
    missing = [tb for tb in tops if not tb.id]
    if not missing:
        return []
    if any(tb.line is None for tb in missing):
        raise CompileError([Problem(
            "Pandoc did not report where every block starts, so IDs cannot be placed "
            "safely; nothing was written")], path)

    taken = {tb.id for tb in tops if tb.id}
    nl = "\r\n" if "\r\n" in source.text else "\n"
    lines = source.text.split(nl)
    assigned = {}
    for tb in sorted(missing, key=lambda t: t.line, reverse=True):
        ident = new_id(taken)
        assigned[tb.line] = ident
        i = tb.line - 1
        insert = [f"{{#{ident}}}"]
        # A heading can follow a paragraph line directly; the ID line would then read as
        # part of that paragraph. A blank line first keeps it a block of its own.
        if i > 0 and lines[i - 1].strip():
            insert.insert(0, "")
        lines[i:i] = insert
    new_text = nl.join(lines)

    # Verify on the new text before writing a byte.
    new_source = src.Source(path=path, text=new_text, meta=source.meta,
                            body=src.split_front_matter(new_text, path)[1])
    new_ast, new_pos = src.parse(new_source)
    new_tops = src.top_blocks(new_ast, new_pos)
    trouble = []
    if len(new_tops) != len(tops):
        trouble.append(f"block count changed from {len(tops)} to {len(new_tops)}")
    elif _strip_ids(new_tops) != _strip_ids(tops):
        trouble.append("the document changed beyond the IDs")
    else:
        for old, new in zip(tops, new_tops):
            want = old.id or assigned.get(old.line)
            if new.id != want:
                trouble.append(f"block at line {old.line} came out as #{new.id}, not #{want}")
    if any(tb.problems for tb in new_tops):
        trouble.append("stamping introduced a problem: "
                       + "; ".join(str(p) for tb in new_tops for p in tb.problems))
    if trouble:
        raise CompileError([Problem(t) for t in trouble]
                           + [Problem("stamping would change the document; nothing was written")],
                           path)
    if write:
        atomic_write(path, new_text)
    return [assigned[line] for line in sorted(assigned)]


def write_all(out_dir, files):
    """Write several files as close to all-or-nothing as a filesystem allows: every temp
    file is written first, and only then are they renamed into place. A full disk or a
    permission error fails before any output is replaced."""
    staged = []
    try:
        for name, text in files.items():
            staged.append((_stage(os.path.join(out_dir, name), text), os.path.join(out_dir, name)))
    except BaseException:
        for tmp, _ in staged:
            if os.path.exists(tmp):
                os.unlink(tmp)
        raise
    for tmp, path in staged:
        os.replace(tmp, path)


def _stage(path, text):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".slopmill-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        else:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return tmp


def atomic_write(path, text):
    os.replace(_stage(path, text), path)
