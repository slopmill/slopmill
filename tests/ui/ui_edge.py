# SPDX-License-Identifier: MIT
import asyncio, json, sys, httpx
from playwright.async_api import async_playwright
BASE = "http://127.0.0.1:8441"
ISSUE = sys.argv[1]  # path to sandbox issue.md on disk
results = {}
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1300, "height": 900})
        page = await ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        await page.goto(BASE + "/?t=testtoken123#sandbox")
        await page.wait_for_selector(".blk")

        # 1. typing during an in-flight save is not lost
        async def slow(route):
            await asyncio.sleep(1.5)
            await route.continue_()
        await page.route("**/doc", slow)
        await page.click(".blk.prose .view")
        await page.keyboard.press("End")
        await page.keyboard.type(" FIRST")
        await page.wait_for_timeout(900)          # autosave fires, PUT is held for 1.5s
        await page.keyboard.type(" SECOND")        # typed while the save is in flight
        await page.wait_for_timeout(4500)
        await page.unroute("**/doc")
        text = open(ISSUE).read()
        results["1 in-flight typing kept"] = ("FIRST SECOND" in text)

        # 2. another computer saves first: our version is kept and restorable
        await page.keyboard.type(" LOCALONLY")
        async with httpx.AsyncClient(cookies={"slopmill_token": "testtoken123"}) as c:
            st = (await c.get(BASE + "/api/issues/sandbox")).json()
            st["blocks"][-1]["text"] = st["blocks"][-1]["text"] + " REMOTE"
            await c.put(BASE + "/api/issues/sandbox/doc", headers={"x-slopmill": "1"},
                        json={"base": st["rev"], "meta": st["meta"], "blocks": st["blocks"]})
        await page.wait_for_timeout(2500)
        banner = await page.evaluate("document.querySelector('#banner').innerText")
        results["2 conflict banner shown"] = "changed on another computer" in banner
        text = open(ISSUE).read()
        results["2 remote save not clobbered"] = "REMOTE" in text and "LOCALONLY" not in text
        await page.click("#banner button:has-text('Restore my version')", timeout=4000)
        await page.wait_for_timeout(2500)
        text = open(ISSUE).read()
        results["2 restore writes local version"] = "LOCALONLY" in text

        # 3. refused save blocks leaving the issue, text kept
        await page.click(".add-end button:has-text('+ Prose')")
        await page.keyboard.type("::: {.closing}")
        await page.keyboard.press("Enter")
        await page.keyboard.type("x")
        await page.keyboard.press("Enter")
        await page.keyboard.type(":::")
        await page.wait_for_timeout(2000)
        results["3 refused save shows banner"] = "Not saved" in await page.evaluate("document.querySelector('#banner').innerText")
        await page.select_option("#issue-select", "050-proof")     # any other issue (seeded by ui_proof.py --seed)
        await page.wait_for_timeout(1500)
        title = await page.evaluate("document.querySelector('.doc-title').value")
        results["3 stayed on the issue"] = title == "Sandbox"
        still = await page.evaluate("[...document.querySelectorAll('.blk textarea')].some(t => t.value.includes('::: {.closing}'))")
        results["3 refused text still in editor"] = still
        # remove the bad block so the rest can continue
        await page.evaluate("""() => { const t=[...document.querySelectorAll('.blk textarea')].find(t=>t.value.includes('::: {.closing}')); t.value=''; t.dispatchEvent(new Event('input')); }""")
        await page.wait_for_timeout(1500)

        # 4. crash buffer: type and close before autosave
        await page.click(".blk.prose .view")
        await page.keyboard.press("End")
        await page.keyboard.type(" CRASHTEXT")
        await page.wait_for_timeout(100)
        await page.close()
        page = await ctx.new_page()
        page.on("pageerror", lambda e: errs.append(str(e)))
        await page.goto(BASE + "/#sandbox")
        await page.wait_for_selector(".blk")
        await page.wait_for_timeout(800)
        banner = await page.evaluate("document.querySelector('#banner').innerText")
        results["4 crash buffer offered"] = "unsaved changes" in banner
        await page.click("#banner button:has-text('Restore my version')", timeout=4000)
        await page.wait_for_timeout(2000)
        results["4 crash text restored"] = "CRASHTEXT" in open(ISSUE).read()

        # 5. a selection across two blocks does not open a comment
        await page.click("button[data-screen=review]")
        await page.wait_for_function("() => { const d = document.querySelector('#preview-frame')?.contentDocument; return d && d.querySelector('#issue-body [data-block]'); }")
        await page.wait_for_timeout(800)
        await page.evaluate("""() => { const d = document.querySelector('#preview-frame').contentDocument;
          const ps=[...d.querySelectorAll('#issue-body p[data-block]')];
          const r=d.createRange(); r.setStart(ps[0].firstChild, 3); r.setEnd(ps[1].firstChild, 5);
          const s=d.getSelection(); s.removeAllRanges(); s.addRange(r);
        }""")
        await page.evaluate("document.querySelector('#preview-frame').contentDocument.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}))")
        await page.wait_for_timeout(400)
        results["5 cross-block selection refused"] = await page.evaluate("document.querySelector('#comment-pop').hidden")
        # 6. a repeated phrase: the selected occurrence is the one highlighted
        await page.click("button[data-screen=compose]")
        await page.wait_for_selector(".blk")
        await page.click(".add-end button:has-text('+ Prose')")
        await page.keyboard.type("Keep this opening. Later, keep this ending.")
        await page.wait_for_timeout(1500)
        await page.click("button[data-screen=review]")
        await page.wait_for_timeout(2500)
        await page.evaluate("""() => { const d = document.querySelector('#preview-frame').contentDocument;
          const p=[...d.querySelectorAll('#issue-body p[data-block]')].find(p=>p.textContent.includes('Later, keep this'));
          const t=p.firstChild; const i=t.textContent.indexOf('keep this ending');
          const r=d.createRange(); r.setStart(t,i); r.setEnd(t,i+9);
          const s=d.getSelection(); s.removeAllRanges(); s.addRange(r);
        }""")
        await page.evaluate("document.querySelector('#preview-frame').contentDocument.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}))")
        await page.click("#pop-open", timeout=3000)
        await page.fill("#pop-note", "only the second one")
        await page.click("#pop-form button[type=submit]")
        await page.wait_for_timeout(1200)
        marked = await page.evaluate("""() => { const d = document.querySelector('#preview-frame').contentDocument; const m=[...d.querySelectorAll('mark.hl')].pop(); return m ? m.nextSibling.textContent.slice(0,8) : null; }""")
        results["6 second occurrence highlighted"] = marked == " ending."

        # 7. a chat message is kept when it cannot be sent
        await page.click("button[data-screen=compose]")
        await page.wait_for_selector(".blk")
        await page.click(".add-end button:has-text('+ Prose')")
        await page.keyboard.type("::: {.closing}")
        await page.keyboard.press("Enter"); await page.keyboard.type("x"); await page.keyboard.press("Enter"); await page.keyboard.type(":::")
        await page.wait_for_timeout(1500)
        await page.fill("#chat-input", "Make the ending shorter")
        await page.click("#chat-send")
        await page.wait_for_timeout(1500)
        results["7 unsent chat kept"] = await page.evaluate("document.querySelector('#chat-input').value") == "Make the ending shorter"
        results["page errors"] = errs
        print(json.dumps(results, indent=1))
        await b.close()
try:
    asyncio.run(main())
except Exception as e:
    print('STOPPED:', type(e).__name__)
    print(json.dumps(results, indent=1))
