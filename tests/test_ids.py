# SPDX-License-Identifier: MIT
"""Acceptance check 9: slopmill ids."""
import os
import re
import shutil

import pytest

from conftest import FIXTURE, FRONT, needs_022
from slopmill import ids, render, source as src
from slopmill.errors import CompileError


@needs_022
def test_9_strip_then_stamp_022(tmp_path, pack):
    text = open(os.path.join(FIXTURE, "issue.md"), encoding="utf-8").read()
    stripped = re.sub(r"^\{#b-[a-z0-9]+\}\n", "", text, flags=re.M)
    assert stripped.count("{#") == 0
    p = tmp_path / "issue.md"
    p.write_text(stripped, encoding="utf-8")

    with pytest.raises(CompileError, match="70 blocks have no ID"):
        render.compile_source(src.read_source(str(p)), pack, check_assets=False)

    added = ids.stamp(str(p))
    assert len(added) == 70 and len(set(added)) == 70
    stamped = src.read_source(str(p))
    tops = src.top_blocks(*src.parse(stamped))
    assert [t.id for t in tops] == added

    before = render.compile_source(src.read_source(os.path.join(FIXTURE, "issue.md")), pack,
                                   check_assets=False)
    after = render.compile_source(stamped, pack, check_assets=False)
    assert (after.body, after.site, after.email) == (before.body, before.site, before.email)

    # second run is a no-op
    snapshot = p.read_text(encoding="utf-8")
    assert ids.stamp(str(p)) == []
    assert p.read_text(encoding="utf-8") == snapshot


def test_9_heading_right_after_paragraph(tmp_path):
    p = tmp_path / "issue.md"
    p.write_text(FRONT + "A paragraph\n## A heading straight after it\n\nMore.\n", encoding="utf-8")
    added = ids.stamp(str(p))
    tops = src.top_blocks(*src.parse(src.read_source(str(p))))
    assert [t.node["t"] for t in tops] == ["Para", "Header", "Para"]
    assert [t.id for t in tops] == added


def test_9_keeps_existing_ids_and_fenced_divs(tmp_path):
    p = tmp_path / "issue.md"
    p.write_text(FRONT + "{#keep}\nFirst.\n\n::: {.closing}\nQuote.\n:::\n\n- a\n- b\n",
                 encoding="utf-8")
    added = ids.stamp(str(p))
    tops = src.top_blocks(*src.parse(src.read_source(str(p))))
    assert tops[0].id == "keep"
    assert [t.id for t in tops[1:]] == added
    assert tops[1].node["c"][0][1] == ["closing"]


def test_9_refuses_when_source_has_problems(tmp_path):
    p = tmp_path / "issue.md"
    body = FRONT + "{#a}\n{#b}\nTwo IDs on one paragraph.\n"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(CompileError):
        ids.stamp(str(p))
    assert p.read_text(encoding="utf-8") == body


def test_9_refuses_if_stamping_would_change_the_document(tmp_path, monkeypatch):
    p = tmp_path / "issue.md"
    body = FRONT + "One.\n\nTwo.\n"
    p.write_text(body, encoding="utf-8")
    real = src.top_blocks
    calls = {"n": 0}

    def lying(ast, pos_ast=None):
        calls["n"] += 1
        tops = real(ast, pos_ast)
        if calls["n"] == 2:   # the verification parse
            tops = tops[:1]
        return tops
    monkeypatch.setattr(src, "top_blocks", lying)
    with pytest.raises(CompileError, match="nothing was written"):
        ids.stamp(str(p))
    assert p.read_text(encoding="utf-8") == body


def test_9_crlf_preserved(tmp_path):
    p = tmp_path / "issue.md"
    p.write_bytes((FRONT + "One.\n\nTwo.\n").replace("\n", "\r\n").encode())
    ids.stamp(str(p))
    raw = p.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
