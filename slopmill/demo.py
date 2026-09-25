# SPDX-License-Identifier: AGPL-3.0-or-later
"""The public click-through demo: `slopmill demo`.

Every visitor gets a private copy of a prefilled workspace, and the writing comes from a
stand-in writer that returns prepared text. No AI model is called and no key exists, so the
demo costs nothing to run and nothing a visitor types leaves the server.

    visitor ──> DemoSite ──> their own create_app(demo=True) on their own copy
                    │           (DemoWriter, DemoImages; sessions expire when idle)
                    └──> the static intro site, for any host that is not demo.*
"""
import asyncio
import collections
import html
import ipaddress
import itertools
import os
import re
import secrets
import shutil
import threading
import time

from .agent import ASK_MARK
from .pack import load_pack
from .providers import Cancelled, Reply

SESSION_COOKIE = "slopmill_token"
WRITE_DELAY = 1.6          # seconds: long enough to see the progress card, short enough not to bore


def _ids(prompt):
    """The blocks a writing pass asks for, which of them are pictures, and which charts."""
    head = prompt.split("\n\nGeneral direction")[0].split("=== ATTACHED FILE")[0]

    def listed(kind):
        m = re.search(r"These are " + kind + r" prompts[^\n]*", head)
        return set(re.findall(r"#([A-Za-z][\w-]*)", m.group(0))) if m else set()
    first = re.split(r"\nThese are (?:PICTURE|CHART)", head)[0]
    ids = list(dict.fromkeys(re.findall(r"#([A-Za-z][\w-]*)", first)))
    return ids, listed("PICTURE"), listed("CHART")


def _chart_rows(text):
    """label-number pairs a visitor typed into a Chart: prompt ("Mon 4, Tue 3", "Q1 10",
    "2019 5" or "apples: 12")."""
    text = re.sub(r"^\s*(chart|graph|plot)\s*:", "", text or "", flags=re.I)
    rows = re.findall(r"([A-Za-z0-9][A-Za-z0-9 '&-]{0,40}?)\s*[:=]?\s+(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)%?", text or "")
    out = []
    for label, num in rows:
        label = label.strip(" -")
        if label.lower() in ("chart", "graph", "plot") or not label:
            continue
        out.append(f"{label[:40]} | {num}")
    return out[:30]          # the most a chart takes (charts.MAX_ROWS)


