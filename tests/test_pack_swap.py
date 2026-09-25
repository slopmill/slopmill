# SPDX-License-Identifier: MIT
"""Acceptance checks 13 and 14: the engine is publication-neutral; Pandoc is sandboxed."""
import glob
import os
import re
import subprocess

from conftest import FIXTURE, HERE, ROOT
from slopmill import pandoc, render, source as src
from slopmill.pack import load_pack

ENGINE = sorted(glob.glob(os.path.join(ROOT, "slopmill", "*.py")))
URL_OK = {"https://", "http://", "https://...", "https://example.com", "https://api.openai.com/v1",
          # setup.py: where a provider's keys are made, and a local Ollama's address
          "https://platform.openai.com/api-keys", "https://console.anthropic.com/settings/keys",
          "http://localhost:11434/v1",
          # quickcheck.py: LanguageTool's free public service (a provider's public API address)
          "https://api.languagetool.org/v2",
          # research.py: DuckDuckGo's plain results page (a search provider's public address)
          "https://html.duckduckgo.com/html/",
          # contact.py: the mail service the site's contact form sends through
          "https://api.resend.com/emails"}
# The engine may name the two designs it ships (the default and the example) and no other.
SHIPPED = {"starter", "storybook"}
OTHER_DESIGNS = sorted(n for n in os.listdir(os.path.join(ROOT, "packs")) if n not in SHIPPED)
FORBIDDEN = [(re.escape(n), "another design's name") for n in OTHER_DESIGNS] + [
    (r"#[0-9a-fA-F]{6}\b", "hex colour"),
    (r"https?://[^\s\"')]*", "URL"),
]


def test_13_engine_has_no_publication_strings():
    assert ENGINE
    hits = []
    for path in ENGINE:
        text = open(path, encoding="utf-8").read()
        for pattern, what in FORBIDDEN:
            for m in re.finditer(pattern, text, re.I):
                # Scheme prefixes used to recognise absolute links, placeholders, the
                # sample issue's example link and a provider's public API address are not
                # publication strings. (Before 2026-09-23 this pattern matched only the
                # scheme, so every URL passed; it now sees the whole address.)
                if what == "URL" and (m.group(0) in URL_OK or "{" in m.group(0)):
                    continue
                hits.append(f"{os.path.basename(path)}: {what} {m.group(0)!r}")
    assert not hits, hits


def test_13_second_pack_renders_the_same_source_differently(tmp_path):
    minimal = load_pack(os.path.join(HERE, "packs", "minimal"))
    wv = load_pack(os.path.join(HERE, "packs", "fixture"))
    body = ("---\nnumber: 1\nslug: one\ntitle: T\nsubject: S\npreview_text: P\nog_image: x.jpg\n"
            "issue_date: D\n---\n\n{#a}\nHello **there**{.green}, [link](https://example.com).\n\n"
            "{#b}\n## Heading\n\n::: {.figure #c}\n![Alt](pic.jpg){light=pic-light.jpg}\n\nCap.\n:::\n")
    p = tmp_path / "issue.md"
    p.write_text(body)
    s = src.read_source(str(p))
    a = render.compile_source(s, wv, check_assets=False)
    # minimal pack takes fewer meta fields; give it its own front matter
    p2 = tmp_path / "min.md"
    p2.write_text("---\ntitle: T\nslug: one\n---\n" + body.split("---\n", 2)[2])
    b = render.compile_source(src.read_source(str(p2)), minimal, check_assets=False)
    assert "#118844" in a.body and "#118844" not in b.body
    assert '<strong class="accent-green">there</strong>' in b.body
    assert '<figure><img src="https://cdn.example.org/pic.jpg" alt="Alt">' in b.body
    assert "<figcaption>Cap.</figcaption>" in b.body
    assert "nl-dark" not in b.body


def test_14_pandoc_runs_sandboxed(monkeypatch):
    seen = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        seen.append(cmd)
        return real(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, "run", spy)
    pandoc.to_ast("Hello.\n")
    parse_calls = [c for c in seen if "-f" in c]
    assert parse_calls and all("--sandbox" in c for c in parse_calls)


def test_14_pack_paths_are_confined(tmp_path):
    import shutil
    import pytest
    from slopmill.errors import EnvironmentProblem
    pack_dir = tmp_path / "evil"
    shutil.copytree(os.path.join(HERE, "packs", "minimal"), pack_dir)
    toml = (pack_dir / "pack.toml").read_text()
    for bad in ('site_head = "/etc/hostname"', 'site_head = "../outside.html"'):
        (tmp_path / "outside.html").write_text("secret")
        (pack_dir / "pack.toml").write_text(toml.replace('[pack]\n', f'[pack]\n{bad}\n'))
        with pytest.raises(EnvironmentProblem, match="outside the pack"):
            load_pack(str(pack_dir))
    (pack_dir / "pack.toml").write_text(toml.replace('[pack]\n', '[pack]\nassets_dir = "../../x"\n'))
    with pytest.raises(EnvironmentProblem, match="assets_dir"):
        load_pack(str(pack_dir))


def test_14_templates_are_sandboxed(tmp_path):
    import shutil
    import pytest
    from jinja2.exceptions import SecurityError
    pack_dir = tmp_path / "evil"
    shutil.copytree(os.path.join(HERE, "packs", "minimal"), pack_dir)
    (pack_dir / "templates" / "p.html").write_text(
        "{{ content.__class__.__mro__[1].__subclasses__() }}")
    p = tmp_path / "i.md"
    p.write_text("---\ntitle: T\nslug: s\n---\n\n{#a}\nx\n")
    with pytest.raises(SecurityError):
        render.compile_source(src.read_source(str(p)), load_pack(str(pack_dir)), check_assets=False)


def test_14_template_symlink_out_of_pack_is_refused(tmp_path):
    import shutil
    import pytest
    from slopmill.errors import EnvironmentProblem
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET")
    pack_dir = tmp_path / "evil"
    shutil.copytree(os.path.join(HERE, "packs", "minimal"), pack_dir)
    (pack_dir / "templates" / "p.html").unlink()
    (pack_dir / "templates" / "p.html").symlink_to(secret)
    p = tmp_path / "i.md"
    p.write_text("---\ntitle: T\nslug: s\n---\n\n{#a}\nx\n")
    with pytest.raises(EnvironmentProblem, match="outside the pack"):
        render.compile_source(src.read_source(str(p)), load_pack(str(pack_dir)), check_assets=False)
