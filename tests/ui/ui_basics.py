# SPDX-License-Identifier: MIT
"""SPEC-BASICS in a real browser: Background notes, the research switch, the formatting bar,
+ Heading, + Box, moving and deleting blocks, the writer's notes on cards, and the Download
warning. Needs a server on :8441 started with tests/fake_llm.py (FAKE_LLM_DELAY=1 is plenty)
and a workspace seeded with --seed first. Prints PASS/FAIL per check; non-zero on failure.

    python3 tests/ui/ui_basics.py --seed WORKSPACE     # before starting the server
    python3 tests/ui/ui_basics.py OUT_DIR TOKEN WORKSPACE
"""
import asyncio
import os
import re
import sys

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8441"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SLUG = "070-basics"
FAILS = []

ISSUE = """---
number: 70
slug: 070-basics
title: Basics test
subject: null
preview_text: A test
og_image: https://example.com/x.jpg
issue_date: 2026-09-26
---

{#b-one}
The first paragraph, which has a word to make bold.

{#b-two}
The second paragraph.

::: {.prompt #b-pr1}
Say something about the weather. About 40 words.
:::

{#b-three}
The third paragraph, the last one.

::: {.prompt #b-pr2}
Say something about the harbour. About 30 words.
:::
"""


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        FAILS.append(name)


OTHER = ISSUE.replace("number: 70", "number: 71").replace("070-basics", "071-other").replace(
    "title: Basics test", "title: Other issue")


PARA = "This paragraph goes on about one subject for a while, so the section runs long. " * 12
HEADED = f"""---
number: 72
slug: 072-headed
title: Headed issue
subject: A subject
preview_text: A test
og_image: https://example.com/x.jpg
issue_date: 2026-09-26
---

{{#h-one}}
The cold open. {PARA}

{{#h-two}}
The first real section. {PARA}

{{#h-three}}
Another subject entirely. {PARA}
"""


def seed(ws):
    h = os.path.join(ws, "issues", "072-headed")
    os.makedirs(h, exist_ok=True)
    with open(os.path.join(h, "issue.md"), "w", encoding="utf-8") as f:
        f.write(HEADED)
    # A design that asks for section headings: the test design with [headings] added,
    # stored as an uploaded design in the workspace.
    sys.path.insert(0, REPO)
    from slopmill.pack import export_design, load_pack
    text = export_design(load_pack(os.path.join(REPO, "tests", "packs", "fixture")))
    text = text.replace('name = "fixture"', 'name = "headed"', 1)
    text += ('\n[headings]\nadd = true\nlevel = 2\nafter_words = 300\nnot_before = ["fold", "closing"]\n'
             'describe = "Short headings, a line the author would say out loud."\n')
    os.makedirs(os.path.join(ws, "designs"), exist_ok=True)
    with open(os.path.join(ws, "designs", "headed.design.toml"), "w", encoding="utf-8") as f:
        f.write(text)
    with open(os.path.join(h, "settings.json"), "w", encoding="utf-8") as f:
        f.write('{"design": "headed"}')
    o = os.path.join(ws, "issues", "071-other")
    os.makedirs(o, exist_ok=True)
    with open(os.path.join(o, "issue.md"), "w", encoding="utf-8") as f:
        f.write(OTHER)
    d = os.path.join(ws, "issues", SLUG)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "issue.md"), "w", encoding="utf-8") as f:
        f.write(ISSUE)
    for extra in ("background.md", "review.json"):
        if os.path.exists(os.path.join(d, extra)):
            os.remove(os.path.join(d, extra))


async def wait_saved(page):
    await page.wait_for_function("() => document.querySelector('#save-state').textContent === 'Saved'", timeout=8000)


