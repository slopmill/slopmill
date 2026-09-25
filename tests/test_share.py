# SPDX-License-Identifier: MIT
"""SPEC-SHARE: setup, the key file, pandoc selection, start's refusals, the starter design,
and a generate pass over the API-key route with the key kept out of everything."""
import base64
import json
import os
import re
import stat
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import HERE  # noqa: F401
from slopmill import pandoc, providers, setup as setup_mod
from slopmill.cli import main as cli_main
from slopmill.pack import load_pack
from slopmill.server.app import create_app

H = {"x-slopmill": "1"}
KEY = "sk-test-DO-NOT-LEAK-7f3a9c2b1e"
ROOT = os.path.dirname(HERE)


def run_setup(ws, *extra, env=None):
    e = {**os.environ, **(env or {})}
    return subprocess.run([sys.executable, "-m", "slopmill", "setup", "--workspace", str(ws), *extra],
                          capture_output=True, text=True, env=e, stdin=subprocess.DEVNULL, cwd=ROOT)


# ── 3 setup ─────────────────────────────────────────────────────────────────────

def test_3_setup_openai_writes_config_env_voice_and_practice_issue(tmp_path):
    ws = tmp_path / "ws"
    r = run_setup(ws, "--yes", "--name", "The Tuesday Letter", "--author", "Sam",
                  env={"OPENAI_API_KEY": KEY})
    assert r.returncode == 0, r.stderr
    cfg = (ws / "slopmill.toml").read_text()
    assert 'model = "gpt-6-sol"' in cfg and 'model = "gpt-image-2"' in cfg
    assert 'reasoning_effort = "high"' in cfg and KEY not in cfg
    env = ws / ".env"
    assert env.read_text() == f"OPENAI_API_KEY={KEY}\n"
    if os.name == "posix":
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
    brief = (ws / "voices" / "starter" / "writer.md").read_text()
    assert brief.startswith("You are ghost-writing parts of The Tuesday Letter, a newsletter by Sam.")
    assert (ws / "issues" / "001-welcome" / "issue.md").is_file()
    assert json.loads((ws / "issues" / "001-welcome" / "settings.json").read_text()) == {"design": "storybook", "voice": "storybook"}
    assert (ws / "voices" / "storybook" / "VOICE.md").is_file()
    assert KEY not in r.stdout + r.stderr
    # the file it wrote is one the providers accept
    w, i, p = providers.from_config(providers.load_config(str(ws / "slopmill.toml")))
    assert (w.model, w.key_env, w.reasoning_effort, i.model) == ("gpt-6-sol", "OPENAI_API_KEY", "high", "gpt-image-2")


def test_3_setup_again_says_so_and_force_redoes_it(tmp_path):
    ws = tmp_path / "ws"
    assert run_setup(ws, "--yes", env={"OPENAI_API_KEY": KEY}).returncode == 0
    r = run_setup(ws, "--yes", env={"OPENAI_API_KEY": KEY})
    assert r.returncode == 0 and "already set up" in r.stdout
    r = run_setup(ws, "--yes", "--force", "--provider", "anthropic", env={"ANTHROPIC_API_KEY": "sk-ant-x"})
    assert r.returncode == 0
    cfg = (ws / "slopmill.toml").read_text()
    assert 'provider = "anthropic"' in cfg and 'model = "claude-opus-5-5"' in cfg and "[images]\nprovider = \"none\"" in cfg
    assert "OPENAI_API_KEY=" in (ws / ".env").read_text() and "ANTHROPIC_API_KEY=sk-ant-x" in (ws / ".env").read_text()
    assert "from before: OPENAI_API_KEY" in r.stdout      # said, not hidden
    # the practice issue is not added twice
    assert sorted(os.listdir(ws / "issues")) == ["001-welcome"]


def test_3_setup_other_service_with_no_key(tmp_path):
    ws = tmp_path / "ws"
    r = run_setup(ws, "--yes", "--provider", "other", "--base-url", "http://localhost:11434/v1",
                  "--model", "llama-local", env={"WRITER_API_KEY": ""})
    assert r.returncode == 0, r.stderr
    cfg = (ws / "slopmill.toml").read_text()
    assert 'base_url = "http://localhost:11434/v1"' in cfg and 'api_key_env = ""' in cfg
    w, i, _ = providers.from_config(providers.load_config(str(ws / "slopmill.toml")))
    assert w.key_env is None and i is None and not (ws / ".env").exists()


def test_3_setup_without_a_key_refuses_and_writes_nothing(tmp_path):
    ws = tmp_path / "ws"
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    r = subprocess.run([sys.executable, "-m", "slopmill", "setup", "--workspace", str(ws), "--yes"],
                       capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, cwd=ROOT)
    assert r.returncode == 2 and "no API key" in r.stderr
    assert not (ws / "slopmill.toml").exists()


