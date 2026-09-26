# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hosted trial: `slopmill trial --config trial.toml` (SPEC-TRIAL.md).

The public demo (demo.py) with a real writer, for a newsletter's readers, for a week or a
budget, whichever runs out first:

    visitor ──> TrialSite ── no invite?  a page saying who it is for
                    │    ── ended?       a page saying thank you
                    └──> DemoSite: their own private workspace, create_app(demo=True)
                             writer = Budgeted(OpenAI's API): every call reserves its worst
                             case against the budget first, then settles what it cost

The spend is kept in a ledger file that survives a restart; the key comes only from the
environment; AI pictures are off; research is the free search, never the writer's own.
"""
import datetime
import hmac
import html
import json
import math
import os
import re
import threading
import time
import tomllib
import uuid

from .demo import (DemoSite, Sessions, _cookie, _header, _query, _send_simple, visitor_ip)
from .ids import atomic_write
from .providers import Cancelled, LLMError, OpenAIText

INVITE_COOKIE = "slopmill_trial"
BYTES_PER_TOKEN = 1        # the true worst case: a byte-level tokenizer makes at most one token a byte
FRAMING_TOKENS = 200       # the chat format's own tokens around the messages, allowed for generously


class Exhausted(LLMError):
    pass


class Ledger:
    """What the trial has spent, in dollars. reserve() before a call, settle() after."""

    def __init__(self, path, budget):
        self.path, self.budget = path, float(budget)
        self.lock = threading.Lock()
        self.reserved = {}
        self.spent, self.calls = 0.0, 0
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.spent, self.calls = float(data.get("spent_usd", 0)), int(data.get("calls", 0))
            # A call that was running when the process stopped may have been billed: what it
            # held counts as spent (the ledger never forgets money it cannot account for).
            self.spent += sum(float(v) for v in (data.get("open") or {}).values())
        except FileNotFoundError:
            pass
        except (ValueError, TypeError) as e:
            # A damaged ledger must not read as "nothing spent".
            raise RuntimeError(f"the trial ledger {path} cannot be read ({e}); fix or remove it")

    def left(self):
        """What is left for new calls: the budget less what was spent and what running calls hold."""
        with self.lock:
            return self.budget - self.spent - sum(self.reserved.values())

    def unspent(self):
        """What has not been spent (calls in flight do not count: they may cost less)."""
        with self.lock:
            return self.budget - self.spent

    def reserve(self, amount):
        with self.lock:
            if self.spent + sum(self.reserved.values()) + amount > self.budget:
                raise Exhausted("the trial's budget is used up, or this request is too big for "
                                "what is left: thank you for trying slopmill")
            token = uuid.uuid4().hex
            self.reserved[token] = amount
            self._write()
            return token

    def release(self, token):
        """A reservation given back unused: the call was never sent."""
        with self.lock:
            self.reserved.pop(token, None)
            self._write()

    def settle(self, token, actual):
        with self.lock:
            self.reserved.pop(token, None)
            self.spent += max(0.0, float(actual))
            self.calls += 1
            self._write()

    def _write(self):
        """Called with the lock held: the spend and every open reservation, atomically."""
        atomic_write(self.path, json.dumps({"spent_usd": round(self.spent, 6), "calls": self.calls,
                                            "open": self.reserved, "budget_usd": self.budget,
                                            "updated": time.time()}) + "\n")


class Limiter:
    """At most `per_hour` model calls an hour from one address."""

    def __init__(self, per_hour):
        self.per_hour = per_hour
        self.seen = {}
        self.lock = threading.Lock()

    def check(self, ip):
        now = time.time()
        with self.lock:
            recent = [t for t in self.seen.get(ip, []) if now - t < 3600]
            if len(recent) >= self.per_hour:
                self.seen[ip] = recent
                raise LLMError(f"that is {self.per_hour} requests from your address this hour: "
                               f"please wait a little before the next one")
            self.seen[ip] = recent + [now]


class Budgeted:
    """A writer whose every call is paid for out of the ledger first."""
    web_search = False            # research is the free search, never the writer's own paid one

    def __init__(self, inner, ledger, price_in, price_out, max_out, limiter=None, ip=""):
        self.inner, self.ledger = inner, ledger
        self.price_in, self.price_out, self.max_out = float(price_in), float(price_out), int(max_out)
        self.limiter, self.ip = limiter, ip
        self.label = getattr(inner, "label", "model")
        self.max_request_bytes = getattr(inner, "max_request_bytes", 115_000)

    def cost(self, tokens_in, tokens_out):
        return tokens_in / 1e6 * self.price_in + tokens_out / 1e6 * self.price_out

    def worst_case(self, system, prompt, files):
        size = len(system.encode()) + len(prompt.encode()) + sum(os.path.getsize(p) for p in files)
        return self.cost(math.ceil(size / BYTES_PER_TOKEN) + FRAMING_TOKENS, self.max_out)

    def describe(self):
        return getattr(self.inner, "describe", lambda: {})()

    def __call__(self, system, prompt, files, cancel=None, timeout=None):
        size = len(system.encode()) + len(prompt.encode()) + sum(os.path.getsize(p) for p in files)
        if size > self.max_request_bytes:
            # Refused before anything is sent: nothing is reserved or spent.
            raise LLMError(f"the request is {size // 1024}KB and this model takes at most "
                           f"{self.max_request_bytes // 1024}KB: shorten the issue")
        for p in files:                      # a file that cannot be read fails here, unpaid
            try:
                with open(p, "rb") as f:
                    f.read(1)
            except OSError as e:
                raise LLMError(f"an attached file could not be read: {e}")
        worst = self.worst_case(system, prompt, files)
        token = self.ledger.reserve(worst)           # refused for the budget: no hourly slot used
        if self.limiter is not None:
            try:
                self.limiter.check(self.ip)
            except LLMError:
                self.ledger.release(token)
                raise
        try:
            kw = {"timeout": timeout} if timeout else {}
            reply = self.inner(system, prompt, files, cancel=cancel, **kw)
        except Cancelled:
            self.ledger.settle(token, worst)      # it was sent: it may have been billed
            raise
        except LLMError as e:
            if e.status is not None:              # the provider answered with an error: not billed
                self.ledger.release(token)
            else:                                 # lost on the way back: it may have been billed
                self.ledger.settle(token, worst)
            raise
        except Exception:
            self.ledger.settle(token, worst)
            raise
        u = getattr(reply, "usage", None) or {}
        actual = self.cost(u["in"], u["out"]) if u.get("exact") else worst
        self.ledger.settle(token, actual)
        return reply


# ── the site ────────────────────────────────────────────────────────────────────

PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>slopmill trial</title>
<style>body{{font:17px/1.6 Georgia,serif;max-width:34em;margin:12vh auto;padding:0 20px}}h1{{font-size:26px}}</style>
<h1>{title}</h1><p>{body}</p>
<p><a href="{home}">{home_label}</a> · install it on your own computer, with your own key.</p>
"""


def page(title, body, home):
    """A plain page; the words about who the trial is for and where to get slopmill come from
    trial.toml, so the engine names no publication."""
    label = re.sub(r"^https?://", "", home).rstrip("/") or home
    return PAGE.format(title=html.escape(title), body=html.escape(body), home=html.escape(home, quote=True),
                       home_label=html.escape(label))


class TrialSite:
    """The invite gate and the end, in front of the demo's per-visitor workspaces."""

    def __init__(self, inner, invite, ends_at, ledger, min_call=0.0, audience="its readers", home="/"):
        if not invite or len(invite) < 16:
            raise ValueError("[trial] invite must be a long random token (16+ characters)")
        self.inner, self.invite, self.ends_at, self.ledger = inner, invite, ends_at, ledger
        self.min_call = min_call      # dollars: the output cap at the output price
        self.home = home
        self.not_invited = page(f"This trial is for {audience}",
                                "It opens from the invitation link. If you were sent one, use that link.", home)

    def ended(self):
        if time.time() >= self.ends_at:
            return "The trial week is over."
        # Over when not even the smallest call could fit (its output cap alone), counting only
        # what was spent: a call still running may cost less than it holds.
        if self.ledger.unspent() < self.min_call:
            return "The trial's budget has been used."
        return None

    def _invited(self, scope):
        got = _cookie(scope, INVITE_COOKIE) or ""
        return bool(got) and hmac.compare_digest(got.encode(), self.invite.encode())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        path = scope.get("path", "/")
        why = self.ended()
        if why:
            if path.startswith("/api/"):
                return await _send_simple(send, 410, json.dumps({"error": f"the trial has ended: {why}"}),
                                          "application/json")
            return await _send_simple(send, 410, page("The trial has ended: thank you", why, self.home))
        k = _query(scope, "k") or ""
        if k and hmac.compare_digest(k.encode(), self.invite.encode()):
            # The link from the newsletter: remember it, and drop it from the address bar.
            age = max(60, int(self.ends_at - time.time()))
            cookie = (f"{INVITE_COOKIE}={self.invite}; Max-Age={age}; Path=/; HttpOnly; Secure; "
                      f"SameSite=Lax")
            await send({"type": "http.response.start", "status": 303,
                        "headers": [(b"location", b"/"), (b"set-cookie", cookie.encode()),
                                    (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b""})
            return
        if not self._invited(scope):
            if path.startswith("/api/"):
                return await _send_simple(send, 403, '{"error": "this trial opens from the newsletter\'s link"}',
                                          "application/json")
            return await _send_simple(send, 403, self.not_invited)
        return await self.inner(scope, receive, send)


def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    t, w = cfg.get("trial") or {}, cfg.get("writer") or {}
    for need in ("invite", "ends_at", "budget_usd", "ledger"):
        if need not in t:
            raise ValueError(f"[trial] needs {need}")
    for need in ("model", "price_in_per_mtok", "price_out_per_mtok"):
        if need not in w:
            raise ValueError(f"[writer] needs {need}")
    if int(w.get("max_output_tokens", 16000)) <= 0:
        raise ValueError("[writer] max_output_tokens must be above zero: the budget relies on it")
    if float(w["price_in_per_mtok"]) <= 0 or float(w["price_out_per_mtok"]) <= 0:
        raise ValueError("[writer] prices must be the model's real prices, above zero: the budget depends on them")
    ends = t["ends_at"]
    ends_at = (ends if isinstance(ends, datetime.datetime) else
               datetime.datetime.fromisoformat(str(ends).replace("Z", "+00:00")))
    if ends_at.tzinfo is None:
        raise ValueError("[trial] ends_at must say its time zone (end it with Z for UTC)")
    return cfg, ends_at.timestamp()


def build(config_path, root, template=None, *, writer=None, searcher=None, limit=100, idle=7200, per_ip=20):
    """The whole trial site as one ASGI app. writer/searcher are for tests."""
    from .pack import load_pack
    from .research import DuckDuckGo
    from .server.app import create_app

    cfg, ends_at = load_config(config_path)
    t, w = cfg["trial"], cfg["writer"]
    ledger = Ledger(t["ledger"], t["budget_usd"])
    limiter = Limiter(int(t.get("calls_per_hour_per_address", 60)))
    key_env = w.get("api_key_env", "OPENAI_API_KEY")
    if writer is None and not os.environ.get(key_env):
        # Without a key every call would fail after its worst case was counted.
        raise ValueError(f"the trial needs the writer's key in ${key_env}; it is not set")
    inner_writer = writer or OpenAIText(w["model"], key_env=key_env,
                                        label=w.get("label", w["model"]),
                                        reasoning_effort=w.get("reasoning_effort"),
                                        max_output_tokens=int(w.get("max_output_tokens", 16000)))
    search = searcher if searcher is not None else DuckDuckGo(jina=True)
    pack = load_pack("starter")
    ends_label = datetime.datetime.fromtimestamp(ends_at, datetime.timezone.utc).strftime("%B %-d")

    def info():
        return {"ends_at": ends_at, "ends": ends_label, "budget_usd": ledger.budget,
                "left_usd": round(max(0.0, ledger.left()), 2)}

    def make_app(workspace, sid, ip=""):
        llm = Budgeted(inner_writer, ledger, w["price_in_per_mtok"], w["price_out_per_mtok"],
                       int(w.get("max_output_tokens", 16000)), limiter, ip)
        return create_app(workspace=workspace, pack=pack, token=sid, demo=True, llm=llm,
                          model_label=w.get("label", w["model"]), images=None, research=search,
                          trial=info)

    if template is None:                   # the spec's command: --config alone
        template = make_template(os.path.join(os.path.dirname(os.path.abspath(root)),
                                              os.path.basename(root) + "-template"))
    sessions = Sessions(make_app, root, template, limit=limit, idle=idle, per_ip=per_ip, with_ip=True)
    min_call = int(w.get("max_output_tokens", 16000)) / 1e6 * float(w["price_out_per_mtok"])
    home = t.get("home_url", "/")
    return TrialSite(DemoSite(sessions, site_dir=None, home_url=home), t["invite"], ends_at, ledger,
                     min_call=min_call, audience=t.get("audience", "its readers"), home=home), ledger


def make_template(dest):
    """A visitor's starting workspace: what `slopmill setup` makes, without a key."""
    from .pack import load_pack
    from .setup import add_practice_issue, personalise_voice
    from .voices import VoiceLibrary
    os.makedirs(dest, exist_ok=True)
    starter = load_pack("starter")
    personalise_voice(dest, starter, "Your Newsletter", "You")
    example = load_pack("storybook")
    VoiceLibrary(dest).seed_from_design(example)
    add_practice_issue(dest, example)
    return dest