class DemoWriter:
    """Answers a writing pass the way a model would, from prepared text.

    `prepared` maps a block ID to the text written for it (for the demo issue this is what
    a real model wrote, once). Any other block gets one of the stand-in paragraphs, which
    say plainly that they are stand-ins."""
    kind = "demo"
    label = "demo writer (no AI)"
    max_request_bytes = 400_000

    STAND_IN = [
        "This paragraph was written by the demo's stand-in writer, not by an AI model. With "
        "slopmill installed and your own key, the model would write this from your prompt, in "
        "your voice.",
        "Here the model would have answered your prompt. The demo keeps a few paragraphs ready "
        "instead, so trying it costs nobody anything. Install slopmill to hear your own voice "
        "come back.",
        "Imagine something rather good here, written from exactly what you asked for. That is "
        "what the real thing does; the demo only pretends, and says so.",
    ]
    ANSWER = (
        "This is the demo, so no AI model is connected and I cannot really answer. With slopmill "
        "installed, the model reads your issue and answers questions right here, without "
        "changing anything. It can look things up for you too. Here is the kind of prompt it "
        "might suggest; press Add to Plan to try it:\n"
        "=== PROMPT ===\nChart: honey pots left on the shelf. Monday 4, Tuesday 3, Wednesday 1, Thursday 0\n=== END ===")
    FIXES = [("teh", "the", "spelling"), ("recieve", "receive", "spelling"),
             ("seperate", "separate", "spelling"), ("alot", "a lot", "spelling"),
             ("it's own", "its own", "its/it's")]

    def __init__(self, prepared=None, pictures=None, delay=WRITE_DELAY, prompts=None):
        self.prepared = dict(prepared or {})
        self.pictures = dict(pictures or {})
        # the prompt each prepared answer was written for: a visitor who changes it gets a
        # stand-in, not the old answer to a question they no longer asked
        self.prompts = {k: " ".join(v.split()) for k, v in (prompts or {}).items()}
        self.delay = delay
        self._turn = itertools.count()

    def describe(self):
        return {"provider": "demo", "label": self.label}

    def _wait(self, cancel):
        end = time.time() + self.delay
        while time.time() < end:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            time.sleep(0.05)

    def __call__(self, system, prompt, files, cancel=None, timeout=None):
        self._wait(cancel)
        if system.startswith("You are proofreading"):
            return Reply(self._proof(files), {"in": len(prompt) // 4, "out": 40, "exact": False})
        if ASK_MARK in system:
            text = self.ANSWER
            return Reply(text, {"in": len(prompt) // 4, "out": len(text) // 4, "exact": False})
        ids, pics, graphs = _ids(prompt)
        document = ""
        if files:
            try:
                document = open(files[-1], encoding="utf-8").read()
            except OSError:
                document = ""

        def still_asked(i):
            """The block's own prompt, read from its own section of the document, is still
            word for word the one the prepared answer was written for."""
            want = self.prompts.get(i)
            if want is None:
                return True
            m = re.search(r"^\[PROMPT #" + re.escape(i) + r"\b[^\]]*\]\n(.*?)(?=\n\n\[|\Z)", document, re.S | re.M)
            return bool(m) and " ".join(m.group(1).split()) == want
        out = []
        for i in ids:
            if i in graphs:
                m = re.search(r"^\[PROMPT #" + re.escape(i) + r"\b[^\]]*\]\n(.*?)(?=\n\n\[|\Z)", document, re.S | re.M)
                rows = _chart_rows(m.group(1)) if m else []
                mine = len(rows) >= 2
                rows = rows if mine else ["Monday | 4", "Tuesday | 3", "Wednesday | 1", "Thursday | 0"]
                asked = re.sub(r"^\s*(chart|graph|plot)\s*:\s*", "", m.group(1) if m else "", flags=re.I)
                title = re.split(r"[.:\n]", asked.strip())[0].strip()[:80] or "Your numbers"
                title = title[:1].upper() + title[1:]
                out.append(f"=== CHART {i} ===\nTYPE: column\n"
                           f"TITLE: {title if mine else 'Honey pots on the shelf, by day'}\n"
                           f"UNIT: \nDATA:\n" + "\n".join(rows) +
                           f"\nSOURCE: {'Your prompt' if mine else 'A stand-in: the demo makes nothing up about the world'}\n"
                           f"ALT: A column chart of {len(rows)} values.\n"
                           f"CAPTION: {'' if mine else 'A stand-in chart: put numbers in your prompt and the demo draws those.'}\n"
                           "=== END ===")
                continue
            if i in pics:
                d = (self.pictures.get(i) if still_asked(i) else None) or {
                    "DESCRIPTION": "a honey pot on a sunny windowsill beside an open notebook",
                    "ALT": "A honey pot beside an open notebook on a sunny windowsill",
                    "CAPTION": "A stand-in picture: the demo draws nothing new."}
                out.append(f"=== IMAGE {i} ===\n" + "\n".join(f"{k}: {v}" for k, v in d.items())
                           + "\n=== END ===")
            else:
                text = (self.prepared.get(i) if still_asked(i) else None) \
                    or self.STAND_IN[next(self._turn) % len(self.STAND_IN)]
                out.append(f"=== BLOCK {i} ===\n{text}\n=== END ===")
        note = ("Written from prepared text: this is the demo, so no AI model was called."
                if ids else "Nothing to change.")
        out.append(f"=== NOTE ===\n{note}\n=== END ===")
        text = "\n".join(out)
        return Reply(text, {"in": len(prompt) // 4, "out": len(text) // 4, "exact": False})

    def _proof(self, files):
        document = open(files[-1], encoding="utf-8").read() if files else ""
        found = []
        for m in re.finditer(r"^\[(?:PROSE|DRAFT|COMPONENT) #([\w-]+)[^\]]*\]\n(.*?)(?=\n\n\[|\Z)",
                             document, re.S | re.M):
            for find, rep, why in self.FIXES:
                if len(re.findall(r"\b" + re.escape(find) + r"\b", m.group(2))) == 1:
                    found.append(f"=== FIX #{m.group(1)} ===\nFIND: {find}\nREPLACE: {rep}\n"
                                 f"WHY: {why}\n=== END ===")
        return "\n".join(found) or "=== NONE ==="


class DemoImages:
    """Every picture prompt gets the same prepared picture."""
    kind = "demo"
    label = "demo pictures (no AI)"

    def __init__(self, picture):
        self.picture = picture

    def describe(self):
        return {"provider": "demo", "label": self.label}

    def __call__(self, prompt, out_path, cancel=None):
        time.sleep(0.8)
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        path = os.path.splitext(out_path)[0] + ".jpg"
        shutil.copyfile(self.picture, path)
        return path


class Sessions:
    """One private workspace and app per visitor, made on the first visit and removed after
    `idle` seconds without a request. At most `limit` at once (the least recently used goes
    first) and at most `per_ip` new ones an hour from one address."""

    def __init__(self, make_app, root, template, limit=60, idle=1800, per_ip=20):
        self.make_app, self.root, self.template = make_app, root, template
        self.limit, self.idle, self.per_ip = limit, idle, per_ip
        self.live = collections.OrderedDict()        # id -> [app, last_seen, dir]
        self.sizes = {}                               # id -> (bytes, measured at)
        self.made = collections.defaultdict(list)    # ip -> [times]
        self.lock = threading.Lock()
        os.makedirs(root, exist_ok=True)
        for name in os.listdir(root):                # a restart forgets every session
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)

    def get(self, sid):
        with self.lock:
            s = self.live.get(sid or "")
            if not s or s[0] is None:
                return None
            s[1] = time.time()
            self.live.move_to_end(sid)
            return s[0]

    def new(self, ip):
        now = time.time()
        with self.lock:
            recent = [t for t in self.made[ip] if now - t < 3600]
            if len(recent) >= self.per_ip:
                return None, None
            self.made[ip] = recent + [now]
            self._sweep(now)
            while len(self.live) >= self.limit:
                old, (_, _, d) = self.live.popitem(last=False)
                self.sizes.pop(old, None)
                shutil.rmtree(d, ignore_errors=True)
            sid = secrets.token_urlsafe(24)
            d = os.path.join(self.root, sid)
            self.live[sid] = [None, now, d]          # counted from now: the cap cannot be overshot
        try:
            shutil.copytree(self.template, d)
            app = self.make_app(d, sid)
        except Exception:
            with self.lock:
                self.live.pop(sid, None)
            shutil.rmtree(d, ignore_errors=True)
            raise
        with self.lock:
            if sid in self.live:
                self.live[sid][0] = app
        return sid, app

    def _sweep(self, now):
        for sid in [k for k, (_, seen, _) in self.live.items() if now - seen > self.idle]:
            _, _, d = self.live.pop(sid)
            self.sizes.pop(sid, None)
            shutil.rmtree(d, ignore_errors=True)
        for ip in [k for k, v in self.made.items() if not any(now - t < 3600 for t in v)]:
            del self.made[ip]

    def over_budget(self, sid, budget):
        """True when this visitor's copy has grown past `budget` bytes (voice uploads,
        history, pictures). Measured at most every few seconds per session."""
        with self.lock:
            s = self.live.get(sid or "")
            if not s:
                return False
            d = s[2]
            cached = self.sizes.get(sid)
        if cached and time.time() - cached[1] < 5:
            return cached[0] > budget
        total = 0
        for root, _, files in os.walk(d):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        with self.lock:
            self.sizes[sid] = (total, time.time())
        return total > budget

    def sweep(self):
        with self.lock:
            self._sweep(time.time())


def _cookie(scope, name):
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                key, _, val = part.strip().partition("=")
                if key == name:
                    return val
    return None


# Cloudflare's published ranges (cloudflare.com/ips, 2026-09-24). Its CF-Connecting-IP header
# is only believed when the request reached the origin from one of these.
CLOUDFLARE = [ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32")]


def visitor_ip(scope):
    """The address to count sessions against. The proxy in front (Caddy) sets
    X-Forwarded-For to the address that connected to it; if that is Cloudflare, the real
    visitor is in CF-Connecting-IP. A client-supplied CF-Connecting-IP from anywhere else is
    ignored, so it cannot be varied to dodge the limit."""
    peer = _header(scope, "x-forwarded-for").split(",")[-1].strip() or (scope.get("client") or ("?",))[0]
    try:
        from_cf = any(ipaddress.ip_address(peer) in n for n in CLOUDFLARE)
    except ValueError:
        from_cf = False
    return (_header(scope, "cf-connecting-ip").strip() or peer) if from_cf else peer


def _query(scope, name):
    from urllib.parse import parse_qs
    vals = parse_qs(scope.get("query_string", b"").decode("latin-1")).get(name)
    return vals[0] if vals else None


def _header(scope, name):
    for k, v in scope.get("headers", []):
        if k == name.encode():
            return v.decode("latin-1")
    return ""


async def _send_simple(send, status, body, ctype="text/html; charset=utf-8", headers=()):
    data = body.encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", ctype.encode()), (b"content-length", str(len(data)).encode()),
                            (b"cache-control", b"no-store"), *headers]})
    await send({"type": "http.response.body", "body": data})


BUSY = """<!doctype html><meta charset="utf-8"><title>slopmill demo</title>
<body style="font:16px system-ui;margin:0;display:grid;place-items:center;min-height:100vh;background:Canvas;color:CanvasText;color-scheme:light dark">
<div style="max-width:30em;padding:24px"><h1 style="font-size:22px">The demo is busy</h1>
<p>Too many demos were started from your connection in the last hour. Try again later, or
<a href="{home}">read about slopmill</a>.</p></div>"""


class DemoSite:
    """The ASGI app for the whole site: the intro pages on the main host, and the demo on any
    host starting with `demo.` (or on every host when there are no intro pages)."""

    def __init__(self, sessions, site_dir=None, home_url="/", budget=8_000_000, contact=None):
        self.sessions = sessions
        self.budget = budget
        self.home_url = home_url
        self.static = None
        from .contact import Contact
        self.contact = contact if contact is not None else Contact(
            template=os.path.join(site_dir, "contact", "_result.html") if site_dir else None)
        if site_dir:
            from starlette.applications import Starlette
            from starlette.routing import Mount
            from starlette.staticfiles import StaticFiles
            # Wrapped in an app, so a missing file is a plain 404 rather than an exception.
            self.static = Starlette(routes=[Mount("/", app=StaticFiles(directory=site_dir, html=True))])
        self._sweeper = None

    def _is_demo(self, scope):
        host = _header(scope, "host").split(":")[0].lower()
        return self.static is None or host.startswith("demo.")

    async def _contact(self, scope, receive, send):
        from .contact import MAX_BODY
        body, more = b"", True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
            if len(body) > MAX_BODY:
                body = body[:MAX_BODY + 1]
                break
        status, page, where = await asyncio.to_thread(
            self.contact.handle, body, visitor_ip(scope), _header(scope, "origin"),
            _header(scope, "referer"), _header(scope, "host").split(":")[0])
        if where:
            await send({"type": "http.response.start", "status": 303,
                        "headers": [(b"location", where.encode()), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b""})
            return
        await _send_simple(send, status, page)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    self._sweeper = asyncio.create_task(self._sweep_forever())
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    if self._sweeper:
                        self._sweeper.cancel()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        if not self._is_demo(scope):
            if scope.get("path") in ("/contact", "/contact/") and scope.get("method") == "POST":
                return await self._contact(scope, receive, send)
            return await self.static(scope, receive, send)
        sid = _cookie(scope, SESSION_COOKIE) or _query(scope, "t")
        app = self.sessions.get(sid)
        if app is not None:
            if scope.get("method") not in ("GET", "HEAD") and \
                    await asyncio.to_thread(self.sessions.over_budget, sid, self.budget):
                return await _send_simple(send, 413, '{"error": "this demo copy is full: reload the page for a fresh one"}',
                                          "application/json")
            return await app(scope, receive, send)
        path = scope.get("path", "/")
        if path.startswith("/api/"):
            return await _send_simple(send, 401, '{"error": "this demo session has ended: reload the page for a new one"}',
                                      "application/json")
        if scope.get("method") not in ("GET", "HEAD") or path.startswith("/static/"):
            return await _send_simple(send, 404, "not found", "text/plain")
        ip = visitor_ip(scope)
        sid, app = await asyncio.to_thread(self.sessions.new, ip)
        if sid is None:
            return await _send_simple(send, 429, BUSY.replace("{home}", html.escape(self.home_url)))
        # the session's own app sets its cookie from ?t= and redirects to the page
        await send({"type": "http.response.start", "status": 303,
                    "headers": [(b"location", f"/?t={sid}".encode()), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b""})

    async def _sweep_forever(self):
        while True:
            await asyncio.sleep(60)
            await asyncio.to_thread(self.sessions.sweep)


def build(template, picture, root, site_dir=None, prepared=None, pictures=None, home_url="/",
          limit=60, idle=1800, per_ip=20, delay=WRITE_DELAY, prompts=None, budget=8_000_000):
    """The whole demo site as one ASGI app."""
    from .server.app import create_app
    pack = load_pack("starter")

    def make_app(workspace, sid):
        return create_app(workspace=workspace, pack=pack, token=sid, demo=True,
                          llm=DemoWriter(prepared, pictures, delay=delay, prompts=prompts),
                          model_label="demo writer (no AI)",
                          images=DemoImages(picture))

    return DemoSite(Sessions(make_app, root, template, limit=limit, idle=idle, per_ip=per_ip),
                    site_dir=site_dir, home_url=home_url, budget=budget)
