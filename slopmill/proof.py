# SPDX-License-Identifier: AGPL-3.0-or-later
"""Proof: a spelling and grammar pass over the issue's text (SPEC-PROOF.md).

The model names each mistake as FIND/REPLACE on one block. A fix is written into the issue
only when FIND is plain text that occurs exactly once in the block's visible words, so it
can never touch a link address, an image name, a class or an ID. Each applied fix keeps a
`before` and an `after` widened with neighbouring words until each is unique, so the author
can switch any one of them back to the original and forward again later.
"""
import os
import re
import tempfile
import uuid

from . import agent, doc
from .providers import Cancelled, LLMError, usage_of

CHECKED = ("prose", "draft", "component")
MAX_FIX = 300
MIN_SHOWN = 14      # characters: a highlight shorter than this is hard to see, and short
                    # strings like "at" also occur inside the page's other words
# Markdown syntax: a fix containing any of these could change the structure, not a word.
FORBIDDEN_RE = re.compile(r"[*_\[\]{}<>`|#~\\\n]|:::")
FIX_RE = re.compile(r"^=== FIX #?([A-Za-z][A-Za-z0-9_-]*) ===[ \t]*\n(.*?)\n=== END ===", re.S | re.M)
# Stretches of a block's Markdown that are not words on the page.
HIDDEN_RES = [
    re.compile(r"\{[^}\n]*\}"),                   # attribute blocks, accents, classes
    re.compile(r"(`+)[^`]*?\1"),                  # inline code
    re.compile(r"<[^>\n]*>"),                     # HTML tags and <autolinks>
    # bare addresses, with balanced (...) inside, but not the full stop after
    re.compile(r"https?://(?:[^\s<>()\[\]]|\([^\s<>()]*\))*(?:[^\s<>()\[\].,;:!?'\"]|\([^\s<>()]*\))"),
    re.compile(r"^[ \t]*:::.*$", re.M),           # fenced-div lines
    re.compile(r"^[ \t]*\[[^\]\n]+\]:[ \t]*\S.*$", re.M),   # reference definitions
    # A reference link's label is not on the page: the [label] of [text][label], and the
    # whole of [text][] and a bare [text] (their text IS the label, so it must not change).
    re.compile(r"(?<=\])\[[^\[\]\n]+\]"),
    re.compile(r"\[[^\[\]\n]*\]\[\]"),
    re.compile(r"(?<!\])\[[^\[\]\n]*\](?![(\[:])"),
    # Block quotes are someone else's words: never corrected
    re.compile(r"^[ \t]*>.*$", re.M),
    # HTML entities are one character on the page: a fix never contains or crosses one
    re.compile(r"&(?:[A-Za-z][A-Za-z0-9]*|#[0-9]+|#[xX][0-9A-Fa-f]+);"),
]

BRIEF = """
You are proofreading an issue of a newsletter for its author. You fix mistakes; you never
edit. The attached voice rules describe how the author writes on purpose: nothing they
describe is a mistake.

Fix only these:
- misspelled words (not deliberate ones: a misspelling that is the joke stays)
- doubled words ("at at"), and a missing or extra word that leaves a sentence broken
- the wrong word for the sound: their/there/they're, its/it's, your/you're, then/than
- subject-verb agreement and wrong verb forms
- the capitalisation of names: products, companies, people, places (GitHub, Claude Code)
- British spellings: the newsletter is written in US English

Never change:
- word choice, tone, rhythm or sentence length; casual words ("kinda", "gonna", "Ok")
- sentence fragments, sentences that start with And, But or So, run-on sentences he
  clearly meant, rhetorical questions
- punctuation style: commas, ellipses of any length, exclamation marks, emoji
- playful casing ("ViBe CoDinG"), words in capitals for emphasis
- anything in a quotation of someone else
- link addresses, image names, or anything inside { } or ( ) after a link

The attached DOCUMENT.md lists the blocks as [PROSE #id], [DRAFT #id] and [COMPONENT #id].
Answer with one block per mistake, in document order:
=== FIX #<block id> ===
FIND: <words copied exactly from that block, enough of them to appear only once in it>
REPLACE: <the same words with only the mistake corrected>
WHY: <two to five words: "doubled word", "spelling", "name: GitHub", "its/it's">
=== END ===
FIND and REPLACE are plain text on one line, with no Markdown symbols (* _ [ ] { } # ` < >).
If there is nothing to fix, answer only: === NONE ===
""".strip()


class Stale(ValueError):
    """The words a fix would switch are no longer in the block exactly once."""


def checkable(blocks):
    return [b for b in blocks if b.type in CHECKED and b.text.strip()]


def duplicate_ids(blocks):
    seen, dup = set(), []
    for b in blocks:
        if b.id and b.id in seen and b.id not in dup:
            dup.append(b.id)
        seen.add(b.id)
    return dup