# ── 4 .env and start ────────────────────────────────────────────────────────────

def test_4_env_file_fills_only_what_is_unset(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\nA=from-file\nexport B="quoted value"\nC=\n\nnot a line\n')
    env = {"A": "from-shell"}
    got = setup_mod.load_env(str(p), env)
    assert env == {"A": "from-shell", "B": "quoted value", "C": ""} and sorted(got) == ["B", "C"]


def test_4_start_before_setup_says_what_to_run(tmp_path, capsys):
    assert cli_main(["start", "--workspace", str(tmp_path / "nothing"), "--no-browser"]) == 2
    assert "uv run slopmill setup" in capsys.readouterr().err


def test_4_serve_without_any_model_says_what_to_run(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))          # no writer program anywhere
    assert cli_main(["serve", "--workspace", str(tmp_path / "ws")]) == 2
    assert "slopmill setup" in capsys.readouterr().err


# ── 2 pandoc ────────────────────────────────────────────────────────────────────

def _fake_pandoc(d, version):
    exe = d / "pandoc"
    exe.write_text(f"#!/bin/sh\necho 'pandoc {version}'\n")
    exe.chmod(0o755)
    return str(exe)


@pytest.mark.skipif(os.name != "posix", reason="shell-script stand-ins")
def test_2_an_old_pandoc_on_the_path_is_passed_over(tmp_path, monkeypatch):
    old = _fake_pandoc(tmp_path, "2.9.2")
    newer = tmp_path / "b"
    newer.mkdir()
    good = _fake_pandoc(newer, "3.6")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("SLOPMILL_PANDOC", raising=False)
    monkeypatch.setattr(pandoc, "_bundled", lambda: good)
    pandoc._found.cache_clear()
    try:
        assert pandoc._found() == (good, (3, 6))
        monkeypatch.setattr(pandoc, "_bundled", lambda: None)
        pandoc._found.cache_clear()
        with pytest.raises(pandoc.EnvironmentProblem, match="2.9"):
            pandoc._found()
        assert old
    finally:
        pandoc._found.cache_clear()


# ── 5 the starter design ────────────────────────────────────────────────────────

def test_5_starter_design_exports_and_passes_its_own_check(tmp_path):
    out = tmp_path / "starter.design.toml"
    assert cli_main(["design", "export", "starter", "--out", str(out)]) == 0
    assert cli_main(["design", "check", str(out)]) == 0
    p = load_pack("starter")
    assert {"figure", "note", "closing"} <= set(p.components) and p.components["note"].describe


# ── 6 the API-key route, end to end ─────────────────────────────────────────────

JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")


def fake_openai(seen):
    def handler(req):
        seen.append(req)
        body = json.loads(req.content)
        if req.url.path.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(JPEG).decode()}]})
        prompt = body["messages"][1]["content"].split("\n\nGeneral direction")[0]
        head = prompt.split("=== ATTACHED FILE")[0]
        pics = set(re.findall(r"#([A-Za-z][\w-]*)", head.split("PICTURE prompts")[1])) if "PICTURE prompts" in head else set()
        ids = list(dict.fromkeys(re.findall(r"#([A-Za-z][\w-]*)", head.split("\nThese are PICTURE")[0])))
        out = []
        for i in ids:
            if i in pics:
                out.append(f"=== IMAGE {i} ===\nDESCRIPTION: a desk by a window\nALT: A desk by a window\nCAPTION: Morning.\n=== END ===")
            else:
                out.append(f"=== BLOCK {i} ===\nA short paragraph the model wrote for {i}.\n=== END ===")
        # a provider that echoes the key back in its prose must not leak it either
        out.append(f"=== NOTE ===\nDone. (debug: {req.headers['authorization']})\n=== END ===")
        return httpx.Response(200, json={"choices": [{"message": {"content": "\n".join(out)}}],
                                         "usage": {"prompt_tokens": 1234, "completion_tokens": 56}})
    return handler


