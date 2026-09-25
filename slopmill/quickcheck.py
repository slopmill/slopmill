# SPDX-License-Identifier: AGPL-3.0-or-later
"""The free quick check: spelling and grammar from LanguageTool, no AI model and no tokens.

It asks LanguageTool (the free public service at languagetool.org, or your own server) about
the issue's visible words, turns each suggestion into the same FIND/REPLACE fix the AI check
makes, and hands those to proof.apply_fixes, so the highlights, click-to-switch, Done and
Undo all work the same way.

Only mistakes are asked about (spelling, grammar, doubled words, capitals), never style. A word the author uses in their own voice pack (a name, a coinage, a brand) is never
"corrected", and neither is anything in the workspace's dictionary.txt.
"""
import os
import re

from . import proof

DEFAULT_URL = "https://api.languagetool.org/v2"
# Everything but style: a voice is allowed to be informal, repetitive or unusual on purpose.
# Capitals are left alone too: Small Important Things and playful CaSiNg are a voice's to
# choose (the Milne example does it on every page).
NOT_MISTAKES = ("STYLE,TYPOGRAPHY,REDUNDANCY,COLLOQUIALISMS,PUNCTUATION,PLAIN_ENGLISH,WIKIPEDIA,"
                "REPETITIONS_STYLE,GENDER_NEUTRALITY,CASING")
CHUNK = 18_000                 # bytes (UTF-8): the free service takes up to 20KB a request
ALWAYS_KNOWN = {"slopmill"}
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")


class CheckError(Exception):
    pass