def duplicate_message(blocks):
    ids = ", ".join("#" + i for i in duplicate_ids(blocks))
    return f"two blocks share the ID {ids}; give one of them a new ID before checking"


def rules_files(voice):
    """The voice pack's rules files that are on: what the author does on purpose."""
    if voice is None:
        return []
    return [(e["path"], voice.path(e["path"])) for e in voice.files()
            if e["on"] and not e["missing"] and e["role"] == "rules"]


def document(blocks):
    parts = []
    for b in checkable(blocks):
        tag = f"[COMPONENT #{b.id} {b.attrs.get('class', '')}]".replace(" ]", "]") \
            if b.type == "component" else f"[{b.type.upper()} #{b.id}]"
        parts.append(f"{tag}\n{b.text.strip()}")
    return "\n\n".join(parts) + "\n"


def request_size(voice, blocks):
    ask = ask_text(blocks)
    return (len(BRIEF.encode()) + len(ask.encode())
            + sum(os.path.getsize(p) + agent.FRAMING for _, p in rules_files(voice))
            + len(document(blocks).encode()) + agent.FRAMING)


def ask_text(blocks):
    return (f"Proofread these {len(checkable(blocks))} blocks. Mistakes only; the author's "
            f"voice is not a mistake.")


# ── where the words are ─────────────────────────────────────────────────────────

