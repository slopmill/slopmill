# SPDX-License-Identifier: AGPL-3.0-or-later
"""The contact form on the intro site: POST /contact, sent on as an email.

A plain HTML form (the site runs no script), so it answers with a page: a redirect to
/contact/sent/ when the message went, or a short page saying what was wrong.

Nothing about where it goes is in the code or the page. The server is told at start:
    SLOPMILL_CONTACT_TO     the address messages go to
    SLOPMILL_CONTACT_FROM   the sender the mail service will accept, e.g. "slopmill.org <x@y>"
    RESEND_API_KEY          the key for Resend (resend.com), which sends it
Without all three the form answers "not switched on yet" and sends nothing.

Guards: a hidden field only robots fill in, no foreign Origin or Referer, 20KB at most, 5
messages an hour from one address and 40 a day in all.
"""
import collections
import html
import os
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

SEND_URL = "https://api.resend.com/emails"
MAX_BODY = 20_000
PER_HOUR = 5              # one address can be a whole office
PER_DAY = 40
EMAIL_RE = re.compile(r"^[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")


class Contact:
    def __init__(self, to=None, sender=None, key=None, hosts=("slopmill.org", "www.slopmill.org"),
                 post=None, clock=time.time, template=None):
        self.to = to if to is not None else os.environ.get("SLOPMILL_CONTACT_TO", "")
        self.sender = sender if sender is not None else os.environ.get("SLOPMILL_CONTACT_FROM", "")
        self.key = key if key is not None else os.environ.get("RESEND_API_KEY", "")
        self.hosts = {h.lower() for h in hosts}
        self.post = post or _resend_post
        self.clock = clock
        self.template = template          # the site's own page, with {{TITLE}} and {{TEXT}}
        self.lock = threading.Lock()
        self.by_ip = collections.defaultdict(list)
        self.all = []

    @property
    def ready(self):
        return bool(self.to and self.sender and self.key)

    def same_site(self, origin, referer, host=""):
        """Not posted from someone else's page. A foreign Origin or Referer is refused; a
        browser that sends neither (some privacy tools strip both) is let through, and the
        hidden field and the limits still apply. A request's Host header counts for nothing."""
        for value in (origin, referer):
            if value and value != "null":
                return (urlsplit(value).hostname or "").lower() in self.hosts
        return True

    def allow(self, ip):
        now = self.clock()
        with self.lock:
            self.all = [t for t in self.all if now - t < 86400]
            if len(self.all) >= PER_DAY:
                return False                  # before any per-address entry is made
            for k in [k for k, v in self.by_ip.items() if not v or now - v[-1] >= 3600]:
                del self.by_ip[k]             # an hour old: forgotten, so memory stays bounded
            mine = [t for t in self.by_ip.get(ip, []) if now - t < 3600]
            if len(mine) >= PER_HOUR:
                self.by_ip[ip] = mine
                return False
            self.by_ip[ip] = mine + [now]
            self.all.append(now)
            return True

    def handle(self, body, ip, origin="", referer="", host=""):
        """(status, page html or None, redirect or None) for one POST."""
        if not self.same_site(origin, referer, host):
            return 403, self.page("That did not come from the contact page.",
                             "Open slopmill.org/contact and send it from there."), None
        if len(body) > MAX_BODY:
            return 413, self.page("That message is too long.", "Keep it under about 5,000 characters."), None
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}
        if form.get("website", "").strip():        # the field people never see
            return 303, None, "/contact/sent/"
        name = " ".join(form.get("name", "").split())[:100]
        email = form.get("email", "").strip()[:200]
        message = form.get("message", "").replace("\r\n", "\n").strip()
        if not EMAIL_RE.match(email):
            return 422, self.page("That email address does not look right.",
                             "We need it to write back. Go back and check it."), None
        if len(message) < 10:
            return 422, self.page("The message is empty.", "Go back and say a little more."), None
        if len(message) > 5000:
            return 422, self.page("That message is too long.", "Keep it under 5,000 characters."), None
        if not self.ready:
            return 503, self.page("The contact form is not switched on yet.",
                             "Sorry about that. Open an issue on GitHub instead, and we will see it."), None
        if not self.allow(ip):
            return 429, self.page("That is a lot of messages.", "Please try again in an hour."), None
        payload = {"from": self.sender, "to": [self.to], "reply_to": email,
                   "subject": f"slopmill.org: a message from {name or email}"[:150],
                   "text": f"From: {name or '(no name given)'} <{email}>\n\n{message}\n\n"
                           f"(Sent from the contact form on slopmill.org. Reply to write back.)"}
        try:
            self.post(self.key, payload)
        except Exception:
            return 502, self.page("The message could not be sent just now.",
                             "Nothing went wrong on your side. Please try again later."), None
        return 303, None, "/contact/sent/"


    def page(self, title, text):
        if self.template:
            try:
                with open(self.template, encoding="utf-8") as f:
                    t = f.read()
                return t.replace("{{TITLE}}", html.escape(title)).replace("{{TEXT}}", html.escape(text))
            except OSError:
                pass
        return page(title, text)


def _resend_post(key, payload):
    import httpx
    r = httpx.post(SEND_URL, json=payload, timeout=15,
                   headers={"Authorization": f"Bearer {key}", "User-Agent": "slopmill-contact/1"})
    if r.status_code >= 300:
        raise RuntimeError(f"the mail service answered {r.status_code}")


def page(title, text):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · slopmill</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="/style.css"></head>
<body><main class="narrow"><section class="hero"><h1>{html.escape(title)}</h1>
<p class="lede">{html.escape(text)}</p><p><a class="btn ghost" href="/contact/">Back to the form</a>
<a class="btn ghost" href="/">Home</a></p></section></main></body></html>"""