def test_6_generate_over_the_api_key_route_keeps_the_key_out(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    assert run_setup(ws, "--yes", env={"OPENAI_API_KEY": KEY}).returncode == 0
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    setup_mod.load_env(str(ws / ".env"))                 # what start does
    seen = []
    t = httpx.MockTransport(fake_openai(seen))
    w, _, _ = providers.from_config(providers.load_config(str(ws / "slopmill.toml")))
    w.transport = t
    images = providers.OpenAIImages("gpt-image-2", key_env="OPENAI_API_KEY", transport=t)
    app = create_app(workspace=str(ws), pack=load_pack("starter"), llm=w, model_label=w.label,
                     token="tok", images=images)
    c = TestClient(app)
    c.cookies.set("slopmill_token", "tok")
    r = c.post("/api/issues/001-welcome/generate", json={}, headers=H)
    assert r.status_code == 200, r.text
    end = time.time() + 20
    while time.time() < end:
        job = c.get("/api/issues/001-welcome").json()["job"]
        if job and job["status"] != "running":
            break
        time.sleep(0.1)
    st = c.get("/api/issues/001-welcome").json()
    assert st["job"]["status"] == "done", st["job"]
    drafts = [b for b in st["blocks"] if b["type"] == "draft"]
    assert len(drafts) == 2 and any("wrote for b-w1p1" in b["text"] for b in drafts)
    assert seen and seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(seen[0].content)["model"] == "gpt-6-sol"
    usage = c.get("/api/usage?slug=001-welcome").json()
    assert usage["issue"]["exact"] and usage["issue"]["in"] >= 1234
    # the key is nowhere a person or a file can see it
    everything = json.dumps(st) + json.dumps(usage)
    for root, _, files in os.walk(ws):
        for f in files:
            if f == ".env":
                continue
            try:
                everything += open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
            except OSError:
                pass
    assert KEY not in everything


# ── the rename (2026-09-24): what was made under the old name still opens ──────

def test_files_from_before_the_rename_still_work(tmp_path, monkeypatch):
    from slopmill.cli import workspace_config
    from slopmill.pack import design_from_text, export_design
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "compositor.toml").write_text("[writer]\nprovider = \"command\"\ncommand = \"x\"\n")
    assert workspace_config(str(ws)) == str(ws / "compositor.toml")
    (ws / "slopmill.toml").write_text("")
    assert workspace_config(str(ws)) == str(ws / "slopmill.toml")
    text = export_design(load_pack("starter")).replace("slopmill-design/1", "compositor-design/1")
    assert design_from_text(text).name == "starter"


def test_the_old_cookie_and_header_still_open_the_editor(tmp_path):
    app = create_app(workspace=str(tmp_path / "ws"), pack=load_pack("starter"), llm=lambda *a, **k: "",
                     model_label="x", token="tok")
    c = TestClient(app)
    c.cookies.set("compositor_token", "tok")
    assert c.get("/api/state").status_code == 200
    assert c.post("/api/issues", json={"title": "Old tab"}, headers={"x-compositor": "1"}).status_code == 200
    assert c.post("/api/issues", json={"title": "No header"}).status_code == 403


def test_2_pandoc_wrapper_marks_are_dropped_everywhere():
    ast = {"blocks": [{"t": "Div", "c": [["b-a", [], [["wrapper", "1"]]], [
        {"t": "Para", "c": [{"t": "Span", "c": [["", ["green"], [["wrapper", "1"]]], [{"t": "Str", "c": "x"}]]}]}]]}]}
    pandoc._drop_wrapper_marks(ast["blocks"])
    assert ast["blocks"][0]["c"][0] == ["b-a", [], []]
    assert ast["blocks"][0]["c"][1][0]["c"][0]["c"][0] == ["", ["green"], []]


@pytest.mark.skipif(os.name != "posix", reason="shell-script stand-ins")
def test_2_a_pandoc_4_on_the_path_is_passed_over_for_the_bundled_3(tmp_path, monkeypatch):
    _fake_pandoc(tmp_path, "4.0")
    b = tmp_path / "b"
    b.mkdir()
    good = _fake_pandoc(b, "3.9")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("SLOPMILL_PANDOC", raising=False)
    monkeypatch.delenv("COMPOSITOR_PANDOC", raising=False)
    monkeypatch.setattr(pandoc, "_bundled", lambda: good)
    pandoc._found.cache_clear()
    try:
        assert pandoc._found() == (good, (3, 9))
    finally:
        pandoc._found.cache_clear()


def test_3_switching_to_a_keyless_service_after_another_one(tmp_path):
    """Round-2 review: with an older key still in .env, the keyless config was skipped."""
    ws = tmp_path / "ws"
    assert run_setup(ws, "--yes", env={"OPENAI_API_KEY": KEY}).returncode == 0
    r = run_setup(ws, "--yes", "--force", "--provider", "other", "--base-url", "http://localhost:11434/v1",
                  "--model", "llama-local", env={"WRITER_API_KEY": ""})
    assert r.returncode == 0, r.stderr
    cfg = (ws / "slopmill.toml").read_text()
    assert 'api_key_env = ""' in cfg and "from before: OPENAI_API_KEY" in r.stdout
    w, _, _ = providers.from_config(providers.load_config(str(ws / "slopmill.toml")))
    assert w.key_env is None
