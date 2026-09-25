# SPDX-License-Identifier: MIT
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from slopmill import render, source as src  # noqa: E402
from slopmill.pack import load_pack  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "022")
# The design the suite runs against (tests/packs/fixture): every block shape, no real publication.
FIXTURE_PACK = os.path.join(HERE, "packs", "fixture")
# Issue 022 and the design it shipped in are the author's own; the public copy leaves them out.
needs_022 = pytest.mark.skipif(not os.path.isdir(FIXTURE),
                               reason="the author's own issue 022 is not in this copy")

FRONT = """---
number: 99
slug: 099-test
title: Test
subject: Test subject
preview_text: Preview
og_image: x.jpg
issue_date: JANUARY 1, 2027
---

"""


@pytest.fixture(scope="session")
def pack():
    return load_pack(FIXTURE_PACK)


@pytest.fixture
def compile_md(tmp_path, pack):
    """Compile a Markdown body (front matter added) and return the Result."""
    def go(body, front=FRONT, use_pack=None, check_assets=False):
        p = tmp_path / "issue.md"
        p.write_text(front + body, encoding="utf-8")
        return render.compile_source(src.read_source(str(p)), use_pack or pack,
                                     check_assets=check_assets)
    return go