class LanguageTool:
    kind = "languagetool"

    def __init__(self, url=DEFAULT_URL, language="en-US", transport=None, timeout=30):
        self.url = url.rstrip("/")
        self.language = language
        self.transport = transport          # tests hand in an httpx.MockTransport
        self.timeout = timeout

    def describe(self):
        return {"provider": "languagetool", "url": self.url, "language": self.language,
                "public": self.url == DEFAULT_URL}

    def check(self, text):
        import httpx
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as c:
                r = c.post(f"{self.url}/check", data={
                    "text": text, "language": self.language, "disabledCategories": NOT_MISTAKES,
                    "level": "default"})
        except httpx.HTTPError as e:
            raise CheckError(f"could not reach LanguageTool at {self.url}: {e}")
        if r.status_code == 429:
            raise CheckError("LanguageTool's free service is busy (too many checks this minute); "
                             "try again in a minute")
        if r.status_code != 200:
            raise CheckError(f"LanguageTool answered HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json().get("matches") or []
        except ValueError:
            raise CheckError("LanguageTool's answer was not JSON")


def known_words(voice=None, workspace=None):
    """Words the author uses on purpose: everything in the voice pack's files that are on,
    plus the workspace's dictionary.txt (one word per line)."""
    words = set(ALWAYS_KNOWN)
    paths = []
    if voice is not None:
        paths += [voice.path(e["path"]) for e in voice.files() if e["on"] and not e["missing"]]
    if workspace:
        paths.append(os.path.join(workspace, "dictionary.txt"))
    for p in paths:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                words.update(w.lower() for w in WORD_RE.findall(f.read()))
        except OSError:
            pass
    return words


def _widen(text, start, end):
    """The smallest stretch around [start, end) that occurs once in the block: the flagged
    word first, then a word more on each side, up to three times."""
    s, e = start, end
    for _ in range(4):
        if text.count(text[s:e]) == 1:
            return s, e
        left = text.rfind(" ", 0, max(0, s - 1))
        right = text.find(" ", e + 1)
        s = 0 if left < 0 else left + 1
        e = len(text) if right < 0 else right
    return (s, e) if text.count(text[s:e]) == 1 else (None, None)


def _bytes(text):
    return len(text.encode("utf-8"))


def _pieces(visible, limit):
    """[(start, piece)]: a block's visible text in pieces of at most `limit` UTF-8 bytes (a
    single paragraph can outgrow one request), cut after a sentence, else at a space, and
    only inside a word when a piece has no space at all."""
    out, start = [], 0
    while _bytes(visible[start:]) > limit:
        end = start + limit                     # characters; shrink until the bytes fit
        while _bytes(visible[start:end]) > limit:
            end = start + (end - start) * 9 // 10
        cut = max(visible.rfind(". ", start, end), visible.rfind("\n", start, end))
        if cut <= start:
            cut = visible.rfind(" ", start, end)
        cut = cut + 1 if cut > start else end
        out.append((start, visible[start:cut]))
        start = cut
    out.append((start, visible[start:]))
    return out


def _utf16_to_index(text):
    """LanguageTool counts positions in UTF-16 units, as Java does: an emoji is two. Returns
    a list mapping each UTF-16 position to the Python index it falls on."""
    table = []
    for i, ch in enumerate(text):
        table.append(i)
        if ord(ch) > 0xFFFF:
            table.append(i)
    table.append(len(text))
    return table


def fixes(blocks, lt, known=frozenset(), log=lambda level, text: None):
    """[{block, find, replace, why}] for proof.apply_fixes, from LanguageTool's matches.
    If the service stops answering partway (it is free and rate-limited), what was already
    checked is kept and the rest is reported as not checked."""
    items = []
    for b in proof.checkable(blocks):
        visible = proof.mask(b.text).replace("\0", " ")     # same length: offsets carry over
        for base, piece in _pieces(visible, CHUNK):
            items.append((b, base, piece))
    proposed, batch, size = [], [], 0
    state = {"checked": 0, "failed": None}

    def run(batch):
        joined, starts, pos = [], [], 0
        for b, base, piece in batch:
            starts.append((pos, b, base, piece))
            joined.append(piece)
            pos += len(piece) + 2
        text = "\n\n".join(joined)
        to_index = _utf16_to_index(text)
        for m in lt.check(text):
            u_off, u_len = m.get("offset", 0), m.get("length", 0)
            reps = [r.get("value") for r in (m.get("replacements") or []) if r.get("value")]
            if not reps or u_len <= 0 or u_off + u_len >= len(to_index):
                continue
            off = to_index[u_off]
            length = to_index[u_off + u_len] - off
            owner = next(((s, b, base, piece) for s, b, base, piece in reversed(starts) if s <= off), None)
            if owner is None:
                continue
            s0, b, base, piece = owner
            a, z = base + off - s0, base + off - s0 + length
            if off - s0 + length > len(piece) or "\0" in proof.mask(b.text)[a:z]:
                continue
            flagged = b.text[a:z]
            category = ((m.get("rule") or {}).get("category") or {}).get("id", "")
            if category == "TYPOS" and flagged.lower().strip("'’") in known:
                continue
            s, e = _widen(b.text, a, z)
            if s is None:
                continue
            find = b.text[s:e]
            replace = b.text[s:a] + reps[0] + b.text[z:e]
            why = (m.get("shortMessage") or m.get("message") or "fix").rstrip(".")
            proposed.append({"block": b.id, "find": find, "replace": replace,
                             "why": " ".join(why.split())[:60]})

    def attempt(batch):
        if state["failed"]:
            return
        try:
            run(batch)
            state["checked"] += len({id(b) for b, _, _ in batch})
        except CheckError as e:
            if not state["checked"]:
                raise                        # nothing checked at all: the pass fails, says why
            state["failed"] = str(e)

    for item in items:
        if batch and size + _bytes(item[2]) + 2 > CHUNK:
            attempt(batch)
            batch, size = [], 0
        batch.append(item)
        size += _bytes(item[2]) + 2
    if batch:
        attempt(batch)
    if state["failed"]:
        log("warn", f"only part of the issue was checked: {state['failed']}")
    log("info", f"LanguageTool suggested {len(proposed)} fix(es)")
    return proposed


def quick_check(blocks, lt, known=frozenset(), log=lambda level, text: None):
    """(new blocks, applied fixes, dropped). No model, no tokens."""
    if not proof.checkable(blocks):
        raise ValueError("there is no text to check yet")
    if proof.duplicate_ids(blocks):
        raise ValueError(proof.duplicate_message(blocks))
    proposed = fixes(blocks, lt, known, log)
    new_blocks, applied, dropped = proof.apply_fixes(blocks, proposed)
    for fx, why in dropped:
        log("warn", f"#{fx['block']}: skipped “{fx['find'][:60]}”: {why}")
    log("ok", f"{len(applied)} fix(es) applied" + (f", {len(dropped)} skipped" if dropped else ""))
    return new_blocks, applied, dropped
