# SPDX-License-Identifier: MIT
import asyncio, sys, json
from playwright.async_api import async_playwright
BASE = "http://127.0.0.1:8441"
OUT = sys.argv[1]
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1440, "height": 900}, color_scheme="light")
        page = await ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append("console:" + m.text) if m.type == "error" else None)
        await page.goto(BASE + "/?t=testtoken123#sandbox")
        await page.wait_for_selector(".blk")
        await page.screenshot(path=f"{OUT}/1-compose-light.png", full_page=True)
        # focus the prompt, check selbar state
        await page.click(".blk.prose .view")
        await page.wait_for_timeout(300)
        # select part of the first paragraph and make it a prompt
        await page.evaluate("(() => { const t = document.querySelector('.blk.editing textarea'); t.setSelectionRange(0, 38); t.dispatchEvent(new Event('select')); })()")
        await page.wait_for_timeout(200)
        await page.screenshot(path=f"{OUT}/1b-compose-editing.png")
        await page.click("#selbar button[data-kind=prompt]")
        await page.wait_for_timeout(800)
        await page.screenshot(path=f"{OUT}/1c-compose-split.png")
        n = await page.evaluate("document.querySelectorAll('.blk.prompt').length")
        print("prompts after split:", n)
        await page.click("#selbar button[data-kind=prose]")
        await page.wait_for_timeout(800)
        n = await page.evaluate("document.querySelectorAll('.blk.prompt').length")
        print("prompts after converting back:", n)
        # type a new paragraph at end via + Prose
        await page.click(".add-end button:has-text('+ Prose')")
        await page.keyboard.type("A closing line I typed myself.")
        await page.wait_for_timeout(1500)
        st = await page.evaluate("document.querySelector('#save-state').textContent")
        print("save state:", st)
        # generate
        await page.click("button.cta")
        await page.wait_for_selector(".filling", timeout=5000)
        await page.wait_for_timeout(2500)
        await page.screenshot(path=f"{OUT}/2-build-running.png", full_page=True)
        # SPEC-STEPS 4: a generate pass stays on Draft; Proof it → goes on
        await page.wait_for_selector("#flow .dblk.draft", timeout=30000)
        await page.click("button.cta")
        await page.wait_for_function("() => { const d = document.querySelector('#preview-frame')?.contentDocument; return d && d.querySelector('#issue-body [data-block]'); }", timeout=30000)
        await page.wait_for_timeout(1500)
        await page.screenshot(path=f"{OUT}/3-review.png", full_page=True)
        # select text inside the draft and comment
        box = await page.evaluate("""() => { const d = document.querySelector('#preview-frame').contentDocument;
          const blk = [...d.querySelectorAll('#issue-body p[data-kind=draft]')][0];
          const r = d.createRange(); const t = blk.firstChild;
          r.setStart(t, 0); r.setEnd(t, 40);
          const s = d.getSelection(); s.removeAllRanges(); s.addRange(r);
          const b = r.getBoundingClientRect(); return {x: b.left + 5, y: b.top + 5};
        }""")
        await page.mouse.move(box["x"], box["y"])
        await page.evaluate("document.querySelector('#preview-frame').contentDocument.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}))")
        await page.wait_for_selector("#pop-open:visible", timeout=3000)
        await page.click("#pop-open")
        await page.fill("#pop-note", "Cut this down. Say it once.")
        await page.click("#pop-form button[type=submit]")
        await page.wait_for_selector(".note .who", timeout=5000)
        await page.wait_for_timeout(500)
        await page.screenshot(path=f"{OUT}/4-review-comment.png", full_page=True)
        # dark theme review
        await page.click("[data-theme-set=dark]")
        await page.wait_for_timeout(300)
        await page.screenshot(path=f"{OUT}/5-review-dark.png", full_page=True)
        # send edits
        await page.click(".apply .cta")
        await page.wait_for_selector(".filling", timeout=5000)
        await page.screenshot(path=f"{OUT}/6-revise-running-dark.png", full_page=True)
        await page.wait_for_function("() => { const d = document.querySelector('#preview-frame')?.contentDocument; return d && d.querySelector('#issue-body [data-block]'); }", timeout=30000)
        await page.wait_for_timeout(1500)
        await page.screenshot(path=f"{OUT}/7-after-revise-dark.png", full_page=True)
        await page.click("button[data-screen=compose]")
        await page.wait_for_timeout(500)
        await page.screenshot(path=f"{OUT}/8-compose-dark-with-draft.png", full_page=True)
        # a long, real issue, when this copy has one (issue 022 is the author's own)
        state = await page.evaluate("fetch('/api/state').then(r => r.json())")
        if any(i["slug"] == "022-is-this-the-most-useful-ai-to-date" for i in state["issues"]):
            await page.goto(BASE + "/#022-is-this-the-most-useful-ai-to-date")
            await page.wait_for_function("() => document.querySelector('.doc-title') && document.querySelector('.doc-title').value.startsWith('Is This')")
            await page.wait_for_timeout(800)
            await page.screenshot(path=f"{OUT}/9-022-compose-dark.png")
            await page.click("button[data-screen=review]")
            await page.wait_for_function("() => { const d = document.querySelector('#preview-frame')?.contentDocument; return d && d.querySelector('#issue-body [data-block]'); }", timeout=15000)
            await page.wait_for_timeout(3000)
            await page.screenshot(path=f"{OUT}/10-022-review-dark.png")
            await page.evaluate("window.scrollTo(0, 2600)")
            await page.wait_for_timeout(1200)
            await page.screenshot(path=f"{OUT}/10b-022-review-scrolled.png")
        else:
            print("issue 022 is not in this workspace: skipped the long-issue screenshots")
        # mobile
        m = await b.new_context(viewport={"width": 390, "height": 844}, color_scheme="light", storage_state=await ctx.storage_state())
        mp = await m.new_page()
        mp.on("pageerror", lambda e: errors.append("mobile:" + str(e)))
        await mp.goto(BASE + "/#sandbox")
        await mp.wait_for_selector(".blk")
        await mp.evaluate("localStorage.setItem('cmp-theme','light')")
        await mp.reload(); await mp.wait_for_selector(".blk")
        await mp.screenshot(path=f"{OUT}/11-mobile-compose.png", full_page=True)
        ow = await mp.evaluate("document.documentElement.scrollWidth")
        print("mobile scrollWidth:", ow)
        print("errors:", json.dumps(errors, indent=1))
        await b.close()
asyncio.run(main())
