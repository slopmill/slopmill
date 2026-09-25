# SPDX-License-Identifier: MIT
"""Run Pandoc and hand back its JSON AST.

Pandoc does all the Markdown parsing. slopmill never tokenises Markdown itself; it only
walks the tree Pandoc returns.
"""
import functools
import json
import os
import re
import shutil
import subprocess

from .errors import EnvironmentProblem

# Off: extensions that turn ordinary prose into something else by accident.
#   tex_math_dollars  "$99 a month ... $7 an hour" must stay text
#   subscript/superscript/strikeout  a stray ~ or ^ must stay text
#   emoji             ":thing:" must stay text
#   definition_lists  a line starting ": " after a paragraph must stay text
#   fancy_lists       "B. B. King said" must not become a list
#   task_lists, footnotes, pipe_tables  not rendered by any pack yet
#   gfm_auto_identifiers  an invented heading ID would hide a missing one
#   implicit_header_references, raw_attribute  unused
OFF = (
    "tex_math_dollars", "subscript", "superscript", "strikeout", "emoji",
    "definition_lists", "fancy_lists", "task_lists", "footnotes", "pipe_tables",
    "gfm_auto_identifiers", "implicit_header_references", "raw_attribute",
)
READER = "commonmark_x" + "".join(f"-{ext}" for ext in OFF)

# The AST shape this code walks. A newer Pandoc that changes it must fail loudly here,
# not render something subtly different.
API_MAJOR_MINOR = (1, 23)
MIN_VERSION = (3, 0)


def _version_of(exe):
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=30).stdout
    except OSError:
        return None
    m = re.match(r"pandoc(?:\.exe)? (\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _bundled():
    """The pandoc that the pypandoc_binary package ships, so nobody has to install one."""
    try:
        import pypandoc
        return pypandoc.get_pandoc_path()
    except (ImportError, OSError):
        return None


@functools.lru_cache(maxsize=1)
def _found():
    """(path, version) of the first pandoc 3.x: $SLOPMILL_PANDOC, the one on the PATH,
    then the bundled one. An older pandoc on the PATH is passed over, not used."""
    seen = []
    for exe in (os.environ.get("SLOPMILL_PANDOC") or os.environ.get("COMPOSITOR_PANDOC"),
                shutil.which("pandoc"), _bundled()):
        if not exe or exe in [s[0] for s in seen]:
            continue
        v = _version_of(exe)
        seen.append((exe, v))
        if v and v >= MIN_VERSION and v[0] == MIN_VERSION[0]:     # a pandoc 4 may change the tree
            return exe, v
    if seen:
        found = ", ".join(f"{e} ({'.'.join(map(str, v)) if v else 'unreadable'})" for e, v in seen)
        raise EnvironmentProblem(f"pandoc 3.x is needed; found {found}")
    raise EnvironmentProblem("pandoc is not installed (need 3.0 or newer): "
                             "`uv run` installs one for you, or see docs/SETUP.md")


def _pandoc():
    return _found()[0]


def version():
    return _found()[1]


def to_ast(text, sourcepos=False):
    """Parse Markdown text into Pandoc's JSON AST.

    --sandbox stops Pandoc touching the filesystem or network on the document's behalf.
    """
    if version() < MIN_VERSION:
        raise EnvironmentProblem(
            f"pandoc {'.'.join(map(str, version()))} is too old (need 3.0 or newer)")
    reader = READER + ("+sourcepos" if sourcepos else "")
    proc = subprocess.run(
        [_pandoc(), "--sandbox", "-f", reader, "-t", "json"],
        input=text, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise EnvironmentProblem(f"pandoc failed: {proc.stderr.strip()[:500]}")
    ast = json.loads(proc.stdout)
    api = tuple(ast.get("pandoc-api-version", [])[:2])
    if api != API_MAJOR_MINOR:
        raise EnvironmentProblem(
            f"pandoc produced AST version {api}; slopmill walks {API_MAJOR_MINOR}. "
            f"Re-check the golden tests before widening this.")
    _drop_wrapper_marks(ast["blocks"])
    return ast


def _drop_wrapper_marks(node):
    """Pandoc 3.2 and later mark the Div or Span that an attribute block wraps around a
    block or an inline ({#id} above a paragraph, **text**{.green}) with wrapper="1"; 3.1
    wrote the same node without it. Removing the mark from every attribute triple gives
    each pandoc version the same tree."""
    if isinstance(node, dict):
        c = node.get("c")
        if isinstance(c, (list, dict)):
            _drop_wrapper_marks(c)
    elif isinstance(node, list):
        if (len(node) == 3 and isinstance(node[0], str) and isinstance(node[1], list)
                and isinstance(node[2], list) and ["wrapper", "1"] in node[2]):
            node[2].remove(["wrapper", "1"])
            return
        for x in node:
            _drop_wrapper_marks(x)
