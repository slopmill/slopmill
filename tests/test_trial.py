# SPDX-License-Identifier: MIT
"""SPEC-TRIAL: the hosted trial. No real model, search engine or web page is reached."""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from slopmill import providers, trial
from slopmill.providers import LLMError, Reply
from test_chat_pictures import FakeSearch

INVITE = "k" * 32


def config(tmp_path, ends_in=3600, budget=10.0, price_in=1.0, price_out=8.0, max_out=1000, per_hour=60):
    ends = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ends_in))
    p = tmp_path / "trial.toml"
    p.write_text(f"""
[trial]
invite = "{INVITE}"
ends_at = "{ends}"
budget_usd = {budget}
ledger = "{tmp_path / 'ledger.json'}"
calls_per_hour_per_address = {per_hour}
audience = "the newsletter's readers"
home_url = "https://slopmill.org"

[writer]
model = "gpt-6-sol"
price_in_per_mtok = {price_in}
price_out_per_mtok = {price_out}
max_output_tokens = {max_out}
""")
    return str(p)


class Writer:
    """A stand-in writer that reports what it cost, like the API does."""
    label, max_request_bytes = "fake", 115_000

    def __init__(self, tokens_in=1000, tokens_out=500, exact=True, fail=False, status=None):
        self.calls, self.tin, self.tout, self.exact, self.fail = 0, tokens_in, tokens_out, exact, fail
        self.status = status

    def __call__(self, system, prompt, files, cancel=None, timeout=None):
        self.calls += 1
        if self.fail:
            raise LLMError("down", status=self.status)
        return Reply("=== NOTE ===\nok\n=== END ===",
                     {"in": self.tin, "out": self.tout, "exact": self.exact} if self.exact else None)


@pytest.fixture
def site(tmp_path):
    tpl = tmp_path / "tpl"
    trial.make_template(str(tpl))

    def go(writer=None, **kw):
        app, ledger = trial.build(config(tmp_path, **kw), str(tmp_path / "root"), str(tpl),
                                  writer=writer or Writer(), searcher=FakeSearch([]))
        return TestClient(app, base_url="https://try.slopmill.org"), ledger
    return go


# ── 1. the invitation ────────────────────────────────────────────────────────────

def test_1_no_invite_no_workspace(site):
    c, _ = site()
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 403 and "the newsletter&#x27;s readers" in r.text
    assert c.get("/?k=wrong", follow_redirects=False).status_code == 403
    assert c.get("/api/state").status_code == 403


