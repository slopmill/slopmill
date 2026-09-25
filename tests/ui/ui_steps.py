# SPDX-License-Identifier: MIT
"""SPEC-STEPS in a real browser: what each step is for, drafts only on Draft, and the Edit box
on Draft and Proof that saves and locks. Needs a server on :8441 started with
tests/fake_llm.py (FAKE_LLM_DELAY=2 is plenty) and a workspace seeded with --seed first.
Prints PASS/FAIL per check and exits non-zero on any failure.

    python3 tests/ui/ui_steps.py --seed WORKSPACE     # before starting the server
    python3 tests/ui/ui_steps.py OUT_DIR TOKEN WORKSPACE
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8441"
SLUG = "060-steps"
FAILS = []

ISSUE = """---
number: 60
slug: 060-steps
title: Steps test
subject: Steps test
preview_text: A test
og_image: https://example.com/x.jpg
issue_date: 2026-09-24
---

{#b-open}
The opening paragraph, in my own words.

::: {.prompt #b-pr1}
Say something about the weather. About 40 words.
:::

::: {.prompt #b-pr2}
A second prompt, written in the same pass.
:::

""" + "\n\n".join(f"{{#b-f{n:03d}}}\n" + f"Filler paragraph {n} so the page scrolls. " * 4
                   for n in range(10)) + """

{#b-last}
The closing line, also mine.
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
    md = os.path.join(ws, "issues", SLUG, "issue.md")
    read = lambda: open(md, encoding="utf-8").read()  # noqa: E731
    os.makedirs(out, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1360, "height": 900}, color_scheme="light")
        page = await ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(f"{BASE}/?t={token}#{SLUG}")
        await page.wait_for_selector(".blk")

        # 1 what each step is for
        tabs = await page.evaluate("[...document.querySelectorAll('.steps button')].map(b => b.textContent.trim())")
        check("1 tabs read Plan · Draft · Proof, with nothing under them", tabs[:3] == ["1 · Plan", "2 · Draft", "3 · Proof"], str(tabs))

        # 3 the button says what it will do
        check("3 Plan offers Write the drafts", (await page.inner_text("button.cta")).startswith("Write the drafts"))
        await page.click("button.cta")
        await page.wait_for_selector(".filling", timeout=5000)
        await page.wait_for_selector(".dblk.draft", timeout=30000)
        await page.wait_for_timeout(3500)
        screen = await page.evaluate("document.querySelector('.steps button[aria-selected=true]').dataset.screen")
        check("4 after writing, the screen stays on Draft", screen == "build", screen)
        await page.screenshot(path=f"{out}/1-draft.png", full_page=True)

        # 5 whose words are whose
        tags = await page.evaluate("[...document.querySelectorAll('#flow .dblk .tag > span:first-child, #flow .waiting .tag')].map(t => t.textContent)")
        check("5 your words are tagged locked", tags.count("Your words · locked") == 12, str(tags[:4]))
        check("5 the draft is tagged as written from your prompt", any(t.startswith("Draft · written from your prompt") for t in tags), str(tags))
        frm = await page.inner_text(".dblk.draft .from")
        check("5 the draft shows its prompt above it", frm.startswith("Prompt: Say something about the weather"), frm)
        order = await page.evaluate("[...document.querySelectorAll('#flow > [data-id]')].map(n => n.className.split(' ')[1] + ':' + n.dataset.id).slice(0, 4)")
        check("5 blocks are in the file's order", order[0] == "prose:b-open" and order[1].startswith("draft:")
              and order[2].startswith("draft:") and order[3] == "prose:b-f000", str(order))

        # 2 Plan has no drafts; a drafted prompt says so and links to it
        await page.click("button[data-screen=compose]")
        await page.wait_for_selector(".blk")
        n_drafts = await page.evaluate("document.querySelectorAll('#blocks .blk.draft').length")
        check("2 Plan shows no drafts", n_drafts == 0, str(n_drafts))
        drafted = await page.inner_text(".blk.prompt.has-draft .drafted")
        check("2 a drafted prompt says Drafted", drafted.lower().startswith("drafted"), drafted)
        await page.click(".blk.prompt.has-draft .drafted .linkish")
        await page.wait_for_selector("#flow .dblk.draft")
        screen = await page.evaluate("document.querySelector('.steps button[aria-selected=true]').dataset.screen")
        check("2 Read it on Draft opens Draft", screen == "build", screen)
        # stale
        await page.click("button[data-screen=compose]")
        await page.click(".blk.prompt.has-draft textarea")
        await page.keyboard.press("End")
        await page.keyboard.type(" And mention rain.")
        await page.wait_for_function("() => document.querySelector('.blk.prompt.has-draft .drafted.stale')", timeout=8000)
        check("2 a changed prompt says Changed since its draft", "changed since its draft" in (await page.inner_text(".drafted.stale")).lower())
        check("3 Plan offers Write the drafts again", (await page.inner_text("button.cta")).startswith("Write the drafts"))

        # 6 Edit / Cancel on Draft
        await page.click("button[data-screen=build]")
        await page.wait_for_selector("#flow .dblk.prose")
        before = read()
        await page.click("#flow .dblk.prose[data-id=b-open] button:has-text('Edit')")
        box = await page.input_value(".edit-box")
        check("6 Edit opens a box holding the block's text", box == "The opening paragraph, in my own words.", box)
        label = await page.inner_text(".dblk.editing .edit-row .send")
        check("6 your words save with Save (not lock)", label == "Save", label)
        await page.fill(".edit-box", "Throw this away.")
        # 11 one box at a time; leaving is refused while it has changes
        await page.click("#flow .dblk.prose[data-id=b-last] button:has-text('Edit')")
        still = await page.evaluate("document.querySelector('.dblk.editing').dataset.id")
        check("11 a second Edit is refused while the first has changes", still == "b-open", still)
        await page.click("button[data-screen=review]")
        screen = await page.evaluate("document.querySelector('.steps button[aria-selected=true]').dataset.screen")
        check("11 leaving Draft with unsaved changes is refused", screen == "build", screen)
        await page.click(".dblk.editing button:has-text('Cancel')")
        after = read()
        view = await page.inner_text("#flow .dblk[data-id=b-open] .body")
        check("6 Cancel puts the view back and saves nothing", before == after and "opening paragraph" in view)

        # 8 + 9 save prose, several paragraphs
        await page.click("#flow .dblk.prose[data-id=b-open] button:has-text('Edit')")
        await page.fill(".edit-box", "My new opening.\n\nAnd a second paragraph.")
        await page.keyboard.press("Control+Enter")
        await page.wait_for_function("() => !document.querySelector('.dblk.editing')", timeout=8000)
        await page.wait_for_timeout(600)
        text = read()
        check("8 the prose was saved with its ID", "{#b-open}\nMy new opening." in text)
        check("9 the second paragraph became its own block", text.count("And a second paragraph.") == 1
              and "My new opening.\n\n{#b-" in text)

        # 13 Lock (was Keep as my words) is on the draft; 7 Save & lock through the box
        acts = await page.evaluate("[...document.querySelectorAll('#flow .dblk.draft .acts button')].map(b => b.textContent)")
        check("13 a draft offers Edit, Lock, Rewrite, Discard", acts[:2] == ["Edit", "Lock"] and "Discard" in acts, str(acts))
        await page.click("#flow .dblk.draft button:has-text('Edit')")
        label = await page.inner_text(".dblk.editing .edit-row .send")
        check("6 a draft saves with Save & lock", label == "Save & lock", label)
        await page.fill(".edit-box", "The weather was mine to describe after all.")
        await page.screenshot(path=f"{out}/2-editing-draft.png", full_page=True)
        await page.click(".dblk.editing button:has-text('Save & lock')")
        await page.wait_for_function("() => !document.querySelector('.dblk.editing')", timeout=8000)
        await page.wait_for_timeout(600)
        text = read()
        check("7 the edited draft is saved as your words", "The weather was mine to describe after all." in text
              and ".draft" not in text.split("A second prompt")[0])
        check("7 its prompt is gone", "b-pr1" not in text)
        await page.click("button[data-screen=compose]")
        plan = await page.evaluate("[...document.querySelectorAll('#blocks .blk')].map(b => b.className.split(' ')[1] + ':' + b.dataset.id)")
        check("7 Plan shows it as prose", any(x.startswith("prose:") for x in plan) and not any("b-pr1" in x for x in plan), str(plan[:4]))

        # 10 the block changed since the box opened: refused, the text stays
        await page.click("button[data-screen=build]")
        await page.click("#flow .dblk.prose[data-id=b-last] button:has-text('Edit')")
        await page.fill(".edit-box", "My careful rewrite of the ending.")
        with open(md, encoding="utf-8") as f:
            cur = f.read()
        with open(md, "w", encoding="utf-8") as f:       # another computer changes the same block
            f.write(cur.replace("The closing line, also mine.", "Changed elsewhere."))
        await page.click(".dblk.editing .edit-row .send")
        await page.wait_for_function("() => document.querySelector('.edit-msg.bad')", timeout=8000)
        msg = await page.inner_text(".edit-msg.bad")
        kept = await page.input_value(".edit-box")
        check("10 a block changed elsewhere is refused and says so", "changed since you opened it" in msg, msg)
        check("10 the refused text stays in the box", kept == "My careful rewrite of the ending.", kept)
        check("10 nothing was written over the other change", "Changed elsewhere." in read() and "careful rewrite" not in read())
        await page.fill(".edit-box", "")
        await page.click(".dblk.editing .edit-row .send")
        msg = await page.inner_text(".edit-msg")
        check("10 an empty box is refused with a reason", "empty" in msg, msg)
        await page.click(".dblk.editing button:has-text('Cancel')")

        # 12 + 15 Proof: click a block, Comment and Edit text; the dialog saves; scroll kept
        await page.click("button[data-screen=review]")
        await page.wait_for_function("() => { const d = document.querySelector('#preview-frame')?.contentDocument;"
                                     " return d && d.querySelector('#issue-body [data-block]'); }", timeout=15000)
        await page.wait_for_timeout(800)
        title = await page.inner_text(".doc-title")
        check("15 the Proof title says proof", title.endswith("· proof"), title)
        await page.evaluate("window.scrollTo(0, 400)")
        await page.wait_for_timeout(300)
        pt = await page.evaluate("""() => { const f = document.querySelector('#preview-frame'); const d = f.contentDocument;
            const n = d.querySelector('[data-block=b-f005]'); const r = n.getBoundingClientRect(); const fr = f.getBoundingClientRect();
            return {x: fr.left + r.left + 30, y: fr.top + r.top + 6}; }""")
        await page.mouse.click(pt["x"], pt["y"])
        await page.wait_for_selector("#pop-edit:visible", timeout=3000)
        labels = [await page.inner_text("#pop-open"), await page.inner_text("#pop-edit")]
        check("12 clicking a block offers Comment and Edit text", labels == ["COMMENT", "EDIT TEXT"] or [l.lower() for l in labels] == ["comment", "edit text"], str(labels))
        await page.click("#pop-edit")
        await page.wait_for_selector("#modal:not([hidden]) .edit-box")
        await page.fill("#modal .edit-box", "Filler five, rewritten by hand in the proof.")
        y0 = await page.evaluate("window.scrollY")
        await page.click("#modal .edit-row .send")
        await page.wait_for_function("() => document.querySelector('#modal').hidden", timeout=8000)
        await page.wait_for_function("() => { const d = document.querySelector('#preview-frame')?.contentDocument;"
                                     " return d && d.body && d.body.textContent.includes('rewritten by hand in the proof'); }", timeout=8000)
        await page.wait_for_timeout(600)
        y1 = await page.evaluate("window.scrollY")
        check("12 the dialog saved and the preview shows the new text", "rewritten by hand in the proof" in read())
        check("12 the page stayed where it was", abs(y1 - y0) < 40, f"{y0} -> {y1}")
        await page.screenshot(path=f"{out}/3-proof-after-edit.png")
        # selecting words offers Comment and Edit this block
        await frame_eval(page, """const n = d.querySelector('[data-block=b-f006]'); const t = n.firstChild;
            const r = d.createRange(); r.setStart(t, 0); r.setEnd(t, 12); const s = d.getSelection(); s.removeAllRanges(); s.addRange(r);
            d.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));""")
        await page.wait_for_selector("#pop-edit:visible", timeout=3000)
        lbl = (await page.inner_text("#pop-edit")).lower()
        check("12 selecting words offers Edit this block", lbl == "edit this block", lbl)
        await page.click("#pop-edit")
        await page.wait_for_selector("#modal:not([hidden]) .edit-box")
        await page.fill("#modal .edit-box", "Unsaved words in the dialog.")
        await page.keyboard.press("Escape")
        still = await page.evaluate("!document.querySelector('#modal').hidden")
        check("11 Escape does not throw away changes in the dialog", still)
        await page.click("#modal .edit-row button:has-text('Cancel')")
        closed = await page.evaluate("document.querySelector('#modal').hidden")
        check("12 Cancel closes the dialog without saving", closed and "Unsaved words" not in read())

        await page.set_viewport_size({"width": 390, "height": 800})
        await page.wait_for_timeout(200)
        wide = await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        check("no sideways scroll at 390px", wide)

        check("no page errors", not errors, str(errors[:3]))
        await b.close()


if __name__ == "__main__":
    if sys.argv[1] == "--seed":
        seed(sys.argv[2])
        sys.exit(0)
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
    print(f"{len(FAILS)} failed" if FAILS else "all passed")
    sys.exit(1 if FAILS else 0)