async def main(out, token, ws):
    d = os.path.join(ws, "issues", SLUG)
    read = lambda name="issue.md": open(os.path.join(d, name), encoding="utf-8").read()  # noqa: E731
    order = lambda: re.findall(r"#(b-[\w-]+)", read())  # noqa: E731
    os.makedirs(out, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1360, "height": 900}, color_scheme="light")
        page = await ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(f"{BASE}/?t={token}#{SLUG}")
        await page.wait_for_selector(".blk")

        # B6-8 Background
        check("B6 the Background box is on Plan", await page.is_visible("details.background summary"))
        await page.click("details.background summary")
        await page.fill("#bg-text", "Muse Spark launched April 8, 2026: https://ai.meta.com/blog/introducing-muse-spark-msl/")
        await page.wait_for_function("() => document.querySelector('#bg-state').textContent.includes('saved')", timeout=8000)
        check("B6 it is saved beside the issue", read("background.md").startswith("Muse Spark launched"))
        check("B6 and not in the issue itself", "Muse Spark" not in read())
        check("B7 the request size under the button counts it",
              "sends about" in (await page.inner_text("#cta-note")))
        await page.reload()
        await page.wait_for_selector(".blk")
        kept = await page.input_value("#bg-text")
        opened = await page.evaluate("document.querySelector('details.background').open")
        check("B6 it is still there after a reload, and open", kept.startswith("Muse Spark") and opened)

        # B6 a failed Background save stops a switch to another issue, and the words stay
        async def fail_once(route):
            await route.fulfill(status=500, body='{"error": "disk full"}', content_type="application/json")
        await page.route("**/background", fail_once)
        await page.fill("#bg-text", kept + " Unsaved line.")
        await page.wait_for_function("() => document.querySelector('#bg-state').textContent.includes('not saved')", timeout=8000)
        await page.select_option("#issue-select", "071-other")
        await page.wait_for_timeout(1500)
        title = await page.evaluate("document.querySelector('.doc-title').value")
        still = await page.input_value("#bg-text")
        check("B6 a failed save keeps you on the issue, with your notes", title == "Basics test" and still.endswith("Unsaved line."), title)
        await page.unroute("**/background")
        await page.reload()          # the tab is closed before the notes ever reached the server
        await page.wait_for_selector("#bg-text")
        back = await page.input_value("#bg-text")
        await page.wait_for_function("() => document.querySelector('#bg-state').textContent.includes('saved') && "
                                     "!document.querySelector('#bg-state').textContent.includes('saving')", timeout=8000)
        check("B6 notes that never saved come back after a reload, and save",
              back.endswith("Unsaved line.") and read("background.md").endswith("Unsaved line."), back[-30:])

        # B6 saves land in order: a slow first save cannot overwrite a later one
        slow = {"n": 0}

        async def slow_first(route):
            slow["n"] += 1
            if slow["n"] == 1:
                await asyncio.sleep(2.5)
            await route.continue_()
        await page.route("**/background", slow_first)
        await page.fill("#bg-text", "First version.")
        await page.wait_for_timeout(1000)            # its save is in flight, held
        await page.fill("#bg-text", "Second version.")
        await page.wait_for_function("() => document.querySelector('#bg-state').textContent.includes('saved') && "
                                     "!document.querySelector('#bg-state').textContent.includes('saving')", timeout=12000)
        await page.unroute("**/background")
        check("B6 the later words are what is saved", read("background.md") == "Second version.", read("background.md"))
        await page.select_option("#issue-select", "071-other")
        await page.wait_for_function("() => document.querySelector('.doc-title').value === 'Other issue'", timeout=8000)
        check("B6 once saved, switching works and shows that issue's own notes", await page.input_value("#bg-text") == "")
        await page.select_option("#issue-select", SLUG)
        await page.wait_for_function("() => document.querySelector('.doc-title').value === 'Basics test'", timeout=8000)

        # L title suggestions
        await page.click(".suggest-titles")
        await page.wait_for_selector(".title-pick", timeout=20000)
        picks = await page.evaluate("[...document.querySelectorAll('.title-pick')].map(b => b.textContent)")
        check("L Suggest titles offers titles from the page, cleaned", picks[:3] == ["Birds, Again", "The Porch Light Question", "Small Birds, Big Opinions"], str(picks))
        await page.click(".title-pick:has-text('The Porch Light Question')")
        await wait_saved(page)
        check("L clicking one makes it the title, saved", "title: The Porch Light Question" in read()
              and await page.input_value(".doc-title") == "The Porch Light Question")
        check("L and the suggestions close", not await page.is_visible(".title-picks"))
        await page.fill(".doc-title", "Basics test")
        await wait_saved(page)

        # A1 the research switch
        sw = ".blk.prompt[data-id=b-pr1] .research"
        check("A1 a prompt says it looks things up", (await page.inner_text(sw)).startswith("Looks it up"))
        await page.click(sw)
        await wait_saved(page)
        check("A1 switching it off is saved in the file", "::: {.prompt #b-pr1 research=off}" in read())
        check("A1 and it says so", (await page.inner_text(sw)) == "Your words only")
        await page.click(sw)
        await wait_saved(page)
        check("A1 switching it back on is saved", "::: {.prompt #b-pr1}" in read())

        # 13 the formatting bar, on the paragraph being typed in
        top_before = await page.evaluate("document.querySelector('.blk[data-id=b-two]').getBoundingClientRect().top")
        await page.click(".blk[data-id=b-one] .view")
        shown = await page.evaluate("""() => [...document.querySelectorAll('.fmtbar')].filter(b => b.offsetParent)
            .map(b => b.closest('.blk').dataset.id)""")
        check("13 the bar shows on the paragraph being edited, and only there", shown == ["b-one"], str(shown))
        top_after = await page.evaluate("document.querySelector('.blk[data-id=b-two]').getBoundingClientRect().top")
        bar = await page.evaluate("document.querySelector('.blk[data-id=b-one] .fmtbar').getBoundingClientRect().bottom")
        box = await page.evaluate("document.querySelector('.blk[data-id=b-one]').getBoundingClientRect().top")
        check("13 it sits on the paragraph's top edge", abs(bar - box) <= 4, f"bar bottom {bar}, box top {box}")
        check("13 opening it does not add height of its own", top_after - top_before < 40, f"{top_before} -> {top_after}")
        await page.screenshot(path=f"{out}/fmtbar.png", clip={"x": 0, "y": 280, "width": 900, "height": 260})
        ta = ".blk[data-id=b-one] textarea"
        await page.evaluate("""() => { const t = document.querySelector('.blk[data-id=b-one] textarea');
            const i = t.value.indexOf('bold'); t.setSelectionRange(i, i + 4); t.dispatchEvent(new Event('select')); }""")
        await page.click(".blk.editing .fmtbar button[data-fmt=bold]")
        check("13 Bold wraps the selection", "a word to make **bold**." in await page.input_value(ta))
        await page.click(".blk.editing .fmtbar button[data-fmt=italic]")
        check("13 Italic wraps the selection (still selected)", "***bold***" in await page.input_value(ta))
        page.once("dialog", lambda dlg: asyncio.ensure_future(dlg.accept("https://example.org/page")))
        await page.evaluate("""() => { const t = document.querySelector('.blk[data-id=b-one] textarea');
            const i = t.value.indexOf('first'); t.setSelectionRange(i, i + 5); t.dispatchEvent(new Event('select')); }""")
        await page.click(".blk.editing .fmtbar button[data-fmt=link]")
        check("13 Link asks for the address and links the words",
              "[first](https://example.org/page)" in await page.input_value(ta))
        # Leave b-one first: its box folds back to text, and the page moves under the pointer.
        await page.click("h1, .doc-sub", position={"x": 2, "y": 2})
        await page.wait_for_timeout(200)
        await page.click(".blk[data-id=b-two] .view")
        await page.wait_for_function("() => document.activeElement && document.activeElement.closest && "
                                     "document.activeElement.closest('.blk') && "
                                     "document.activeElement.closest('.blk').dataset.id === 'b-two'", timeout=3000)
        await page.click(".blk.editing .fmtbar button[data-fmt=heading]")
        check("13 Heading makes the block a heading", await page.input_value(".blk[data-id=b-two] textarea") == "## The second paragraph.")
        check("13 and it looks like one", await page.evaluate("document.querySelector('.blk[data-id=b-two]').classList.contains('heading')"))
        await page.click(".blk.editing .fmtbar button[data-fmt=heading]")
        check("13 Heading again turns it back", await page.input_value(".blk[data-id=b-two] textarea") == "The second paragraph.")
        await page.click(".blk.editing .fmtbar button[data-fmt=list]")
        v = await page.input_value(".blk[data-id=b-two] textarea")
        check("13 List starts the line with a dash", v == "- The second paragraph.", repr(v))
        await page.click(".blk.editing .fmtbar button[data-fmt=list]")
        await page.click(".blk.editing .fmtbar button[data-fmt=quote]")
        v = await page.input_value(".blk[data-id=b-two] textarea")
        check("13 Quote starts the line with >", v == "> The second paragraph.", repr(v))
        await page.click(".blk.editing .fmtbar button[data-fmt=quote]")
        await wait_saved(page)
        check("13 the formatting is saved", "**bold**" in read() and "[first](https://example.org/page)" in read())
        await page.click(".blk.prompt[data-id=b-pr1] textarea")
        try:     # the paragraph left behind folds once the press is over
            await page.wait_for_function("() => ![...document.querySelectorAll('.fmtbar')].some(b => b.offsetParent)", timeout=2000)
        except Exception:
            pass
        n_bars = await page.evaluate("[...document.querySelectorAll('.fmtbar')].filter(b => b.offsetParent).length")
        in_prompt = await page.evaluate("!!document.querySelector('.blk.prompt .fmtbar')")
        check("13 a prompt has no formatting bar, and none shows while typing in one", n_bars == 0 and not in_prompt,
              f"{n_bars} visible, in prompt: {in_prompt}")
        in_bar = await page.evaluate("!!document.querySelector('.selbar [data-fmt]')")
        check("13 the shared bar keeps only Prose / Prompt", not in_bar)

        # 14 + Heading (an empty one is not saved)
        await page.click(".add-end button:has-text('+ Heading')")
        await page.click(".blk[data-id=b-one] .view")
        await page.keyboard.press("End")
        await page.keyboard.type(" x")
        await wait_saved(page)
        check("14 a heading with no words is not saved", not re.search(r"^##\s*$", read(), re.M))
        await page.click(".add-end button:has-text('+ Heading')")
        # (just before, a paragraph above was being edited: folding it must not make this press miss)
        focused = await page.evaluate("document.activeElement.tagName + ':' + (document.activeElement.value || '')")
        check("14 + Heading lands even while a paragraph above is open", focused == "TEXTAREA:## ", focused)
        await page.keyboard.type("A New Section")
        await wait_saved(page)
        check("14 + Heading adds a heading block", "\n## A New Section\n" in read(), repr(read()[-400:]))

        # 15 + Box
        await page.click(".add-end button:has-text('+ Box')")
        titles = await page.evaluate("[...document.querySelectorAll('#modal .box-list button')].map(b => b.textContent)")
        check("15 + Box lists the design's boxes by title", "Concept" in titles and "Previously" in titles, str(titles))
        await page.click("#modal .box-list button:has-text('Concept')")
        sel = await page.evaluate("""() => { const t = document.activeElement;
            return t && t.tagName === 'TEXTAREA' ? t.value.slice(t.selectionStart, t.selectionEnd) : null; }""")
        check("15 the box goes in with its line selected", sel == "Write here.", str(sel))
        await page.wait_for_function("() => [...document.querySelectorAll('.blk.component .problem')].some(p => p.textContent.includes('still says'))", timeout=8000)
        check("15 until its line is replaced, the box says so", True)
        await page.keyboard.type("A plain definition.")
        await wait_saved(page)
        check("15 the box is saved with a label", re.search(r'::: \{\.concept label="Concept"\}\nA plain definition\.\n:::', read()) is not None)

        # 12 moving
        before = order()
        await page.hover(".blk[data-id=b-one]")
        await page.click(".blk[data-id=b-one] .tools button[aria-label='Move down']")
        await wait_saved(page)
        after = order()
        check("12 Move down swaps a block with the next", after.index("b-one") == before.index("b-one") + 1, f"{before[:4]} -> {after[:4]}")
        up_off = await page.evaluate("document.querySelector('.blk[data-id=b-two] .tools button[aria-label=\"Move up\"]').disabled")
        check("12 the first block cannot move up", up_off)

        # 10 the writer's note, and a prompt moving with its draft
        await page.click("button.cta")
        await page.wait_for_selector("#flow .dblk.draft", timeout=30000)
        await page.wait_for_function("() => [...document.querySelectorAll('.dblk .wnote')].length > 0", timeout=15000)
        note = await page.inner_text(".dblk.draft .wnote")
        check("10 Draft shows the writer's note on the draft", note == "The writer says: I had no page to link for the voice file.", note)
        await page.click("button[data-screen=compose]")
        await page.wait_for_selector(".blk.prompt .wnote")
        check("10 Plan shows it under the prompt", "no page to link" in await page.inner_text(".blk.prompt .wnote"))
        draft_id = re.search(r"\.draft #(b-[\w-]+) for=b-pr1", read()).group(1)
        before = order()
        await page.click(".blk.prompt[data-id=b-pr1] button[aria-label='Move up']")
        await wait_saved(page)
        after = order()
        pi = after.index("b-pr1")
        check("12 a prompt moves together with its draft", after[pi + 1] == draft_id and pi == before.index("b-pr1") - 1,
              f"{before} -> {after}")

        # K rewrites: rapid fire on Draft, a batch from Plan
        def runs():
            """Writing calls: run folders whose reply is not the research planner's (=== SEARCH)."""
            n = 0
            for x in os.listdir(os.path.join(d, ".runs")):
                reply = os.path.join(d, ".runs", x, "REPLY.md")
                if x.startswith("run-") and os.path.exists(reply) and \
                        not open(reply, encoding="utf-8").read().lstrip().startswith("=== SEARCH"):
                    n += 1
            return n
        busy = "() => !!document.querySelector('.steps button .dot')"
        d1 = re.search(r"\.draft #(b-[\w-]+) for=b-pr1", read()).group(1)
        d2 = re.search(r"\.draft #(b-[\w-]+) for=b-pr2", read()).group(1)
        await page.click("button[data-screen=build]")
        await page.wait_for_selector(f"#flow .dblk.draft[data-id={d2}]")
        n0 = runs()
        await page.click(f"#flow .dblk.draft[data-id={d1}] button:has-text('Rewrite')")
        await page.fill("#rw-text", "Make it shorter")
        await page.press("#rw-text", "Enter")
        await page.wait_for_function(busy, timeout=8000)
        await page.click(f"#flow .dblk.draft[data-id={d2}] button:has-text('Rewrite')")
        await page.fill("#rw-text", "Mention rain")
        await page.press("#rw-text", "Enter")
        queued = await page.inner_text(f"#flow .dblk.draft[data-id={d2}] .rwnote")
        check("K a second rewrite sent while the first runs is next up", queued.startswith("Next up") and "Mention rain" in queued, queued)
        check("K the bar says it goes when this pass finishes", "when this pass finishes" in await page.inner_text("#rwbar"))
        await page.wait_for_function("() => [...document.querySelectorAll('.dblk .wnote')].some(n => n.textContent.includes('Mention rain'))", timeout=40000)
        await page.wait_for_function(f"() => !({busy})()", timeout=20000)
        notes = await page.evaluate("[...document.querySelectorAll('.dblk.draft')].map(n => (n.querySelector('.wnote') || {}).textContent || '')")
        check("K each direction reached its own section", any("got the direction: Make it shorter" in x for x in notes)
              and any("got the direction: Mention rain" in x for x in notes), str(notes))
        check("K two sends, two passes, the second started by itself", runs() == n0 + 2, f"{n0} -> {runs()}")

        # K on Draft: two directions added to the list, then one pass for both
        await page.click(f"#flow .dblk.draft[data-id={d1}] button:has-text('Rewrite')")
        await page.fill("#rw-text", "Open with the weather")
        await page.click(".rwbox button:has-text('Add to list')")
        await page.click(f"#flow .dblk.draft[data-id={d2}] button:has-text('Rewrite')")
        await page.fill("#rw-text", "End on the boats")
        await page.click(".rwbox button:has-text('Add to list')")
        bar = await page.inner_text("#rwbar")
        check("K Add to list lines them up without sending", "2 sections to rewrite" in bar and not await page.evaluate(busy), bar)
        n2 = runs()
        await page.click("#rwbar button:has-text('Rewrite them now')")
        await page.wait_for_function(busy, timeout=8000)
        await page.wait_for_function(f"() => !({busy})()", timeout=30000)
        try:
            await page.wait_for_function("() => [...document.querySelectorAll('.dblk .wnote')].some(n => n.textContent.includes('End on the boats'))", timeout=10000)
        except Exception:
            pass
        notes = await page.evaluate("[...document.querySelectorAll('.dblk.draft')].map(n => (n.querySelector('.wnote') || {}).textContent || '')")
        check("K the listed directions went in ONE pass", runs() == n2 + 1 and any("Open with the weather" in x for x in notes)
              and any("End on the boats" in x for x in notes), f"{n2} -> {runs()} {notes}")

        await page.click("button[data-screen=compose]")
        await page.wait_for_selector(".blk.prompt[data-id=b-pr2]")
        await page.click(".blk.prompt[data-id=b-pr1] .acts button:has-text('Rewrite')")
        await page.click(".blk.prompt[data-id=b-pr2] .acts button:has-text('Rewrite')")
        label = await page.inner_text("button.cta")
        check("K Plan's main button runs the list", label.startswith("Rewrite 2 drafts"), label)
        n1 = runs()
        await page.click("button.cta")
        await page.wait_for_function(busy, timeout=8000)
        await page.wait_for_function(f"() => !({busy})()", timeout=30000)
        check("K a Plan batch is one pass", runs() == n1 + 1, f"{n1} -> {runs()}")
        await page.click("button[data-screen=compose]")
        await page.wait_for_selector(".cta-row .rw-all")
        check("K the list is empty after it ran", await page.evaluate("!document.querySelector('.rwnote')")
              and (await page.inner_text("button.cta")).startswith("Read the draft"))
        await page.click(".cta-row .rw-all")
        check("K rewrite all drafts puts every drafted prompt on the list", (await page.inner_text("button.cta")).startswith("Rewrite 2 drafts"))
        await page.click(".cta-row .rw-all")
        check("K and can take them all off again", (await page.inner_text("button.cta")).startswith("Read the draft"))
        # K a discarded draft drops off the list (its prompt is not written again)
        await page.click(".cta-row .rw-all")
        await page.click("button[data-screen=build]")
        await page.wait_for_selector(f"#flow .dblk.draft[data-id={d2}]")
        await page.click(f"#flow .dblk.draft[data-id={d2}] button:has-text('Discard')")
        await wait_saved(page)
        bar = await page.inner_text("#rwbar")
        check("K a discarded draft drops off the rewrite list", bar.startswith("1 section"), bar)
        await page.click("button[data-screen=compose]")
        await page.wait_for_selector(".blk.prompt[data-id=b-pr2]")
        label = await page.inner_text("button.cta")
        check("K Plan writes the new and rewrites the listed in one button", label.startswith("Write 1 · rewrite 1"), label)
        await page.click("button.cta")          # write b-pr2 again (and rewrite b-pr1), one pass
        await page.wait_for_function(busy, timeout=8000)
        await page.wait_for_function(f"() => !({busy})()", timeout=30000)
        d2 = re.search(r"\.draft #(b-[\w-]+) for=b-pr2", read()).group(1)
        await page.click("button[data-screen=compose]")
        await page.wait_for_selector(".blk.prompt[data-id=b-pr2]")
        try:
            await page.wait_for_function("() => !document.querySelector('button.cta').textContent.startsWith('Write')", timeout=10000)
        except Exception:
            pass
        label = await page.inner_text("button.cta")
        check("K a discarded draft stays off the list once written again", label.startswith("Read the draft")
              and await page.evaluate("!document.querySelector('.rwnote')"), label)

        # 12 moving on Draft: a section moves with its hidden prompt
        await page.click("button[data-screen=build]")
        await page.wait_for_selector(f"#flow .dblk.draft[data-id={d2}]")
        before = order()
        await page.click(f"#flow .dblk.draft[data-id={d2}] button[aria-label='Move up']")
        await wait_saved(page)
        after = order()
        pi = after.index("b-pr2")
        check("12 Draft: Move up moves a section with its prompt", after[pi + 1] == d2 and pi < before.index("b-pr2"),
              f"{before} -> {after}")
        seen = await page.evaluate("[...document.querySelectorAll('#flow > [data-id]')].map(n => n.dataset.id)")
        check("12 Draft: the page shows the new order", seen.index(d2) < seen.index("b-three"), str(seen))
        await page.click("#flow .dblk.prose[data-id=b-three] button[aria-label='Move down']")
        await wait_saved(page)
        check("12 Draft: your own words move too", order().index("b-three") > after.index("b-three"))
        await page.click("button[data-screen=compose]")
        await page.wait_for_selector(".blk[data-id=b-three]")

        # 11 deleting your own words asks twice
        await page.hover(".blk[data-id=b-three]")
        btn = ".blk[data-id=b-three] .tools button:has-text('Delete')"
        await page.click(btn)
        check("11 Delete asks Sure? first", await page.is_visible(".blk[data-id=b-three] .tools button:has-text('Sure?')"))
        await page.click(".blk[data-id=b-three] .tools button:has-text('Sure?')")
        await wait_saved(page)
        check("11 then the block is gone", "b-three" not in read())

        # 17 Download warns about empty details
        await page.click("button[data-screen=review]")
        await page.wait_for_selector(".details-empty", timeout=8000)
        warn = await page.inner_text(".details-empty")
        check("17 Download lists empty issue details", "subject" in warn and "preview" not in warn, warn)

        # SPEC-HEADINGS: a design that asks for headings gets them on the way to Proof
        hd = os.path.join(ws, "issues", "072-headed")
        hread = lambda: open(os.path.join(hd, "issue.md"), encoding="utf-8").read()  # noqa: E731
        hruns = lambda: len([x for x in os.listdir(os.path.join(hd, ".runs"))]) if os.path.isdir(os.path.join(hd, ".runs")) else 0  # noqa: E731
        await page.select_option("#issue-select", "072-headed")
        await page.wait_for_function("() => document.querySelector('.doc-title').value === 'Headed issue'", timeout=8000)
        await page.click("button[data-screen=build]")
        await page.wait_for_selector("#flow .dblk.prose")
        await page.click("button.cta:has-text('Proof it')")
        await page.wait_for_function("() => document.querySelector('.doc-title') && document.querySelector('.doc-title').textContent.startsWith('Adding section headings')", timeout=8000)
        check("H going to Proof first adds the headings, on Draft", True)
        await page.wait_for_function("() => document.querySelector('.steps button[aria-selected=true]').dataset.screen === 'review'", timeout=30000)
        check("H then Proof opens by itself", True)
        text = hread()
        check("H the headings went in front of the sections", "## A heading from the fake writer 1\n\n{#h-two}" in text.replace("\r", "")
              or re.search(r"## A heading from the fake writer 1\n\n\{#h-two\}", text) is not None, text[-400:])
        check("H the cold open has none", text.index("{#h-one}") < text.index("## A heading"))
        n = hruns()
        await page.click("button[data-screen=build]")
        await page.wait_for_selector("#flow .dblk.prose")
        await page.click("button.cta:has-text('Proof it')")
        await page.wait_for_function("() => document.querySelector('.steps button[aria-selected=true]').dataset.screen === 'review'", timeout=8000)
        check("H going to Proof again asks nothing more", hruns() == n, f"{n} -> {hruns()}")
        # H while the model is busy: Proof opens, and the headings go in once it is free
        await page.click("button[data-screen=compose]")
        await page.wait_for_selector(".add-end")
        await page.click(".add-end button:has-text('+ Prose')")
        await page.fill(".blk.editing textarea", PARA + PARA)
        await page.click(".add-end button:has-text('+ Prompt')")
        await page.fill(".blk.editing textarea", "Write a closing line.")
        await wait_saved(page)
        await page.click("button.cta:has-text('Write the drafts')")
        await page.wait_for_function(busy, timeout=8000)
        await page.click("button[data-screen=review]")
        toast = await page.evaluate("[...document.querySelectorAll('.toast')].map(t => t.textContent).join(' | ')")
        on = await page.evaluate("document.querySelector('.steps button[aria-selected=true]').dataset.screen")
        check("H going to Proof while the model is busy stays on Draft and says why",
              on == "build" and "once the model is free" in toast, f"{on} {toast}")
        await page.wait_for_function("() => document.querySelector('.doc-title') && document.querySelector('.doc-title').textContent.startsWith('Adding section headings')", timeout=30000)
        await page.wait_for_function("() => document.querySelector('.steps button[aria-selected=true]').dataset.screen === 'review' && !document.querySelector('.steps button .dot')", timeout=30000)
        check("H then they went in, and Proof opened again", hread().count("## A heading from the fake writer") >= 3, hread()[-300:])
        await page.select_option("#issue-select", SLUG)
        await page.wait_for_function("() => document.querySelector('.doc-title').value === 'Basics test'", timeout=8000)

        # SPEC-FACTS: check the facts on Proof (a plain sentence to check first)
        await page.click("button[data-screen=compose]")
        await page.wait_for_selector(".add-end")
        await page.click(".add-end button:has-text('+ Prose')")
        await page.keyboard.type("The harbour opened in 1851 to ships.")
        await wait_saved(page)
        await page.click("button[data-screen=review]")
        await page.wait_for_selector(".panel.facts")
        check("F8 Download warns when the facts were never checked", await page.is_visible(".facts-stale"))
        await page.click(".panel.facts button:has-text('Check the facts')")
        await page.wait_for_selector(".panel.facts .fact-row.wrong:not(.link)", timeout=40000)
        quote = (await page.inner_text(".panel.facts .fact-row.wrong:not(.link) .fact-quote")).strip("“”")
        check("F6 a wrong claim is listed with its words and its source", quote == "The harbour opened in"
              and await page.is_visible(".panel.facts .fact-row.wrong:not(.link) a"), quote)
        check("F8 once checked, Download does not warn", not await page.is_visible(".facts-stale"))
        await page.click(".panel.facts .fact-row.wrong:not(.link) button:has-text('Use this fix')")
        await page.wait_for_function("() => [...document.querySelectorAll('.panel.facts button')].some(b => b.textContent.includes('Undo the fix'))", timeout=8000)
        check("F7 Use this fix corrects the words in the file", quote + " (checked)" in read())
        check("F7 and its own fix does not make the check out of date", not await page.is_visible(".facts-stale"))
        await page.click(".panel.facts button:has-text('Undo the fix')")
        await page.wait_for_function("() => [...document.querySelectorAll('.panel.facts button')].some(b => b.textContent.includes('Use this fix'))", timeout=8000)
        check("F7 Undo puts the words back", quote + " (checked)" not in read() and quote in read())

        # layout
        await page.click("button[data-screen=compose]")
        await page.wait_for_selector(".blk")
        await page.hover(".blk[data-id=b-two]")
        await page.screenshot(path=f"{out}/plan-1360.png", full_page=True)
        overlap = await page.evaluate("""() => {
            const t = document.querySelector('.blk[data-id=b-two] .tools').getBoundingClientRect();
            const side = document.querySelector('#side, aside, .side');
            if (!side) return false; const s = side.getBoundingClientRect();
            return t.right > s.left && t.left < s.right && t.bottom > s.top && t.top < s.bottom; }""")
        check("the block buttons do not sit on the side panel at 1360px", not overlap)
        for w in (320, 360, 390):
            await page.set_viewport_size({"width": w, "height": 800})
            await page.wait_for_timeout(200)
            wide = await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            check(f"no sideways scroll at {w}px", wide)
        await page.screenshot(path=f"{out}/plan-390.png", full_page=True)

        check("no page errors", not errors, str(errors[:3]))
        await b.close()


if __name__ == "__main__":
    if sys.argv[1] == "--seed":
        seed(sys.argv[2])
        sys.exit(0)
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
    print(f"{len(FAILS)} failed" if FAILS else "all passed")
    sys.exit(1 if FAILS else 0)