def _destinations(text):
    """(start, end) of every link or image destination, from "](" to its closing ")":
    balanced parentheses and backslash escapes count, as in CommonMark."""
    out, i = [], text.find("](")
    while i >= 0:
        j, depth = i + 2, 1
        while j < len(text) and text[j] != "\n":
            ch = text[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        end = min(j + 1, len(text))
        out.append((i, end))
        i = text.find("](", end)
    return out


def mask(text):
    """text with every character that is not a word on the page replaced by NUL.
    Same length as text, so positions carry over."""
    chars = list(text)
    for a, b in _destinations(text):
        for i in range(a, b):
            chars[i] = "\0"
    for rx in HIDDEN_RES:
        for m in rx.finditer(text):
            for i in range(m.start(), m.end()):
                chars[i] = "\0"
    if re.search(r"^( {4}|\t)|```|~~~", text, re.M):
        lines = text.split("\n")
        code = agent.code_lines(text)
        pos = 0
        for n, ln in enumerate(lines, 1):
            if n in code:
                for i in range(pos, pos + len(ln)):
                    chars[i] = "\0"
            pos += len(ln) + 1
    return "".join(chars)


def visible_at(text, needle, masked=None):
    """Every start position of needle in text where all of it is visible words."""
    masked = mask(text) if masked is None else masked
    out, i = [], text.find(needle)
    while needle and i >= 0:
        if "\0" not in masked[i:i + len(needle)]:
            out.append(i)
        i = text.find(needle, i + 1)
    return out


def straight(t):
    """Curly quotes as straight ones: the page typesets ' and " and the highlighter
    matches on straightened text, so a fix must be unique that way too."""
    return t.translate({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"'})


def shown_count(text, needle, masked):
    """How often needle appears in the words as the page shows them: hidden stretches and
    Markdown marks gone, quotes straightened, whitespace collapsed. Stricter than the file,
    never looser, so a highlight finds this stretch and no other."""
    shown = "".join(ch for ch, m in zip(straight(text), masked) if m != "\0" and ch not in "*_[]`")
    shown = re.sub(r"\s+", " ", shown)
    n = re.sub(r"\s+", " ", straight(needle))
    count, i = 0, shown.find(n)
    while n and i >= 0:
        count += 1
        i = shown.find(n, i + 1)
    return count


def plain(s):
    return bool(s) and len(s) <= MAX_FIX and not FORBIDDEN_RE.search(s)


# ── the model's answer ──────────────────────────────────────────────────────────

def parse_fixes(reply):
    out = []
    for m in FIX_RE.finditer(reply or ""):
        fields = {}
        for line in m.group(2).split("\n"):
            f = re.match(r"^\s*(FIND|REPLACE|WHY)\s*:\s?(.*)$", line)
            if f:
                fields[f.group(1).lower()] = f.group(2).rstrip()
        out.append({"block": m.group(1), "find": fields.get("find", ""),
                    "replace": fields.get("replace", ""), "why": " ".join(fields.get("why", "").split())[:80]})
    return out


def apply_fixes(blocks, proposed):
    """Apply the model's fixes. Returns (new blocks, applied fixes, dropped [(fix, why)])."""
    by_id = {b.id: b for b in blocks}
    wanted, dropped = {}, []
    for fx in proposed:
        b = by_id.get(fx["block"])
        why = None
        if b is None:
            why = "no such block"
        elif b.type not in CHECKED:
            why = f"a {b.type} block is not checked"
        elif not plain(fx["find"]) or not plain(fx["replace"]):
            why = "not plain text on one line"
        elif fx["find"] == fx["replace"]:
            why = "changes nothing"
        if why:
            dropped.append((fx, why))
        else:
            wanted.setdefault(b.id, []).append(fx)

    texts, applied = {}, []
    for b in blocks:
        if b.id not in wanted:
            continue
        text, items = b.text, []          # item: the fix and its span in the current text
        for fx in wanted[b.id]:
            hits = visible_at(text, fx["find"])
            if len(hits) != 1:
                dropped.append((fx, "not found in the words of that block" if not hits
                                else "appears more than once in that block"))
                continue
            # Only the letters that change belong to this fix; the words around them are
            # context, which a neighbouring fix may share ("recieve teh" then "teh thing").
            f, r = fx["find"], fx["replace"]
            p = 0
            while p < min(len(f), len(r)) and f[p] == r[p]:
                p += 1
            q = 0
            while q < min(len(f), len(r)) - p and f[-1 - q] == r[-1 - q]:
                q += 1
            s, e = hits[0] + p, hits[0] + len(f) - q
            core_old, core_new = f[p:len(f) - q], r[p:len(r) - q]
            if any(_collide(s, e, it["s"], it["e"]) for it in items):
                dropped.append((fx, "overlaps another fix"))
                continue
            _shift(items, e, len(core_new) - len(core_old))
            items.append({**fx, "find": core_old, "replace": core_new, "s": s, "e": s + len(core_new)})
            text = text[:s] + core_new + text[e:]
        # Every fix needs words around it that tell it apart; one that has none goes back
        # to the original, and the rest are widened again against the text that leaves.
        while True:
            found, bad = {}, None
            for it in items:
                w = widen(text, it["s"], it["e"], it["find"],
                          [(o["s"], o["e"]) for o in items if o is not it])
                if w is None:
                    bad = it
                    break
                found[id(it)] = w
            if bad is None:
                break
            text = text[:bad["s"]] + bad["find"] + text[bad["e"]:]
            items.remove(bad)
            _shift(items, bad["e"], len(bad["find"]) - (bad["e"] - bad["s"]))
            dropped.append(({**bad, "find": fx_find(bad)}, "its words are not unique enough to switch back"))
        if not items:
            continue
        texts[b.id] = text
        for it in items:
            before, after = found[id(it)]
            applied.append({"id": "fx-" + uuid.uuid4().hex[:8], "block": b.id, "before": before,
                            "after": after, "why": it["why"], "applied": True})
    new_blocks = [doc.Block(b.type, b.id, texts[b.id], dict(b.attrs)) if b.id in texts else b
                  for b in blocks]
    return new_blocks, applied, dropped


def _collide(s, e, s2, e2):
    """Changed spans that share a character; an empty span (an insertion) collides with a
    span it touches, since which comes first would be ambiguous."""
    if s == e or s2 == e2:
        return s2 <= s <= e2 or s <= s2 <= e
    return s < e2 and s2 < e


def fx_find(item):
    return item["find"] or item.get("why", "")


def _shift(items, at, delta):
    for it in items:
        if it["s"] >= at:
            it["s"] += delta
            it["e"] += delta


def widen(final, s, e, original, others):
    """(before, after) for the fix at final[s:e]: the original words and the corrected
    words, both with the same neighbouring words added until each is unique in its own
    version of the text. Never grows into another fix or across a line. None if no such
    context exists."""
    orig_text = final[:s] + original + final[e:]
    fm, om = mask(final), mask(orig_text)
    left, right = s, e
    # start from whole words: the changed letters may sit inside one
    while left > 0 and final[left - 1] not in " \n" and fm[left - 1] != "\0" \
            and not FORBIDDEN_RE.search(final[left - 1]) and not any(os_ < left <= oe for os_, oe in others):
        left -= 1
    while right < len(final) and final[right] not in " \n" and fm[right] != "\0" \
            and not FORBIDDEN_RE.search(final[right]) and not any(os_ <= right < oe for os_, oe in others):
        right += 1

    def unique(after, before):
        # once in the file (so a toggle can find it) and once on the page (so a highlight can)
        return (len(visible_at(final, after, fm)) == 1 and len(visible_at(orig_text, before, om)) == 1
                and shown_count(final, after, fm) == 1 and shown_count(orig_text, before, om) == 1)

    while True:
        after = final[left:right]
        before = final[left:s] + original + final[e:right]
        if after.strip() and before.strip() and unique(after, before) \
                and min(len(after), len(before)) >= MIN_SHOWN:
            return before, after
        nl = _word_left(final, fm, left, others)
        nr = _word_right(final, fm, right, others)
        if nl is None and nr is None:
            # Unique but short, and the line ends here: short is fine.
            if after.strip() and before.strip() and unique(after, before):
                return before, after
            return None
        left = left if nl is None else nl
        right = right if nr is None else nr


def _word_left(text, masked, left, others):
    i = left
    while i > 0 and text[i - 1] == " ":
        i -= 1
    while i > 0 and text[i - 1] not in " \n":
        i -= 1
    if i == left or "\0" in masked[i:left] or "\n" in text[i:left] or FORBIDDEN_RE.search(text[i:left]):
        return None
    if any(i < oe and os_ < left for os_, oe in others):
        return None
    return i


def _word_right(text, masked, right, others):
    i = right
    while i < len(text) and text[i] == " ":
        i += 1
    while i < len(text) and text[i] not in " \n":
        i += 1
    if i == right or "\0" in masked[right:i] or "\n" in text[right:i] or FORBIDDEN_RE.search(text[right:i]):
        return None
    if any(right < oe and os_ < i for os_, oe in others):
        return None
    return i


# ── switching one fix back and forth ────────────────────────────────────────────

def toggle(blocks, fix, applied):
    """New blocks with fix switched on (applied=True) or off. Raises Stale when the words
    it would switch are not in the block exactly once."""
    b = next((x for x in blocks if x.id == fix["block"]), None)
    if b is None:
        raise Stale("that block is no longer in the issue")
    old, new = (fix["before"], fix["after"]) if applied else (fix["after"], fix["before"])
    masked = mask(b.text)
    hits = visible_at(b.text, old, masked)
    # once in the file AND once on the page, or the highlight the author clicked may not be
    # the stretch this would change
    if len(hits) != 1 or shown_count(b.text, old, masked) != 1:
        if not hits and len(visible_at(b.text, new, masked)) == 1 and shown_count(b.text, new, masked) == 1:
            return blocks        # already switched (the text was saved, the flag was not)
        raise Stale("that sentence has changed since the check, so this fix can no longer be switched")
    i = hits[0]
    text = b.text[:i] + new + b.text[i + len(old):]
    return [doc.Block(x.type, x.id, text, dict(x.attrs)) if x is b else x for x in blocks]


# ── the pass ────────────────────────────────────────────────────────────────────

def proofread(meta, blocks, llm, *, voice, workdir, cancel=None, log=lambda level, text: None,
              on_usage=lambda u: None):
    """One model call. Returns (new blocks, applied fixes, dropped)."""
    if not checkable(blocks):
        raise ValueError("there is no text to check yet")
    if duplicate_ids(blocks):
        raise ValueError(duplicate_message(blocks))
    os.makedirs(workdir, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=workdir, prefix="run-")
    docfile = os.path.join(tmp, "DOCUMENT.md")
    with open(docfile, "w", encoding="utf-8") as f:
        f.write(document(blocks))
    rules = rules_files(voice)
    files = [p for _, p in rules] + [docfile]
    ask = ask_text(blocks)
    size = len(BRIEF.encode()) + len(ask.encode()) + sum(os.path.getsize(p) + agent.FRAMING for p in files)
    limit = getattr(llm, "max_request_bytes", agent.MAX_PAYLOAD)
    log("info", f"voice rules: {', '.join(n for n, _ in rules) or 'none'}; request {size // 1024}KB of {limit // 1024}KB")
    if size > limit:
        raise LLMError(f"the request is {size // 1024}KB and this model takes at most "
                       f"{limit // 1024}KB: shorten the issue")
    chars = len(BRIEF) + len(ask)
    for p in files:
        with open(p, encoding="utf-8", errors="replace") as f:
            chars += len(f.read())
    try:
        reply = llm(BRIEF, ask, files, cancel=cancel)
    except (LLMError, Cancelled) as e:
        on_usage({"kind": "text", **usage_of("", chars), "out": 0,
                  "outcome": "stopped" if isinstance(e, Cancelled) else "failed"})
        raise
    with open(os.path.join(tmp, "REPLY.md"), "w", encoding="utf-8") as f:
        f.write(str(reply))
    on_usage({"kind": "text", **usage_of(reply, chars)})
    proposed = parse_fixes(str(reply))
    if not proposed and "=== NONE ===" not in str(reply):
        # A cut-off or garbled reply must not read as "no mistakes found".
        raise LLMError("the reply had no fixes and did not say there were none, so it was "
                       "probably cut off; nothing was changed. Try the check again.")
    new_blocks, applied, dropped = apply_fixes(blocks, proposed)
    for fx, why in dropped:
        log("warn", f"#{fx['block']}: skipped “{fx['find'][:60]}”: {why}")
    log("ok", f"{len(applied)} fix(es) applied" + (f", {len(dropped)} skipped" if dropped else ""))
    return new_blocks, applied, dropped
