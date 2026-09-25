# SPDX-License-Identifier: AGPL-3.0-or-later
"""The web app: Compose, Build and Review over one issue.md.

Every request needs the access token (set once as a cookie from the ?t= link), because
the server writes files and spends model calls, and it listens on the local network.
"""
import asyncio
import hmac
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from sse_starlette.sse import EventSourceResponse

from .. import agent, doc, export, metadata, proof, quickcheck, render
from ..net import MAX_ASSET_BYTES, Fetched, FetchRefused, _check_public, fetch_public  # noqa: F401
from ..providers import sniff_image
from ..designs import DesignError, DesignLibrary
from ..errors import CompileError, EnvironmentProblem
from ..usage import Ledger
from ..voices import VoiceError, VoiceLibrary
from .jobs import MODEL_SLOT, Runner
from .store import Conflict, NotFound, Workspace

ASK_ALLOWANCE = 2_000      # the instructions a pass adds around the author's own words
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "docs")
DOC_NAMES = {"voice-packs": "VOICE-PACKS.md", "design-files": "DESIGN-FILES.md",
             "providers": "PROVIDERS.md"}
COOKIE = "slopmill_token"
MAX_UPLOAD = 15_000_000          # a picture of the author's own
DEMO_MAX_UPLOAD = 2_000_000
MAX_QUESTION = 4000
CLEANING = threading.BoundedSemaphore(1)
DEMO_MAX_ISSUES = 8
OLD_COOKIE = "compositor_token"      # a browser signed in before the rename
# The editor's own script is the only script that may run. A design's templates and CSS
# are someone else's once designs are shared: the preview frame is sandboxed as well, and
# this policy blocks inline script and event-handler attributes in the page and the frame.
CSP = ("default-src 'self'; script-src 'self'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' data: https://fonts.gstatic.com; img-src 'self' data: https:; "
       "media-src 'self' https:; connect-src 'self'; frame-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def load_token(workspace_dir):
    path = os.path.join(workspace_dir, ".token")
    if os.path.exists(path):
        with open(path) as f:
            tok = f.read().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(18)
    os.makedirs(workspace_dir, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    return tok


def err(status, message, **extra):
    return JSONResponse({"error": message, **extra}, status_code=status)


def shipped_packs():
    """The folder designs in packs/ that carry a first-run voice (the examples)."""
    from ..pack import PACKS_DIR, load_folder
    out = []
    for name in sorted(os.listdir(PACKS_DIR)) if os.path.isdir(PACKS_DIR) else []:
        if os.path.isfile(os.path.join(PACKS_DIR, name, "pack.toml")):
            try:
                p = load_folder(os.path.join(PACKS_DIR, name))
            except Exception:           # a broken folder design is reported where it is used
                continue
            if p.voice_system or p.voice_files:
                out.append(p)
    return out


def create_app(*, workspace, pack, llm, model_label="model", lint_argv=None, token=None,
               images=None, plan=None, config_path=None, demo=False, checker=None, research=None):
    """demo=True is the public click-through (slopmill.demo): no design uploads, no pictures
    fetched from other sites, at most DEMO_MAX_ISSUES issues, and the page says it is a demo."""
    designs = DesignLibrary(workspace, pack)
    ws = Workspace(workspace, pack, designs)
    voices = VoiceLibrary(ws.root)
    voices.seed_from_design(pack)       # first run: the design's old [voice] list
    for p in shipped_packs():           # and the example voices that ship in packs/
        voices.seed_from_design(p)
    ledger = Ledger(ws.root)
    token = token or load_token(ws.root)
    runner = Runner()
    asking = {}                  # slug -> when the question being answered was asked
    asking_lock = threading.Lock()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token
    app.state.workspace = ws
    app.state.voices = voices
    app.state.designs = designs
    app.state.ledger = ledger

    # ── access ──────────────────────────────────────────────────────────────────
    @app.middleware("http")
    async def guard(request: Request, call_next):
        t = request.query_params.get("t")
        if t is not None:
            if hmac.compare_digest(t, token):
                resp = RedirectResponse(request.url.path or "/", status_code=303)
                resp.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                                max_age=60 * 60 * 24 * 365)
                return resp
            return HTMLResponse(LOCKED, status_code=401)
        have = request.cookies.get(COOKIE, "") or request.cookies.get(OLD_COOKIE, "")
        if not hmac.compare_digest(have, token):
            if request.url.path.startswith("/api/"):
                return err(401, "open slopmill with the link that has the access token")
            return HTMLResponse(LOCKED, status_code=401)
        if request.method not in ("GET", "HEAD") and "1" not in (
                request.headers.get("x-slopmill"), request.headers.get("x-compositor")):
            return err(403, "missing request header")
        resp = await call_next(request)
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    # ── pages ───────────────────────────────────────────────────────────────────
    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC, "index.html"),
                            headers={"Cache-Control": "no-store"})

    @app.get("/static/{name}")
    def static(name: str):
        if not re.match(r"^[a-z0-9_-]+\.(js|css|svg)$", name):
            return err(404, "not found")
        p = os.path.join(STATIC, name)
        if not os.path.isfile(p):
            return err(404, "not found")
        return FileResponse(p, headers={"Cache-Control": "no-store"})

    # ── helpers ─────────────────────────────────────────────────────────────────
    def issue_or_404(slug):
        try:
            return ws.issue(slug)
        except NotFound:
            return None

    def asset_url_for(iss):
        # Every bare filename goes through /asset/: the local copy if the issue has one,
        # else a cached copy of the published file (the live site forbids embedding it).
        def f(name):
            return f"/api/issues/{iss.slug}/asset/{name}"
        return f

    def design_of(iss):
        return ws.design_of(iss)

    def voice_name_of(iss):
        name = iss.settings().get("voice")
        have = voices.names()
        if name in have:
            return name
        return pack.name if pack.name in have else (have[0] if have else None)

    def voice_of(iss):
        name = voice_name_of(iss)
        return voices.get(name) if name else None

    def images_dir_of(iss):
        """Where this issue's design keeps images: new pictures are drawn into it."""
        return os.path.join(iss.dir, design_of(iss).assets_dir)

    def image_dirs_of(iss):
        """Every image folder the issue has, its design's first: an issue that changed
        design keeps its older pictures in the old design's folder."""
        return [images_dir_of(iss)] + iss.other_image_dirs(design_of(iss).assets_dir)

    def next_request(iss, meta, blocks, extra=""):
        """What the next pass would send, against what the writer takes. `extra` is the
        author's direction and comments; the instructions' own wording is allowed for."""
        limit = getattr(llm, "max_request_bytes", agent.MAX_PAYLOAD)
        try:
            size = agent.request_size(llm, design_of(iss), voice_of(iss), images, meta, blocks,
                                      "x" * ASK_ALLOWANCE + extra, image_dirs_of(iss))
        except (OSError, VoiceError) as e:
            return {"bytes": None, "limit": limit, "error": str(e)}
        return {"bytes": size, "limit": limit, "tokens": size // 4}

    def state_of(iss):
        meta, blocks, rev, _ = iss.load()
        prompts = {b.id: b for b in blocks if b.type == "prompt"}
        sources = {}
        for b in blocks:
            if b.type == "draft":
                p = prompts.get(b.attrs.get("for"))
                b.attrs["stale"] = "1" if p and b.attrs.get("prompt") != doc.prompt_hash(p.text) else ""
                if agent.drawn_from(b, image_dirs_of(iss)):
                    b.attrs["picture"] = "1"
                elif agent.chart_of(b, image_dirs_of(iss)):
                    b.attrs["chart"] = "1"
                    found = agent.chart_of(b, image_dirs_of(iss)).get("sources") or []
                    sources[b.id] = [{"n": x.get("n"), "title": x.get("title", ""), "url": x.get("url", ""),
                                      "snippet_only": bool(x.get("snippet_only"))}
                                     for x in found if isinstance(x, dict)
                                     and str(x.get("url", "")).startswith("https://")]
        job = runner.last(iss.slug)
        d = design_of(iss)
        v = voice_of(iss)
        want = iss.settings().get("design")
        return {"slug": iss.slug, "rev": rev, "meta": meta, "blocks": doc.blocks_json(blocks),
                "review": iss.review(), "job": job.to_json() if job else None,
                "seq": runner.bus(iss.slug).seq, "history": iss.snapshots()[-20:],
                "problems": problems_by_block(iss),
                "design": d.name, "design_missing": bool(want and want != d.name),
                "fields": d.meta_fields, "required": d.meta_required, "accents": d.accents,
                "voice": v.summary() if v else None, "lint": bool(lint_argv) and d is pack,
                "next_request": next_request(iss, meta, blocks), "asking": iss.slug in asking,
                "figure": bool(d.figure_component()), "sources": sources}

    def problems_by_block(iss):
        """{block id: [message]} plus "" for problems that belong to no block."""
        try:
            result = compile_preview(iss)
        except (CompileError, doc.DocError) as e:
            return {"": [str(e)]}
        out = {}
        for b in result.blocks:
            if b.problems:
                out[b.id or ""] = [p.message for p in b.problems]
        claimed = {p.message for b in result.blocks for p in (b.problems or [])}
        loose = [p.message for p in result.problems
                 if p.message not in claimed and not p.message.startswith("front matter")]
        if loose:
            out[""] = loose
        return out

    def compile_preview(iss, text=None):
        if text is None:
            text, _ = iss.read()
        return render.compile_text(text, design_of(iss), path=iss.path, mode="preview",
                                   asset_url=asset_url_for(iss))

    def run_lint(iss, result):
        # The linter is the default design's own tool (it knows that publication's palette
        # and voice rules); an issue drawn with another design is not linted.
        if not lint_argv or design_of(iss) is not pack:
            return None
        rdir = os.path.join(iss.dir, ".runs")
        os.makedirs(rdir, exist_ok=True)
        body = os.path.join(rdir, "lint-body.html")
        with open(body, "w", encoding="utf-8") as f:
            f.write(result.site)      # the site variant: it carries the pack's style block
        subject = str(result.meta.get("subject") or "")
        argv = [a.replace("{body}", body).replace("{subject}", subject) for a in lint_argv]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "findings": [{"level": "error", "text": f"linter did not run: {e}"}]}
        findings = []
        for line in proc.stdout.splitlines():
            level = {"✗": "error", "!": "warn", "·": "info"}.get(line[:1])
            if level:
                findings.append({"level": level, "text": line[1:].strip(), "hint": ""})
            elif line.startswith("    ") and findings:
                findings[-1]["hint"] = line.strip()
        if proc.returncode not in (0, 1) or (proc.returncode and not any(
                f["level"] == "error" for f in findings)):
            tail = " ".join((proc.stderr or proc.stdout or "").strip().splitlines()[-3:])[:300]
            findings.insert(0, {"level": "error", "hint": "",
                                "text": f"the linter itself failed (exit {proc.returncode}): {tail or 'no output'}"})
        return {"ok": proc.returncode == 0, "findings": findings}

    def after_pass(iss, log):
        """Compile the preview and lint it; report both to the run log."""
        result = compile_preview(iss)
        bad = [p for p in result.problems if not str(p).startswith("front matter")]
        pending = [p for p, d in result.prompts.items() if not d]
        log("ok" if not bad else "warn",
            f"compiled: {len([b for b in result.blocks if b.kind != 'prompt'])} blocks, "
            f"{len(bad)} problem(s)" + (f", {len(pending)} prompt(s) still unwritten" if pending else ""))
        lint = run_lint(iss, result)
        if lint is not None:
            errs = [f for f in lint["findings"] if f["level"] == "error"]
            warns = [f for f in lint["findings"] if f["level"] == "warn"]
            log("ok" if not errs else "warn", f"lint: {len(errs)} error(s), {len(warns)} warning(s)")
            iss.update_review(lambda d: d.__setitem__("lint", {**lint, "ts": time.time()}))
        runner.bus(iss.slug).publish("doc", rev=iss.read()[1])
        if plan is not None:
            plan.refresh()        # at most every five minutes (SPEC-V2 19), even after a pass

    def providers_info():
        return {"writer": llm.describe() if hasattr(llm, "describe") else {"provider": "custom", "label": model_label},
                "images": images.describe() if images is not None else None,
                "plan": {"provider": plan.provider} if plan is not None else None,
                "config": os.path.basename(config_path) if config_path else None}

    # ── issues ──────────────────────────────────────────────────────────────────
    @app.get("/api/state")
    def app_state():
        return {"pack": pack.name, "model": model_label, "lint": bool(lint_argv),
                "accents": pack.accents,
                "fields": pack.meta_fields, "required": pack.meta_required,
                "issues": ws.list(), "designs": designs.list(), "voices": voices.names(),
                "default_design": pack.name,
                "default_voice": pack.name if pack.name in voices.names() else None,
                "providers": providers_info(), "pictures": images is not None, "demo": demo,
                "research": research.label if research is not None else None,
                "quickcheck": checker is not None and not demo}

    @app.post("/api/issues")
    async def create_issue(request: Request):
        body = await request.json()
        if demo and len(ws.list()) >= DEMO_MAX_ISSUES:
            return err(422, f"the demo keeps at most {DEMO_MAX_ISSUES} issues")
        try:
            design = designs.get(body.get("design") or pack.name)
        except DesignError as e:
            return err(422, str(e))
        voice = body.get("voice")
        if voice and voice not in voices.names():
            return err(422, f"no voice pack named {voice!r}")
        slug = ws.create(body.get("title", ""), body.get("number"), design=design, voice=voice)
        return {"slug": slug}

    @app.put("/api/issues/{slug}/settings")
    async def issue_settings(slug: str, request: Request):
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        body = await request.json()
        change = {}
        if body.get("design") is not None:
            try:
                designs.get(body["design"])
            except DesignError as e:
                return err(422, str(e))
            change["design"] = body["design"]
        if body.get("voice") is not None:
            if body["voice"] not in voices.names():
                return err(422, f"no voice pack named {body['voice']!r}")
            change["voice"] = body["voice"]
        with iss.lock:
            if runner.current(slug):
                return err(409, "the model is working on this issue; wait for it to finish")
            iss.save_settings(**change)
        runner.bus(slug).publish("doc", rev=iss.read()[1])
        return {"settings": iss.settings()}

    @app.get("/api/issues/{slug}")
    def get_issue(slug: str):
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        try:
            return state_of(iss)
        except (doc.DocError, CompileError) as e:
            return err(422, f"issue.md does not read cleanly: {e}")

    @app.put("/api/issues/{slug}/doc")
    async def save_doc(slug: str, request: Request):
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        body = await request.json()
        if not body.get("base"):
            return err(422, "a save must say which revision it was made against (base)")
        try:
            blocks = doc.blocks_from_json(body.get("blocks", []))
            for b in blocks:
                b.attrs.pop("stale", None)      # all three are shown to the editor, not stored
                b.attrs.pop("picture", None)
                b.attrs.pop("chart", None)
            empty = [b for b in blocks if b.type != "comment" and not b.text.strip()]
            if empty:
                return err(422, "empty blocks are not saved")
            with iss.lock:   # a pass cannot start between this check and the write
                if runner.current(slug):
                    return err(409, "the model is working on this issue; wait for it to finish")
                rev, _ = iss.save(body.get("meta") or {}, blocks, base=body["base"])
        except Conflict as c:
            return err(409, "the issue changed somewhere else; reloading", rev=str(c))
        except doc.DocError as e:
            return err(422, str(e))
        m2, b2, _, _ = iss.load()
        return {"rev": rev, "problems": problems_by_block(iss), "next_request": next_request(iss, m2, b2)}

    def lock_in(iss, meta, blocks, rev, target, text, label):
        """Replace one block with `text` as the author's own words, and save. A draft
        becomes prose (locked: the model only proposes changes to prose) and the prompt it
        came from goes. Several paragraphs become several blocks; the first keeps the ID.
        Called under iss.lock. Returns (rev, ids) or raises ValueError / DocError."""
        # accept_proposal drops HTML comments (right for a model's reply, wrong for the
        # author's own typing), and a box on a component must stay that component.
        _, parsed = doc.parse(text + "\n")
        if any(p.type == "comment" for p in parsed):
            raise ValueError("a note in <!-- --> cannot go inside a block: add it between "
                             "blocks on Plan")
        if any(p.type in ("prompt", "draft") for p in parsed):
            raise ValueError("an edit cannot add a prompt or a draft: add prompts on Plan")
        if target.type == "component":
            kinds = [(p.type, p.attrs.get("class")) for p in parsed]
            if kinds != [("component", target.attrs.get("class"))]:
                raise ValueError(f"this is a {target.attrs.get('class') or 'component'} block: "
                                 f"keep its ::: lines, and keep it one block")
        else:
            # Text stays text. A drawn picture's draft is its figure, so that one component
            # (and only that) may come back from a draft that already was one.
            _, was = doc.parse(target.text + "\n") if target.type == "draft" else (None, [])
            allowed = {(p.type, p.attrs.get("class")) for p in was if p.type == "component"}
            if any(p.type != "prose" and (p.type, p.attrs.get("class")) not in allowed
                   for p in parsed):
                raise ValueError("this block is text: a ::: box cannot go inside it")
        replaced = agent.accept_proposal(blocks, {"block": target.id, "base": target.text,
                                                  "text": text})
        before = {id(b) for b in blocks}
        fresh = [b for b in replaced if id(b) not in before]
        # A draft's text parses as prose (or, for a picture, its figure component, which
        # keeps its class), so the pieces are the author's words. Fences typed into the box
        # that would make a prompt or a draft are refused rather than written.
        if any(b.type in ("prompt", "draft") for b in fresh):
            raise ValueError("an edit cannot add a prompt or a draft: add prompts on Plan")
        out = replaced
        if target.type == "draft":
            prompt_id = target.attrs.get("for")
            out = [b for b in out if not (b.type == "prompt" and b.id == prompt_id)]
        iss.snapshot(label)
        new_rev, _ = iss.save(meta, out, base=rev)
        ids = [b.id for b in fresh]

        def stale_fixes(d):
            # What was said about the old words no longer fits the new ones: spelling fixes
            # go stale, and a proposal for the block can no longer be applied.
            for f in (d.get("proof") or {}).get("fixes") or []:
                if f.get("block") in ids:
                    f["stale"] = True
            for p in d.get("proposals") or []:
                if p.get("block") in ids and p.get("status") == "pending":
                    p["status"] = "stale"
                    p["reason"] = "you edited this block yourself"
        if label == "edit":
            iss.update_review(stale_fixes)
        return new_rev, [i for i in ids if i]

    @app.post("/api/issues/{slug}/keep")
    async def keep_draft(slug: str, request: Request):
        """Lock: a draft becomes the author's prose as it stands; its prompt is removed."""
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        body = await request.json()
        with iss.lock:
            if runner.current(slug):
                return err(409, "the model is working on this issue")
            meta, blocks, rev, _ = iss.load()
            if body.get("base") != rev:
                return err(409, "the issue changed; reload and try again")
            draft = next((b for b in blocks if b.id == body.get("draft") and b.type == "draft"), None)
            if not draft:
                return err(404, "no such draft")
            try:
                new_rev, _ = lock_in(iss, meta, blocks, rev, draft, draft.text, "keep")
            except (ValueError, doc.DocError) as e:
                return err(422, str(e))
        runner.bus(slug).publish("doc", rev=new_rev)
        return {"rev": new_rev}

    @app.post("/api/issues/{slug}/blocks/{bid}/edit")
    async def edit_block(slug: str, bid: str, request: Request):
        """The author's own edit of one block, from Draft or Proof (SPEC-STEPS 7-10). The
        check is on the block's text as it was when the box opened, not on the whole file:
        a change elsewhere in the issue does not refuse it."""
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        body = await request.json()
        orig, text = body.get("orig"), body.get("text")
        if not isinstance(orig, str) or not isinstance(text, str):
            return err(422, "say orig and text")
        if not text.strip():
            return err(422, "the text is empty: to remove the block, use Discard or Delete on Plan")
        with iss.lock:
            if runner.current(slug):
                return err(409, "the model is working on this issue; wait for it to finish")
            try:
                meta, blocks, rev, _ = iss.load()
            except (doc.DocError, CompileError) as e:
                return err(422, f"issue.md does not read cleanly: {e}")
            target = next((b for b in blocks if b.id == bid), None)
            if not target:
                return err(404, "that block no longer exists")
            if target.type not in ("prose", "draft", "component"):
                return err(422, f"a {target.type} block is not edited here")
            if target.text.strip() != orig.strip():
                return err(409, "this block changed since you opened it (on another screen or "
                                "computer). Your version is still in the box.",
                           current=target.text)
            if target.text.strip() == text.strip() and target.type != "draft":
                return {"rev": rev, "ids": [bid], "unchanged": True}
            try:
                new_rev, ids = lock_in(iss, meta, blocks, rev, target, text, "edit")
            except (ValueError, doc.DocError) as e:
                return err(422, str(e))
        runner.bus(slug).publish("doc", rev=new_rev)
        return {"rev": new_rev, "ids": ids}

    @app.get("/api/issues/{slug}/preview")
    def preview(slug: str):
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        try:
            result = compile_preview(iss)
        except (CompileError, doc.DocError) as e:
            return err(422, str(e))
        d = design_of(iss)
        return {"rev": iss.read()[1], "html": result.preview, "prompts": result.prompts,
                "problems": [{"line": p.line, "text": p.message} for p in result.problems],
                "lint": iss.review().get("lint") if d is pack else None,
                "design": d.name, "page_css": d.page_css, "site_head": d.site_head}

    def published_copy(iss, name):
        """A picture the design says is published elsewhere: fetched once an hour at most,
        and never in the demo. Returns (path, None) or (None, error response)."""
        d = design_of(iss)
        if demo:          # a public demo never fetches from other sites on a visitor's behalf
            return None, err(404, f"{name} is not in this demo issue")
        cache = os.path.join(iss.dir, ".runs", "asset-cache", d.name)
        cached = os.path.join(cache, name)
        fresh = os.path.isfile(cached) and time.time() - os.path.getmtime(cached) < 3600
        if not fresh:
            try:
                r = fetch_public(d.asset_base + name, allow_private=d.trusted)
            except FetchRefused as e:
                return None, err(403, str(e))
            except Exception as e:
                if os.path.isfile(cached):
                    return cached, None       # stale beats nothing
                return None, err(502, f"could not fetch {name}: {e}")
            if r.status_code != 200:
                return None, err(404, f"{name} is not in the issue's {d.assets_dir}/ and "
                                      f"not published at {d.asset_base} ({r.status_code})")
            os.makedirs(cache, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=cache, prefix=".part-")
            with os.fdopen(fd, "wb") as f:
                f.write(r.content)
            os.replace(tmp, cached)
        return cached, None

    @app.get("/api/issues/{slug}/asset/{name}")
    def asset(slug: str, name: str):
        """An image as the editor should show it: the local copy if the issue has one,
        otherwise where the pack says published assets live."""
        iss = issue_or_404(slug)
        if not iss or not re.match(r"^[A-Za-z0-9._-]+$", name) or name.startswith("."):
            return err(404, "not found")
        p = iss.image_path(name)
        if p:
            return FileResponse(p)
        path, problem = published_copy(iss, name)
        return FileResponse(path) if path else problem

    @app.get("/api/issues/{slug}/export/{kind}")
    def export_issue(slug: str, kind: str):
        """The finished issue as a file to take away (SPEC-EXPORT)."""
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")

        def picture(name):
            if not re.match(r"^[A-Za-z0-9._-]+$", name) or name.startswith("."):
                return None
            p = iss.image_path(name) or published_copy(iss, name)[0]
            if not p:
                return None
            try:
                with open(p, "rb") as f:
                    return f.read()
            except OSError as e:
                raise export.ExportRefused(f"Not downloaded. The picture {name} could not be "
                                           f"read: {e.strerror or e}.", status=500)
        text, _ = iss.read()
        try:
            body, media, filename = export.make(kind, text, iss.path, design_of(iss), slug, picture)
        except export.ExportRefused as e:
            return err(e.status, str(e))
        except (doc.DocError, EnvironmentProblem) as e:
            return err(422, str(e))
        return Response(body, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store"})

    @app.get("/api/issues/{slug}/images/{name}")
    def image(slug: str, name: str):
        iss = issue_or_404(slug)
        p = iss.image_path(name) if iss else None
        return FileResponse(p) if p else err(404, "not found")

    # ── model passes ────────────────────────────────────────────────────────────
    def start_pass(iss, kind, work):
        def fn(job, log, block_event):
            label = iss.snapshot(kind)
            job.snapshot = label
            job.start_rev = iss.read()[1]     # what the pass must still find when it writes
            job.created = []          # pictures drawn by this pass; removed unless it writes
            log("info", f"saved the issue as it was ({label}) so this pass can be undone")
            if not MODEL_SLOT.acquire(blocking=False):
                log("info", "waiting for another model call on this machine to finish")
                while not MODEL_SLOT.acquire(timeout=1):
                    if job.cancel.is_set():
                        raise agent.Cancelled()
            try:
                work(job, log, block_event)
            except agent.Cancelled:
                log("warn", "stopped; nothing from this pass was written")
            except agent.LLMError as e:
                iss.chat("system", f"The model call failed: {e}")
                raise           # the runner logs it and marks the pass failed
            except Exception as e:
                iss.chat("system", f"The pass failed: {type(e).__name__}: {e}")
                raise
            finally:
                MODEL_SLOT.release()
                # Keep only pictures the issue now uses: a pass that stopped, failed or
                # discarded a picture leaves no stray files behind.
                used = set()
                if getattr(job, "committed", False):
                    used = agent.image_refs(iss.read()[0])
                for path in job.created:
                    base = os.path.basename(path)
                    if base in used or (base.startswith(".") and base.endswith(".json")
                                        and base[1:-5] in used):
                        continue
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        with iss.lock:
            return runner.start(iss.slug, kind, fn)

    def usage_recorder(iss, job):
        def on_usage(u):
            label = (getattr(images, "label", "images") if u.get("kind") == "image"
                     else getattr(llm, "label", model_label))
            ledger.record(slug=iss.slug, job=job.id, pass_kind=job.kind, call=u.get("kind"),
                          model=label, **{k: u[k] for k in ("in", "out", "exact", "images", "outcome")
                                          if k in u})
            runner.bus(iss.slug).publish("usage")
        return on_usage

    def commit(job, iss, rev_, new_blocks, then=None):
        """Write a pass's result, or nothing. Stop takes the same lock, so a pass is either
        stopped before this point or has written; never both."""
        with iss.lock:
            if job.cancel.is_set():
                raise agent.Cancelled()
            # The pass may have waited for the model slot after its snapshot; an edit made
            # meanwhile is neither in the snapshot nor to be overwritten.
            start = getattr(job, "start_rev", None)
            if start and rev_ != start:
                raise RuntimeError("the issue changed while the pass waited to start; nothing was written")
            if new_blocks is not None:
                cur_meta, _, cur_rev, _ = iss.load()
                if cur_rev != rev_:
                    raise RuntimeError("the issue changed during the pass; nothing was written")
                after, _ = iss.save(cur_meta, new_blocks, base=cur_rev)
                # Undo may only put back the snapshot while the file is still what this
                # pass wrote; anything the author does afterwards moves it on.
                iss.update_review(lambda d: d.__setitem__("last_pass", {
                    "snapshot": getattr(job, "snapshot", None), "after": after,
                    "kind": job.kind, "job": job.id, "ts": time.time()}))
            if then:
                then()
            job.committed = True

    def too_big(iss, meta, blocks, extra=""):
        nr = next_request(iss, meta, blocks, extra)
        if nr.get("bytes") and nr["bytes"] > nr["limit"]:
            return err(422, f"the next request would be {nr['bytes'] // 1024}KB and the writer "
                            f"takes at most {nr['limit'] // 1024}KB: turn off some voice files "
                            f"or shorten the issue")
        return None

    @app.post("/api/issues/{slug}/generate")
    async def generate(slug: str, request: Request):
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        body = await request.json()
        meta, blocks, _, _ = iss.load()
        prompts = [b for b in blocks if b.type == "prompt"]
        drafts = {b.attrs.get("for"): b for b in blocks if b.type == "draft"}
        wanted = body.get("targets")
        if wanted:
            targets = [p.id for p in prompts if p.id in wanted]
        else:
            targets = [p.id for p in prompts
                       if p.id not in drafts or drafts[p.id].attrs.get("prompt") != doc.prompt_hash(p.text)]
        direction = (body.get("direction") or "").strip()
        if targets:
            refused = too_big(iss, meta, blocks, direction)
            if refused:
                return refused
        if not targets:
            with iss.lock:
                job = runner.start(iss.slug, "build", lambda job, log, be: (
                    log("info", "every prompt already has an up-to-date draft"), after_pass(iss, log)))
            return {"job": job.to_json() if job else None, "targets": []}

        def work(job, log, block_event):
            meta_, blocks_, rev_, _ = iss.load()
            prose = sum(1 for b in blocks_ if b.type == "prose")
            log("ok", f"{prose} prose block(s) locked; the model cannot change them")
            log("info", f"{len(targets)} prompt(s) to write · calling {model_label}")
            new_blocks, note, failures = agent.generate(
                design_of(iss), meta_, blocks_, targets, llm, workdir=os.path.join(iss.dir, ".runs"),
                direction=direction, cancel=job.cancel, log=log, block_event=block_event,
                voice=voice_of(iss), images=images, images_dir=images_dir_of(iss),
                image_dirs=image_dirs_of(iss), searcher=research,
                on_usage=usage_recorder(iss, job), created=job.created)
            commit(job, iss, rev_, new_blocks if new_blocks is not blocks_ else None)
            written = len(targets) - len(failures)
            log("ok" if not failures else "warn", f"wrote {written} of {len(targets)} draft(s)")
            if note:
                iss.chat("model", note)
                runner.bus(iss.slug).publish("chat", role="model", text=note)
            after_pass(iss, log)

        job = start_pass(iss, "generate", work)
        if not job:
            return err(409, "the model is already working on this issue")
        if direction:          # only once the pass that reads it has started
            iss.chat("user", direction)
        return {"job": job.to_json(), "targets": targets}

    @app.post("/api/issues/{slug}/revise")
    async def revise(slug: str, request: Request):
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        body = await request.json()
        general = (body.get("general") or "").strip()
        queued = [c for c in iss.review()["comments"] if c.get("status") == "queued"]
        if not queued and not general:
            return err(422, "nothing to send: add a comment or a message first")
        meta0, blocks0, _, _ = iss.load()
        refused = too_big(iss, meta0, blocks0,
                          general + "".join(c.get("note", "") + c.get("quote", "") for c in queued))
        if refused:
            return refused
        ids = [c["id"] for c in queued]

        def work(job, log, block_event):
            meta_, blocks_, rev_, _ = iss.load()
            log("info", f"{len(queued)} comment(s)" + (" and a message" if general else "")
                + f" · calling {model_label}")
            new_blocks, proposals, note, failures, answered = agent.revise(
                design_of(iss), meta_, blocks_, queued, general, llm,
                workdir=os.path.join(iss.dir, ".runs"), cancel=job.cancel, log=log,
                block_event=block_event, voice=voice_of(iss), images=images,
                images_dir=images_dir_of(iss), image_dirs=image_dirs_of(iss),
                on_usage=usage_recorder(iss, job), searcher=research,
                created=job.created)
            def mark(d):
                for c in d["comments"]:
                    if c["id"] not in ids:
                        continue
                    if c.get("block") in answered or not c.get("block"):
                        c["status"] = "sent"
                        c["job"] = job.id
                        c.pop("error", None)
                    else:        # stays queued so it can be sent again
                        c["error"] = "; ".join(failures.get(c.get("block"), ["the model did not answer it"]))
                for p in proposals:
                    d["proposals"].append({"id": uuid.uuid4().hex[:10], "status": "pending",
                                           "job": job.id, "ts": time.time(), **p})
            commit(job, iss, rev_, new_blocks if new_blocks is not blocks_ else None,
                   then=lambda: iss.update_review(mark))
            if proposals:
                log("ok", f"{len(proposals)} change(s) to your own words proposed; accept or reject them")
            if note:
                iss.chat("model", note)
                runner.bus(iss.slug).publish("chat", role="model", text=note)
            after_pass(iss, log)

        job = start_pass(iss, "revise", work)
        if not job:
            return err(409, "the model is already working on this issue")
        if general:
            iss.chat("user", general)
        return {"job": job.to_json()}

    # ── the chat answers (SPEC-CHAT-PICTURES 1-7) ─────────────────────────────────
    def chat_event(slug, entry):
        runner.bus(slug).publish("chat", **{k: v for k, v in entry.items() if k != "ts"})

    @app.post("/api/issues/{slug}/ask")
    async def ask_question(slug: str, request: Request):
        """Ask the writer something. It answers in the chat and changes nothing; editing
        carries on while it thinks. One question at a time per issue."""
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        body = await request.json()
        text = body.get("text") if isinstance(body.get("text"), str) else ""
        text = text.strip()
        if not text:
            return err(422, "type a question first")
        if len(text) > MAX_QUESTION:
            return err(422, f"a question is at most {MAX_QUESTION} characters")
        with asking_lock:
            if slug in asking:
                return err(409, "the model is still answering your last question")
            asking[slug] = time.time()
        try:
            history = [m for m in iss.review().get("chat", []) if m.get("role") in ("user", "model")]
            entry = iss.chat("user", text, kind="ask")
        except Exception:
            with asking_lock:
                asking.pop(slug, None)
            raise
        chat_event(slug, entry)
        job = SimpleNamespace(id="ask-" + uuid.uuid4().hex[:8], kind="ask")

        def work():
            answer = None
            try:
                MODEL_SLOT.acquire()          # behind any pass on this machine, not refused
                try:
                    meta, blocks, _, _ = iss.load()
                    answer = agent.ask(design_of(iss), meta, blocks, text, history, llm,
                                       workdir=os.path.join(iss.dir, ".runs"), voice=voice_of(iss),
                                       on_usage=usage_recorder(iss, job),
                                       image_dirs=image_dirs_of(iss), searcher=research)
                finally:
                    MODEL_SLOT.release()
            except Exception as e:
                why = str(e) if isinstance(e, (agent.LLMError, VoiceError)) else f"{type(e).__name__}: {e}"
                answer = e
                reply = iss.chat("system", f"No answer: {why}", kind="ask-failed")
            else:
                said, prompts, change, sources = answer
                reply = iss.chat("model", said, kind="answer", prompts=prompts, change=change,
                                 asked=text, sources=sources)
            finally:
                with asking_lock:
                    asking.pop(slug, None)
            chat_event(slug, reply)

        threading.Thread(target=work, name=f"ask-{slug}", daemon=True).start()
        return {"asking": True, "id": entry["id"]}

    # ── the author's own picture (SPEC-CHAT-PICTURES 8-11) ───────────────────────
    @app.post("/api/issues/{slug}/pictures")
    async def upload_picture(slug: str, request: Request):
        """The raw bytes of a JPEG, PNG or WebP. The words go in headers, percent-encoded,
        so they stay out of access logs: x-picture-alt, x-picture-caption, x-picture-name.
        base (the revision the browser holds) and after (the block it goes after; empty for
        the top) or replace (a draft it takes the place of) go in the query."""
        from urllib.parse import unquote
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        q, hd = request.query_params, request.headers

        def said(name):
            try:
                return unquote(hd.get(name) or "", errors="strict")
            except UnicodeDecodeError:
                return None
        alt, caption, name = said("x-picture-alt"), said("x-picture-caption"), said("x-picture-name")
        if alt is None or caption is None or name is None:
            return err(400, "the picture's words were not sent as UTF-8")
        alt, caption = " ".join(alt.split()), " ".join(caption.split())
        base, after, replace = q.get("base") or "", q.get("after") or "", q.get("replace") or ""
        if not alt:
            return err(422, "describe the picture in a few words (alt text): it is read out "
                            "to people who cannot see it")
        if len(alt) > 400 or len(caption) > 600:
            return err(422, "the alt text is at most 400 characters and the caption 600")
        d = design_of(iss)
        if not d.figure_component():
            return err(422, f"the design {d.name} has no figure block to hold a picture")
        cap = DEMO_MAX_UPLOAD if demo else MAX_UPLOAD
        too_big = err(413, f"the picture is over {cap // 1_000_000}MB")
        try:
            if int(hd.get("content-length") or 0) > cap:
                return too_big
        except ValueError:
            return err(400, "bad content-length")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > cap:
                return too_big
        if not data:
            return err(422, "no picture was sent")
        folder = images_dir_of(iss)
        stem = re.sub(r"[^a-z0-9]+", "-", os.path.splitext(name)[0].lower()).strip("-")[:40]
        os.makedirs(folder, exist_ok=True)
        # Checked and cleaned before the issue is locked: decoding a big photo takes a
        # moment, and editing should not wait for it.
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".upload-")
        kept = None
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            ext = sniff_image(tmp)
            if not ext:
                return err(422, "that file is not a JPEG, PNG or WebP picture")
            try:     # a phone photo says where it was taken: that is not published
                def clean():
                    with CLEANING:          # one picture decoded at a time: memory stays bounded
                        metadata.strip(tmp, max_pixels=metadata.DEMO_MAX_PIXELS if demo else metadata.MAX_PIXELS)
                await asyncio.to_thread(clean)
            except metadata.MetadataError as e:
                return err(422, str(e))
            with iss.lock:
                if runner.current(slug):
                    return err(409, "the model is working on this issue; wait for it to finish")
                try:
                    meta, blocks, rev, _ = iss.load()
                except (doc.DocError, CompileError) as e:
                    return err(422, f"issue.md does not read cleanly: {e}")
                if base != rev:
                    return err(409, "the issue changed since this page loaded; reload and add it again")
                target = None
                if replace:
                    target = next((b for b in blocks if b.id == replace), None)
                    if target is None or target.type != "draft":
                        return err(409, "that draft no longer exists")
                elif after and not any(b.id == after for b in blocks):
                    return err(409, "the block it was to go after no longer exists")
                stamp = time.strftime("%Y%m%d-%H%M%S")
                n, fname = 1, f"own-{stamp}{'-' + stem if stem else ''}{ext}"
                while os.path.exists(os.path.join(folder, fname)):
                    n += 1
                    fname = f"own-{stamp}{'-' + stem if stem else ''}-{n}{ext}"
                kept = os.path.join(folder, fname)
                os.replace(tmp, kept)
                # typed into a box, so it is words, not Markdown: escaped like the alt text
                md = agent.figure_markdown(d, fname, alt, agent.escape_alt(caption) if caption else "")
                problems = agent.check_markdown(md, d)
                if problems:
                    return err(422, "; ".join(problems))
                _, parsed = doc.parse(md + "\n")
                taken = {b.id for b in blocks if b.id}
                bid = agent.new_id(taken)
                figure = doc.Block("component", bid, parsed[0].text, {"class": parsed[0].attrs.get("class")})
                if target is not None:      # the author's picture in place of a drawn one
                    out = [figure if b is target else b for b in blocks
                           if not (b.type == "prompt" and b.id == target.attrs.get("for"))]
                elif after:
                    out = []
                    for b in blocks:
                        out.append(b)
                        if b.id == after:
                            out.append(figure)
                else:
                    out = [figure] + blocks
                iss.snapshot("picture")
                new_rev, _ = iss.save(meta, out, base=rev)
                kept = None                 # in the issue now: not ours to remove
        except (ValueError, doc.DocError, CompileError) as e:
            return err(422, str(e))
        finally:
            for path in (tmp, kept):
                if path and os.path.exists(path):
                    os.remove(path)
        runner.bus(slug).publish("doc", rev=new_rev)
        return {"rev": new_rev, "id": bid, "name": fname}

    @app.post("/api/issues/{slug}/stop")
    def stop(slug: str):
        job = runner.current(slug)
        iss = issue_or_404(slug)
        if not job or not iss:
            return {"stopped": False}
        with iss.lock:
            if job.committed:
                return {"stopped": False, "reason": "the pass had already written its result"}
            job.cancel.set()
        return {"stopped": True}

    @app.post("/api/issues/{slug}/lint")
    def lint_now(slug: str):
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        with iss.lock:
            job = runner.start(iss.slug, "build", lambda job, log, be: after_pass(iss, log))
        return {"job": job.to_json() if job else None}

    @app.post("/api/issues/{slug}/undo")
    def undo(slug: str):
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        with iss.lock:
            if runner.current(slug):
                return err(409, "the model is working on this issue")
            last = iss.review().get("last_pass")
            if not last or not last.get("snapshot") or last["snapshot"] not in iss.snapshots():
                return err(422, "nothing to undo")
            if iss.read()[1] != last["after"]:
                return err(409, "the issue has changed since the last model pass (an edit, or an "
                                "accepted change); undoing would throw that away too")
            rev = iss.restore(last["snapshot"])
            os.replace(os.path.join(iss.dir, "history", last["snapshot"]),
                       os.path.join(iss.dir, "history", last["snapshot"][:-3] + ".undone"))
            undone_job = last.get("job")

            def unwind(d):
                # What the undone pass did to the review goes too: its comments are queued
                # again and its proposals withdrawn, so the notes match the text on screen.
                d.pop("last_pass", None)
                if not undone_job:
                    return
                if (d.get("proof") or {}).get("job") == undone_job:
                    d.pop("proof", None)          # its fixes are what the undo removed
                for c in d["comments"]:
                    if c.get("job") == undone_job and c.get("status") == "sent":
                        c["status"] = "queued"
                        c.pop("job", None)
                for p in d["proposals"]:
                    if p.get("job") == undone_job and p.get("status") == "pending":
                        p["status"] = "undone"
            iss.update_review(unwind)
        runner.bus(slug).publish("doc", rev=rev)
        return {"rev": rev, "restored": last["snapshot"]}

    # ── comments and proposals ──────────────────────────────────────────────────
    @app.post("/api/issues/{slug}/comments")
    async def add_comment(slug: str, request: Request):
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        body = await request.json()
        note = (body.get("note") or "").strip()
        if not note:
            return err(422, "a comment needs some text")
        try:
            nth = max(0, min(int(body.get("nth") or 0), 1000))
        except (TypeError, ValueError):
            nth = 0
        c = {"id": uuid.uuid4().hex[:10], "block": body.get("block"), "quote": (body.get("quote") or "")[:600],
             "nth": nth, "note": note[:4000], "status": "queued", "ts": time.time()}
        iss.update_review(lambda d: d["comments"].append(c))
        return c

    @app.delete("/api/issues/{slug}/comments/{cid}")
    def delete_comment(slug: str, cid: str):
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        iss.update_review(lambda d: d.__setitem__(
            "comments", [c for c in d["comments"] if not (c["id"] == cid and c["status"] == "queued")]))
        return {"ok": True}

    @app.post("/api/issues/{slug}/proposals/{pid}/{action}")
    def decide(slug: str, pid: str, action: str):
        iss = issue_or_404(slug)
        if not iss or action not in ("accept", "reject"):
            return err(404, "not found")
        with iss.lock:
            if runner.current(slug):
                return err(409, "the model is working on this issue")
            review = iss.review()
            prop = next((p for p in review["proposals"] if p["id"] == pid), None)
            if not prop or prop["status"] != "pending":
                return err(404, "no such pending proposal")
            if action == "accept":
                meta, blocks, rev, _ = iss.load()
                try:
                    new_blocks = agent.accept_proposal(blocks, prop)
                    iss.save(meta, new_blocks, base=rev)
                except (ValueError, doc.DocError) as e:
                    prop["status"] = "stale"
                    prop["reason"] = str(e)
                    iss.save_review(review)
                    return err(409, f"could not accept: {e}")
            prop["status"] = "accepted" if action == "accept" else "rejected"
            iss.save_review(review)
        runner.bus(slug).publish("doc", rev=iss.read()[1])
        return {"ok": True, "rev": iss.read()[1]}

    # ── proof: spelling and grammar ─────────────────────────────────────────────
    @app.post("/api/issues/{slug}/proof")
    def proof_start(slug: str):
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        if runner.current(slug):
            return err(409, "the model is already working on this issue")
        _, blocks, _, _ = iss.load()
        if not proof.checkable(blocks):
            return err(422, "there is no text to check yet")
        if proof.duplicate_ids(blocks):
            return err(422, proof.duplicate_message(blocks))
        limit = getattr(llm, "max_request_bytes", agent.MAX_PAYLOAD)
        try:
            size = proof.request_size(voice_of(iss), blocks)
        except (OSError, VoiceError) as e:
            return err(422, str(e))
        if size > limit:
            return err(422, f"the check would send {size // 1024}KB and the writer takes at most "
                            f"{limit // 1024}KB: shorten the issue")

        def work(job, log, block_event):
            meta_, blocks_, rev_, _ = iss.load()
            log("info", f"checking spelling and grammar in {len(proof.checkable(blocks_))} "
                        f"block(s) · calling {model_label}")
            new_blocks, fixes, dropped = proof.proofread(
                meta_, blocks_, llm, voice=voice_of(iss), workdir=os.path.join(iss.dir, ".runs"),
                cancel=job.cancel, log=log, on_usage=usage_recorder(iss, job))
            record = {"job": job.id, "ts": time.time(), "fixes": fixes, "skipped": len(dropped)}

            def keep_record():
                # commit() compares revisions only when it writes the file; a check that
                # changed nothing must not report on text that moved on meanwhile either.
                if not fixes and iss.read()[1] != rev_:
                    raise RuntimeError("the issue changed during the check; nothing was recorded")
                iss.update_review(lambda d: d.__setitem__("proof", record))
            commit(job, iss, rev_, new_blocks if fixes else None, then=keep_record)
            after_pass(iss, log)

        job = start_pass(iss, "proof", work)
        if not job:
            return err(409, "the model is already working on this issue")
        return {"job": job.to_json()}

    @app.post("/api/issues/{slug}/quickcheck")
    def quickcheck_start(slug: str):
        """The free check: LanguageTool's suggestions as fixes, no model and no tokens. The
        record, highlights and switching are the AI check's own."""
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        if checker is None or demo:
            return err(422, "the free check is off on this server")
        if runner.current(slug):
            return err(409, "the model is already working on this issue")
        _, blocks, _, _ = iss.load()
        if not proof.checkable(blocks):
            return err(422, "there is no text to check yet")
        if proof.duplicate_ids(blocks):
            return err(422, proof.duplicate_message(blocks))

        def work(job, log, block_event):
            meta_, blocks_, rev_, _ = iss.load()
            log("info", f"checking spelling and grammar with LanguageTool ({checker.url})")
            known = quickcheck.known_words(voice_of(iss), ws.root)
            try:
                new_blocks, fixes, dropped = quickcheck.quick_check(blocks_, checker, known, log)
            except quickcheck.CheckError as e:
                iss.chat("system", f"The free check did not run: {e}")
                raise
            record = {"job": job.id, "ts": time.time(), "fixes": fixes, "skipped": len(dropped),
                      "source": "languagetool"}

            def keep_record():
                if not fixes and iss.read()[1] != rev_:
                    raise RuntimeError("the issue changed during the check; nothing was recorded")
                iss.update_review(lambda d: d.__setitem__("proof", record))
            commit(job, iss, rev_, new_blocks if fixes else None, then=keep_record)
            after_pass(iss, log)

        job = start_pass(iss, "proof", work)
        if not job:
            return err(409, "the model is already working on this issue")
        return {"job": job.to_json()}

    @app.post("/api/issues/{slug}/proof/{fid}")
    async def proof_toggle(slug: str, fid: str, request: Request):
        """Switch one fix: applied=false puts the author's words back, true the fix."""
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        body = await request.json()
        want = body.get("applied")
        if not isinstance(want, bool):
            return err(422, "say applied: true or false")
        with iss.lock:
            if runner.current(slug):
                return err(409, "the model is working on this issue")
            review = iss.review()
            fx = next((f for f in (review.get("proof") or {}).get("fixes") or [] if f["id"] == fid), None)
            if not fx:
                return err(404, "no such fix")
            if fx.get("applied") == want:
                return {"ok": True, "rev": iss.read()[1], "fix": fx}
            meta, blocks, rev, _ = iss.load()
            try:
                new_blocks = proof.toggle(blocks, fx, want)
            except proof.Stale as e:
                fx["stale"] = True
                iss.save_review(review)
                return err(409, str(e), fix=fx)
            new_rev = rev if new_blocks is blocks else iss.save(meta, new_blocks, base=rev)[0]
            fx["applied"] = want
            fx.pop("stale", None)
            iss.save_review(review)
        runner.bus(slug).publish("doc", rev=new_rev)
        return {"ok": True, "rev": new_rev, "fix": fx}

    @app.delete("/api/issues/{slug}/proof")
    def proof_done(slug: str):
        """Clear the highlights; the text stays as it stands."""
        iss = issue_or_404(slug)
        if not iss:
            return err(404, "no such issue")
        with iss.lock:
            if runner.current(slug):
                return err(409, "the model is working on this issue")
            iss.update_review(lambda d: d.pop("proof", None))
        runner.bus(slug).publish("doc")          # other open tabs drop their highlights too
        return {"ok": True}

    # ── voice packs ─────────────────────────────────────────────────────────────
    def voice_or_404(name):
        try:
            return voices.get(name)
        except VoiceError:
            return None

    def voice_json(v):
        limit = getattr(llm, "max_request_bytes", agent.MAX_PAYLOAD)
        return {**v.summary(), "limit": limit}

    @app.get("/api/voices")
    def voice_list():
        return {"voices": [voice_json(voices.get(n)) for n in voices.names()],
                "default": pack.name if pack.name in voices.names() else None}

    @app.post("/api/voices")
    async def voice_create(request: Request):
        body = await request.json()
        try:
            v = voices.create((body.get("name") or "").strip())
        except VoiceError as e:
            return err(422, str(e))
        return voice_json(v)

    @app.get("/api/voices/{name}")
    def voice_get(name: str):
        v = voice_or_404(name)
        return voice_json(v) if v else err(404, "no such voice pack")

    @app.get("/api/voices/{name}/file")
    def voice_read(name: str, path: str):
        v = voice_or_404(name)
        if not v:
            return err(404, "no such voice pack")
        try:
            return {"path": path, "text": v.read(path)}
        except (VoiceError, OSError, UnicodeDecodeError) as e:
            return err(404, str(e))

    @app.put("/api/voices/{name}/file")
    async def voice_write(name: str, request: Request):
        v = voice_or_404(name)
        if not v:
            return err(404, "no such voice pack")
        body = await request.json()
        try:
            v.write(body.get("path"), body.get("text"))
        except VoiceError as e:
            return err(422, str(e))
        return voice_json(v)

    @app.post("/api/voices/{name}/files")
    async def voice_upload(name: str, request: Request):
        v = voice_or_404(name)
        if not v:
            return err(404, "no such voice pack")
        body = await request.json()
        files = body.get("files")
        if not isinstance(files, list) or not files:
            return err(422, "no files were sent")
        added, refused = [], []
        for f in files[:40]:
            try:
                added.append(v.add(f.get("name"), f.get("text"), f.get("role")))
            except (VoiceError, AttributeError) as e:
                refused.append(str(e))
        extra = [str(f.get("name")) if isinstance(f, dict) else "?" for f in files[40:]]
        if extra:
            refused.append(f"{len(extra)} more not added: send at most 40 files at a time "
                           f"({', '.join(extra[:20])}{'…' if len(extra) > 20 else ''})")
        return {**voice_json(v), "added": added, "refused": refused}

    @app.put("/api/voices/{name}/arrange")
    async def voice_arrange(name: str, request: Request):
        v = voice_or_404(name)
        if not v:
            return err(404, "no such voice pack")
        body = await request.json()
        try:
            v.arrange(body.get("files"))
        except VoiceError as e:
            return err(422, str(e))
        return voice_json(v)

    @app.delete("/api/voices/{name}/file")
    def voice_delete(name: str, path: str):
        v = voice_or_404(name)
        if not v:
            return err(404, "no such voice pack")
        try:
            v.delete(path)
        except VoiceError as e:
            return err(422, str(e))
        return voice_json(v)

    # ── designs ─────────────────────────────────────────────────────────────────
    @app.get("/api/designs")
    def design_list():
        return {"designs": designs.list(), "default": pack.name}

    @app.get("/api/designs/{name}/download")
    def design_download(name: str):
        try:
            text = designs.export(name)
        except DesignError as e:
            return err(404, str(e))
        return PlainTextResponse(text, media_type="application/toml; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{name}.design.toml"',
            "Cache-Control": "no-store"})

    @app.post("/api/designs")
    async def design_upload(request: Request):
        if demo:
            return err(403, "uploading designs is off in the demo: install slopmill to use your own")
        body = await request.json()
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return err(422, "no design file was sent")
        try:
            name = await asyncio.to_thread(designs.upload, text)
        except DesignError as e:
            return err(422, str(e))
        return {"name": name, "designs": designs.list()}

    @app.delete("/api/designs/{name}")
    def design_delete(name: str):
        if demo:
            return err(403, "designs cannot be removed in the demo")
        try:
            designs.delete(name)
        except DesignError as e:
            return err(422, str(e))
        return {"designs": designs.list()}

    # ── meter, providers, guides ────────────────────────────────────────────────
    @app.get("/api/usage")
    def usage(slug: str = ""):
        out = ledger.summary(slug or None)
        out["plan"] = plan.snapshot() if plan is not None else None
        return out

    @app.get("/api/docs/{name}")
    def guide(name: str):
        fn = DOC_NAMES.get(name)
        if not fn or not os.path.isfile(os.path.join(DOCS, fn)):
            return err(404, "no such guide")
        with open(os.path.join(DOCS, fn), encoding="utf-8") as f:
            return {"name": name, "text": f.read()}

    # ── chat + events ───────────────────────────────────────────────────────────
    @app.get("/api/issues/{slug}/events")
    async def events(slug: str, request: Request, since: int = 0):
        if not issue_or_404(slug):
            return err(404, "no such issue")
        bus = runner.bus(slug)
        try:   # a browser that reconnects on its own says where it got to
            since = max(since, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            pass

        async def stream():
            seq = since
            idle = 0
            while True:
                if await request.is_disconnected():
                    return
                batch, gap = bus.since(seq)
                if gap:
                    yield {"event": "message", "data": json.dumps({"type": "gap", "seq": 0})}
                for ev in batch:
                    seq = ev["seq"]
                    yield {"event": "message", "id": str(ev["seq"]), "data": json.dumps(ev)}
                if batch:
                    idle = 0
                else:
                    idle += 1
                    if idle % 60 == 0:
                        yield {"event": "ping", "data": "{}"}
                await asyncio.sleep(0.25)
        return EventSourceResponse(stream())

    return app


LOCKED = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>slopmill</title><body style="font:16px system-ui;margin:0;display:grid;place-items:center;
min-height:100vh;background:#f8f5ee;color:#1b2330"><div style="max-width:30em;padding:24px">
<p style="font:500 12px ui-monospace,monospace;letter-spacing:.08em;
color:#1f3a5f">slopmill</p><h1 style="font-size:22px">Open the link with the access code</h1>
<p>This server is private. Use the full link it printed when it started (it ends in
<code>?t=…</code>). You only need it once per browser.</p></div>"""
