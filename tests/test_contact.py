# SPDX-License-Identifier: MIT
"""SPEC-SITE2 9: the contact form. No mail is ever sent: the sender is a stand-in."""
import asyncio

import httpx
import pytest

from slopmill import contact
from slopmill.contact import Contact
from test_demo import CF_EDGE, site  # noqa: F401  (the fixture)

FORM = {"name": "Ada", "email": "you@example.com", "message": "Hello there, this is a test message."}


class Outbox:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, key, payload):
        if self.fail:
            raise RuntimeError("down")
        self.sent.append((key, payload))


def app_with(site, **kw):
    build, _ = site
    app = build()
    box = kw.pop("box", Outbox())
    app.contact = Contact(to=kw.get("to", "you@example.com"), sender=kw.get("sender", "site <you@example.com>"),
                          key=kw.get("key", "k"), post=box, clock=kw.get("clock", __import__("time").time))
    return app, box


def post(app, data, origin="https://slopmill.org", ip="1.2.3.4", host="slopmill.org"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{host}",
                                     headers={"cf-connecting-ip": ip, "x-forwarded-for": CF_EDGE,
                                              **({"origin": origin} if origin else {})}) as c:
            return await c.post("/contact/", data=data)
    return asyncio.run(go())


def test_a_message_is_sent_with_the_visitor_as_reply_to(site):
    app, box = app_with(site)
    r = post(app, FORM)
    assert r.status_code == 303 and r.headers["location"] == "/contact/sent/"
    key, p = box.sent[0]
    assert key == "k" and p["to"] == ["you@example.com"] and p["reply_to"] == "you@example.com"
    assert "Hello there" in p["text"] and "Ada" in p["subject"]


def test_robots_that_fill_the_hidden_field_are_thanked_and_ignored(site):
    app, box = app_with(site)
    r = post(app, {**FORM, "website": "http://spam.example"})
    assert r.status_code == 303 and box.sent == []


@pytest.mark.parametrize("data,status,what", [
    ({**FORM, "email": "not an address"}, 422, "email address"),
    ({**FORM, "message": "hi"}, 422, "empty"),
    ({**FORM, "message": "x" * 5001}, 422, "too long"),
])
def test_bad_forms_get_a_readable_page(site, data, status, what):
    app, box = app_with(site)
    r = post(app, data)
    assert r.status_code == status and what in r.text and "<h1>" in r.text and box.sent == []


def test_someone_elses_page_may_not_post(site):
    app, box = app_with(site)
    assert post(app, FORM, origin="https://evil.example").status_code == 403
    assert box.sent == []
    assert post(app, FORM, origin=None).status_code == 303      # a browser that sends neither header


def test_the_limiter_forgets_old_addresses_and_stops_counting_at_the_daily_cap(site, monkeypatch):
    t = [1000.0]
    app, box = app_with(site, clock=lambda: t[0])
    for i in range(10):
        post(app, FORM, ip=f"9.9.9.{i}")
    assert len(app.contact.by_ip) == 10
    t[0] += 3601
    post(app, FORM, ip="8.8.8.8")
    assert list(app.contact.by_ip) == ["8.8.8.8"]
    monkeypatch.setattr(contact, "PER_DAY", len(app.contact.all))
    before = len(app.contact.by_ip)
    for i in range(20):
        assert post(app, FORM, ip=f"7.7.7.{i}").status_code == 429
    assert len(app.contact.by_ip) == before


def test_rate_limits_per_visitor_and_per_day(site, monkeypatch):
    app, box = app_with(site)
    codes = [post(app, FORM, ip="5.5.5.5").status_code for _ in range(contact.PER_HOUR + 1)]
    assert codes[-1] == 429 and codes[:-1] == [303] * contact.PER_HOUR
    monkeypatch.setattr(contact, "PER_DAY", len(box.sent) + 1)
    assert post(app, FORM, ip="6.6.6.6").status_code == 303
    assert post(app, FORM, ip="7.7.7.7").status_code == 429


def test_unconfigured_it_says_so_and_sends_nothing(site):
    app, box = app_with(site, key="")
    r = post(app, FORM)
    assert r.status_code == 503 and "not switched on" in r.text and box.sent == []


def test_a_mail_failure_is_a_page_not_a_crash(site):
    app, _ = app_with(site, box=Outbox(fail=True))
    r = post(app, FORM)
    assert r.status_code == 502 and "could not be sent" in r.text


def test_too_big_is_refused(site):
    app, box = app_with(site)
    r = post(app, {**FORM, "message": "x" * 30_000})
    assert r.status_code == 413 and box.sent == []


def test_the_demo_host_does_not_take_contact_posts(site):
    app, box = app_with(site)
    r = post(app, FORM, host="demo.slopmill.org", origin="https://demo.slopmill.org")
    assert r.status_code != 303 and box.sent == []


def test_nothing_about_where_it_goes_is_in_the_code():
    """The public export refuses any email address; contact.py must pass that scan."""
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(root, "tools"))
    try:
        import export_public
    except ImportError:
        pytest.skip("the exporter is not in this copy")
    data = open(os.path.join(root, "slopmill", "contact.py"), "rb").read()
    assert export_public.scan_bytes("slopmill/contact.py", data) == []


def test_outcome_pages_wear_the_sites_own_header(site, tmp_path):
    tpl = tmp_path / "result.html"
    tpl.write_text('<header class="site-head">slopmill</header><h1>{{TITLE}}</h1><p>{{TEXT}}</p>')
    app, box = app_with(site, key="")
    app.contact.template = str(tpl)
    r = post(app, FORM)
    assert r.status_code == 503 and 'class="site-head"' in r.text and "not switched on" in r.text


def test_the_host_header_is_not_a_licence_to_post(site):
    app, box = app_with(site)
    r = post(app, FORM, host="evil.example", origin="https://evil.example")
    assert r.status_code == 403 and box.sent == []


def test_the_built_site_hides_donations_until_the_page_exists():
    import importlib
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(root, "site"))
    pages = importlib.import_module("pages")
    pages.SPONSOR = None
    assert "coffee" not in pages.support("a<!--support--> coffee<!--/support-->b").lower()
    pages.SPONSOR = "https://example.org/give"
    assert "coffee" in pages.support("a<!--support--> coffee<!--/support-->b")
