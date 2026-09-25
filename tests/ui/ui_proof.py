# SPDX-License-Identifier: MIT
"""SPEC-PROOF in a real browser: the Plan · Draft · Proof names, and the spelling and grammar
check: highlights, click to switch back and forth, the side list, Done. Needs a server on
:8441 started with tests/fake_llm.py (which answers proof calls) and a workspace holding
issues/050-proof/issue.md written by this script's --seed. Prints PASS/FAIL per check.

    python3 tests/ui/ui_proof.py --seed WORKSPACE     # before starting the server
    python3 tests/ui/ui_proof.py OUT_DIR TOKEN WORKSPACE
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8441"
SLUG = "050-proof"
FAILS = []

ISSUE = """---
number: 50
slug: 050-proof
title: Proof test
subject: Proof test
preview_text: A test
og_image: https://example.com/x.jpg
issue_date: 2026-09-24
---

{#b-aaaa}
It was made using claude code and it's kinda meh at at the same time.

""" + "\n\n".join(f"{{#b-f{n:03d}}}\n" + f"Filler paragraph {n} so the page is long enough to scroll. " * 3
                   for n in range(12)) + """

{#b-cccc}
Their going to love it, and I put it on git hub for you.
"""


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        FAILS.append(name)


def seed(ws):
    d = os.path.join(ws, "issues", SLUG)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "issue.md"), "w", encoding="utf-8") as f:
        f.write(ISSUE)


async def frame_eval(page, js):
    return await page.evaluate("(js) => { const d = document.querySelector('#preview-frame').contentDocument;"
                               " return (new Function('d', js))(d); }", js)


async def main(out, token, ws):
    issue_md = os.path.join(ws, "issues", SLUG, "issue.md")
    os.makedirs(out, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await (await b.new_context(viewport={"width": 1440, "height": 900})).new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(f"{BASE}/?t={token}#{SLUG}")
        await page.wait_for_selector(".blk")

        # 1: the names
        tabs = await page.evaluate("[...document.querySelectorAll('.steps button')].map(b => b.firstChild.textContent.trim())")
        check("1 tabs read Plan · Draft · Proof", tabs[:3] == ["1 · Plan", "2 · Draft", "3 · Proof"], str(tabs))
        seen = await page.evaluate("document.body.innerText")
        await page.click("button[data-screen=build]")
        seen += await page.evaluate("document.body.innerText")
        await page.click("button[data-screen=review]")
        await page.wait_for_selector("#preview-frame")
        seen += await page.evaluate("document.body.innerText")
        stale = [w for w in ("Compose", "Review the issue", "Build preview", "see Review") if w in seen]
        check("1 no old step names on any screen", not stale, str(stale))

        # 2-4 in the browser: check, highlights
        await page.click("button:has-text('Check with AI')")
        await page.wait_for_selector(".proof-busy", timeout=5000)
        check("the panel says it is checking", True)
        await page.wait_for_function("() => { const d = document.querySelector('#preview-frame').contentDocument;"
                                     " return d && d.querySelectorAll('mark.fix').length >= 4; }", timeout=60000)
        marks = await frame_eval(page, "return [...d.querySelectorAll('mark.fix')].map(m => [m.dataset.fid, m.className, m.textContent]);")
        check("10 every fix is highlighted", len({m[0] for m in marks}) == 4, str(marks))
        curly = [m for m in marks if "They’re" in m[2]]
        check("10 a fix with an apostrophe is found on the typeset page", bool(curly), str(marks))
        text = open(issue_md, encoding="utf-8").read()
        check("3 the fixes are in the file", "GitHub" in text and "They're going" in text and "at the same" in text
              and "at at" not in text)
        rows = await page.locator(".fix-row").count()
        check("the side list shows each fix", rows == 4, str(rows))

        # 5 and 10: click a highlight → original; again → fix; the page stays put
        gh = next(m for m in marks if "GitHub" in m[2])
        mark = f"mark.fix[data-fid='{gh[0]}']"
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(300)
        y0 = await page.evaluate("window.scrollY")
        box = await frame_eval(page, f"const r = d.querySelector(\"{mark}\").getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2];")
        fr = await page.evaluate("(() => { const r = document.querySelector('#preview-frame').getBoundingClientRect(); return [r.x, r.y]; })()")
        await page.mouse.click(fr[0] + box[0], fr[1] + box[1])
        await page.wait_for_function(f"() => {{ const m = document.querySelector('#preview-frame').contentDocument.querySelector(\"{mark}\");"
                                     " return m && m.classList.contains('orig'); }", timeout=10000)
        got = await frame_eval(page, f"const m = d.querySelector(\"{mark}\"); return m.textContent;")
        check("10 clicking a highlight shows the original words", "git hub" in got, got)
        check("5 the original words are back in the file", "put it on git hub for you" in open(issue_md, encoding="utf-8").read())
        y1 = await page.evaluate("window.scrollY")
        check("the page stays where it was", abs(y1 - y0) < 60, f"{y0} → {y1}")
        box = await frame_eval(page, f"const r = d.querySelector(\"{mark}\").getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2];")
        fr = await page.evaluate("(() => { const r = document.querySelector('#preview-frame').getBoundingClientRect(); return [r.x, r.y]; })()")
        await page.mouse.click(fr[0] + box[0], fr[1] + box[1])
        await page.wait_for_function(f"() => {{ const m = document.querySelector('#preview-frame').contentDocument.querySelector(\"{mark}\");"
                                     " return m && !m.classList.contains('orig'); }", timeout=10000)
        check("10 clicking again restores the fix", "put it on GitHub for you" in open(issue_md, encoding="utf-8").read())
        popped = await page.evaluate("!document.querySelector('#comment-pop') || document.querySelector('#comment-pop').hidden || getComputedStyle(document.querySelector('#comment-pop')).display === 'none'")
        check("a click on a highlight does not open the note box", popped)

        # the side list toggles the same way
        await page.click(f".fix-row[data-fid='{gh[0]}']")
        await page.wait_for_selector(f".fix-row.off[data-fid='{gh[0]}']", timeout=10000)
        check("10 the side list switches a fix too", "git hub" in open(issue_md, encoding="utf-8").read())
        await page.click(f".fix-row[data-fid='{gh[0]}']")
        await page.wait_for_selector(f".fix-row.on[data-fid='{gh[0]}']", timeout=10000)
        await page.screenshot(path=os.path.join(out, "proof.png"), full_page=False)

        # 10: Done
        await page.click(".proof button:has-text('Done')")
        await page.wait_for_selector("button:has-text('Check with AI')", timeout=10000)
        await page.wait_for_timeout(800)
        left = await frame_eval(page, "return d.querySelectorAll('mark.fix').length;")
        check("10 Done removes the highlights", left == 0, str(left))
        check("Done keeps the text", "GitHub" in open(issue_md, encoding="utf-8").read())
        check("no page errors", not errors, "; ".join(errors[:3]))
        await b.close()
    print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
    return 1 if FAILS else 0


if __name__ == "__main__":
    if sys.argv[1] == "--seed":
        seed(sys.argv[2])
        sys.exit(0)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3])))