def test_1_the_link_opens_a_private_workspace(site):
    c, _ = site()
    r = c.get(f"/?k={INVITE}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" in cookie
    r = c.get("/", follow_redirects=False)                    # a workspace is made
    assert r.status_code == 303 and r.headers["location"].startswith("/?t=")
    r = c.get(r.headers["location"], follow_redirects=True)
    assert r.status_code == 200
    st = c.get("/api/state").json()
    assert st["trial"]["left_usd"] == 10.0 and st["pictures"] is False and st["demo"] is True
    assert st["facts"] is True                  # unlike the plain demo: a real writer and research
    assert st["issues"] and st["issues"][0]["slug"] == "001-welcome"


# ── 2. the end ───────────────────────────────────────────────────────────────────

def test_2_after_the_week_it_says_thank_you(site):
    c, _ = site(ends_in=-5)
    r = c.get(f"/?k={INVITE}", follow_redirects=False)
    assert r.status_code == 410 and "The trial week is over." in r.text
    assert c.get("/api/state").status_code == 410


def test_2_when_the_budget_is_spent_it_says_thank_you(site, tmp_path):
    (tmp_path / "ledger.json").write_text(json.dumps({"spent_usd": 9.999, "calls": 3}))
    c, _ = site()
    assert c.get(f"/?k={INVITE}", follow_redirects=False).status_code == 410


# ── 3. the budget ────────────────────────────────────────────────────────────────

def test_3_a_call_is_reserved_then_settled_at_its_real_cost(tmp_path):
    ledger = trial.Ledger(str(tmp_path / "l.json"), 10)
    w = trial.Budgeted(Writer(tokens_in=1_000_000, tokens_out=100_000), ledger, 1.0, 8.0, 1000)
    f = tmp_path / "doc.md"
    f.write_text("x" * 3000)
    w("s", "p", [str(f)])
    assert ledger.spent == pytest.approx(1.0 + 0.8)                 # the provider's counts at the prices
    assert json.loads((tmp_path / "l.json").read_text())["spent_usd"] == pytest.approx(1.8)
    assert trial.Ledger(str(tmp_path / "l.json"), 10).spent == pytest.approx(1.8)   # survives a restart


def test_3_the_worst_case_must_fit(tmp_path):
    ledger = trial.Ledger(str(tmp_path / "l.json"), 0.01)
    w = trial.Budgeted(Writer(), ledger, 1.0, 8.0, 16000)          # 16k tokens out = $0.128 worst case
    with pytest.raises(trial.Exhausted, match="budget is used up"):
        w("s", "p", [])
    assert w.inner.calls == 0


def test_3_no_counts_or_a_failure_costs_the_worst_case(tmp_path):
    ledger = trial.Ledger(str(tmp_path / "l.json"), 10)
    w = trial.Budgeted(Writer(exact=False), ledger, 1.0, 8.0, 1000)
    w("s" * 300, "p", [])
    assert ledger.spent == pytest.approx(w.cost(301 // trial.BYTES_PER_TOKEN + trial.FRAMING_TOKENS, 1000))
    w2 = trial.Budgeted(Writer(fail=True), ledger, 1.0, 8.0, 1000)
    before = ledger.spent
    with pytest.raises(LLMError):
        w2("s", "p", [])
    assert ledger.spent == pytest.approx(before + w2.cost(2 // trial.BYTES_PER_TOKEN + trial.FRAMING_TOKENS, 1000))


def test_3_concurrent_calls_cannot_overshoot(tmp_path):
    ledger = trial.Ledger(str(tmp_path / "l.json"), 1.0)
    gate = threading.Event()

    class Slow(Writer):
        def __call__(self, *a, **k):
            gate.wait(2)
            return super().__call__(*a, **k)
    w = trial.Budgeted(Slow(tokens_in=10, tokens_out=10), ledger, 1.0, 8.0, 100_000)   # worst case $0.80
    results = []

    def one():
        try:
            w("s", "p", [])
            results.append("ok")
        except trial.Exhausted:
            results.append("refused")
    ts = [threading.Thread(target=one) for _ in range(3)]
    for t in ts:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in ts:
        t.join()
    assert sorted(results) == ["ok", "refused", "refused"]            # only one worst case fits $1


def test_3_a_damaged_ledger_is_not_zero(tmp_path):
    (tmp_path / "l.json").write_text("{not json")
    with pytest.raises(RuntimeError, match="cannot be read"):
        trial.Ledger(str(tmp_path / "l.json"), 10)


# ── 4, 6. the writer and the limits ──────────────────────────────────────────────

def test_3_a_call_the_provider_refused_costs_nothing(tmp_path, monkeypatch):
    """A wrong key, a rate limit, an outage: the API answered with an error and billed nothing,
    so the trial must not either, or a bad key would end it after a few dozen clicks."""
    import httpx
    ledger = trial.Ledger(str(tmp_path / "l.json"), 10)
    for code in (400, 401, 429, 500, 503):
        monkeypatch.setenv("TRIAL_TEST_KEY", "sk-test")
        inner = providers.OpenAIText("gpt-6-sol", key_env="TRIAL_TEST_KEY", max_output_tokens=1000,
                                     transport=httpx.MockTransport(lambda r, c=code: httpx.Response(c, json={})))
        w = trial.Budgeted(inner, ledger, 1.0, 8.0, 1000)
        with pytest.raises(LLMError, match=f"HTTP {code}"):
            w("s", "p", [])
    assert ledger.spent == 0 and ledger.unspent() == pytest.approx(10)
    w = trial.Budgeted(Writer(fail=True), ledger, 1.0, 8.0, 1000)     # no status: lost in transit
    with pytest.raises(LLMError):
        w("s", "p", [])
    assert ledger.spent > 0


def test_4_the_output_cap_is_sent(monkeypatch):
    import httpx
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
    monkeypatch.setenv("TRIAL_TEST_KEY", "sk-test")
    w = providers.OpenAIText("gpt-6-sol", key_env="TRIAL_TEST_KEY", max_output_tokens=1234,
                             transport=httpx.MockTransport(handler))
    reply = w("s", "p", [])
    assert sent["max_completion_tokens"] == 1234 and sent["model"] == "gpt-6-sol"
    assert reply.usage == {"in": 7, "out": 3, "exact": True}       # what the budget settles on


def test_4_the_writer_never_searches_itself():
    assert trial.Budgeted.web_search is False


def test_6_calls_per_hour_per_address(tmp_path):
    lim = trial.Limiter(2)
    lim.check("1.2.3.4")
    lim.check("1.2.3.4")
    with pytest.raises(LLMError, match="2 requests from your address this hour"):
        lim.check("1.2.3.4")
    lim.check("5.6.7.8")                                              # another address is fine


def test_config_refuses_zero_prices(tmp_path):
    with pytest.raises(ValueError, match="real prices"):
        trial.load_config(config(tmp_path, price_in=0))


def test_the_invite_must_be_long():
    with pytest.raises(ValueError, match="long random token"):
        trial.TrialSite(None, "short", time.time() + 60, None)



# ── review round 1 fixes ─────────────────────────────────────────────────────────

def test_r1_config_alone_starts(tmp_path):
    app, ledger = trial.build(config(tmp_path), str(tmp_path / "root"), writer=Writer(), searcher=FakeSearch([]))
    assert (tmp_path / "root-template" / "issues" / "001-welcome" / "issue.md").exists()


def test_r1_a_request_refused_before_sending_costs_nothing(tmp_path):
    ledger = trial.Ledger(str(tmp_path / "l.json"), 10)
    w = trial.Budgeted(Writer(), ledger, 1.0, 8.0, 1000)
    w.max_request_bytes = 100
    with pytest.raises(LLMError, match="takes at most"):
        w("s" * 200, "p", [])
    assert ledger.spent == 0 and ledger.calls == 0 and w.inner.calls == 0


def test_r1_it_ends_only_when_no_call_could_fit(site, tmp_path):
    # output cap 1000 tokens at $8/M = $0.008: with $0.02 unspent the trial is still on
    (tmp_path / "ledger.json").write_text(json.dumps({"spent_usd": 9.98, "calls": 3}))
    c, _ = site()
    assert c.get(f"/?k={INVITE}", follow_redirects=False).status_code == 303
    (tmp_path / "ledger.json").write_text(json.dumps({"spent_usd": 9.995, "calls": 4}))
    c, _ = site()
    assert c.get(f"/?k={INVITE}", follow_redirects=False).status_code == 410


def test_r1_a_running_call_does_not_end_the_trial(tmp_path):
    ledger = trial.Ledger(str(tmp_path / "l.json"), 1.0)
    site_ = trial.TrialSite(None, INVITE, time.time() + 60, ledger, min_call=0.1)
    ledger.reserve(0.95)                      # a call in flight holds most of it
    assert site_.ended() is None              # unspent is still $1


def test_r1_the_reservation_is_a_true_worst_case():
    assert trial.BYTES_PER_TOKEN == 1



# ── review round 2 fixes ─────────────────────────────────────────────────────────

def test_r2_a_call_running_at_a_crash_counts_as_spent(tmp_path):
    ledger = trial.Ledger(str(tmp_path / "l.json"), 10)
    ledger.reserve(0.5)                           # sent… and the process dies before settle()
    again = trial.Ledger(str(tmp_path / "l.json"), 10)
    assert again.spent == pytest.approx(0.5) and again.left() == pytest.approx(9.5)


def test_r2_an_unreadable_file_costs_nothing(tmp_path):
    ledger = trial.Ledger(str(tmp_path / "l.json"), 10)
    w = trial.Budgeted(Writer(), ledger, 1.0, 8.0, 1000)
    d = tmp_path / "a-folder"
    d.mkdir()
    with pytest.raises(LLMError):
        w("s", "p", [str(d)])
    assert ledger.spent == 0 and ledger.calls == 0


def test_r2_the_output_cap_must_be_positive(tmp_path):
    p = config(tmp_path, max_out=0)
    with pytest.raises(ValueError, match="max_output_tokens must be above zero"):
        trial.load_config(p)



# ── review round 3 fixes ─────────────────────────────────────────────────────────

def test_r3_no_key_no_start(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="needs the writer's key"):
        trial.build(config(tmp_path), str(tmp_path / "root"), searcher=FakeSearch([]))


def test_r3_a_budget_refusal_uses_no_hourly_slot(tmp_path):
    ledger = trial.Ledger(str(tmp_path / "l.json"), 0.001)
    lim = trial.Limiter(1)
    w = trial.Budgeted(Writer(), ledger, 1.0, 8.0, 16000, lim, "1.2.3.4")
    for _ in range(3):
        with pytest.raises(trial.Exhausted):
            w("s", "p", [])
    lim.check("1.2.3.4")                      # its one slot is still there


def test_r3_a_limiter_refusal_gives_the_reservation_back(tmp_path):
    ledger = trial.Ledger(str(tmp_path / "l.json"), 10)
    lim = trial.Limiter(0)
    w = trial.Budgeted(Writer(), ledger, 1.0, 8.0, 1000, lim, "1.2.3.4")
    with pytest.raises(LLMError, match="this hour"):
        w("s", "p", [])
    assert ledger.left() == pytest.approx(10) and ledger.spent == 0
