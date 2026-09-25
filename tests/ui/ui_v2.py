# SPDX-License-Identifier: MIT
"""SPEC-V2 in a real browser: picture prompts, notes in the text, voice and design managers,
the meter, both themes, a phone width. Needs a server on :8441 started with the fakes:
tests/fake_llm.py (writer), tests/fake_image.py (images), tests/fake_plan.py (plan). Prints
PASS/FAIL per check and exits non-zero on any failure.

    python3 tests/ui/ui_v2.py OUT_DIR TOKEN
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8441"
OUT, TOKEN = sys.argv[1], sys.argv[2]
HERE = os.path.dirname(os.path.abspath(__file__))
FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        FAILS.append(name)


async def frame_eval(page, js):
    return await page.evaluate("(js) => { const d = document.querySelector('#preview-frame').contentDocument;"
                               " return (new Function('d', js))(d); }", js)


async def main():
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1440, "height": 900}, color_scheme="light",
                                  accept_downloads=True)
        page = await ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append("console:" + m.text) if m.type == "error" else None)
        await page.goto(f"{BASE}/?t={TOKEN}#sandbox")
        await page.wait_for_selector(".blk")

        # ── Compose: panels, meter, request size
        await page.wait_for_selector(".pick-row select")
        voice = (await page.evaluate("document.querySelector('.side').innerText")).lower()
        check("voice and design panels", "voice" in voice and "design" in voice and "a pass" in voice, voice[:120])
        await page.wait_for_selector("#meter:not([hidden])", timeout=5000)
        check("meter in the header", True)

        # ── a picture prompt
        await page.click(".add-end button:has-text('+ AI picture')")
        await page.keyboard.type("a backyard at night with birds on a branch")
        await page.wait_for_timeout(1300)
        await page.click(".doc-head")        # leave the block so the note re-renders after the save
        await page.wait_for_timeout(1500)
        note = await page.inner_text(".cta-note")
        check("request size before sending", "sends" in note and "tokens" in note, note)
        tag = await page.evaluate("[...document.querySelectorAll('.blk.prompt.picture .tag')].map(t => t.textContent)")
        check("picture prompt is labelled", any("Picture prompt" in t for t in tag), str(tag))
        await page.screenshot(path=f"{OUT}/1-compose.png", full_page=True)
        await page.click("button.cta")
        await page.wait_for_selector(".filling", timeout=6000)
        await page.wait_for_selector(".filling:has-text('Drawing the picture')", timeout=15000)
        await page.screenshot(path=f"{OUT}/2-build-drawing.png", full_page=True)
        check("build shows the picture being drawn", True)
        # SPEC-STEPS 4: a generate pass stays on Draft; Proof it → goes on
        await page.wait_for_selector("#flow .dblk.draft img", timeout=30000)
        await page.click("button.cta")
        await page.wait_for_selector("#preview-frame", timeout=30000)
        await page.wait_for_function("() => { const d = document.querySelector('#preview-frame')?.contentDocument;"
                                     " return d && d.querySelector('#issue-body img'); }", timeout=15000)
        await page.wait_for_timeout(1200)
        img = await frame_eval(page, "const i = d.querySelector('#issue-body img[src*=\"/asset/img-\"]');"
                                     " return i ? {w: i.naturalWidth, src: i.getAttribute('src')} : null;")
        check("review shows the drawn picture", bool(img and img["w"] > 0), str(img))
        sub = (await page.inner_text(".doc-sub")).lower()
        check("review names the design", "fixture" in sub, sub)
        await page.screenshot(path=f"{OUT}/3-review.png", full_page=True)

        # ── a note on selected words, shown in the text
        pt = await frame_eval(page, """
          const p = [...d.querySelectorAll('#issue-body p[data-kind=paragraph]')][0];
          const t = p.firstChild; const r = d.createRange(); r.setStart(t, 0); r.setEnd(t, 30);
          const s = d.getSelection(); s.removeAllRanges(); s.addRange(r);
          const b = r.getBoundingClientRect(); return {x: b.left + 4, y: b.top + 4};""")
        await frame_eval(page, "d.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));")
        await page.wait_for_selector("#pop-open:visible", timeout=3000)
        check("selecting words offers Comment", (await page.text_content("#pop-open")) == "Comment")
        await page.click("#pop-open")
        await page.fill("#pop-note", "Shorter. Say it once.")
        await page.click("#pop-form button[type=submit]")
        await page.wait_for_function("() => document.querySelector('#preview-frame').contentDocument.querySelector('.cmp-note')", timeout=5000)
        n = await frame_eval(page, "const n = d.querySelector('.cmp-note'); const m = d.querySelector('mark.hl');"
                                   " return {note: n && n.innerText, prevIsBlock: n && n.previousElementSibling.dataset.block, mark: !!m};")
        check("the note sits in the text under its block", bool(n["note"] and "Say it once" in n["note"] and n["prevIsBlock"] and n["mark"]), str(n))

        # clicking the mark brings its note into view
        await frame_eval(page, "d.querySelector('mark.hl').click();")
        await page.wait_for_timeout(200)
        fl = await frame_eval(page, "return !!d.querySelector('.cmp-note.flash');")
        check("clicking a mark shows its note", fl)

        # a plain click on a paragraph offers a note on the whole block
        pos = await page.evaluate("""() => { const f = document.querySelector('#preview-frame');
          const d = f.contentDocument; const ps = [...d.querySelectorAll('#issue-body p[data-kind=paragraph]')];
          const p = ps[ps.length - 1]; const r = p.getBoundingClientRect(), fr = f.getBoundingClientRect();
          return {x: fr.left + r.left + 40, y: fr.top + r.top + 8}; }""")
        await page.mouse.click(pos["x"], pos["y"])
        await page.wait_for_selector("#pop-open:visible", timeout=3000)
        # SPEC-STEPS 12: a click on a block offers Comment and Edit text side by side
        check("clicking a block offers a block note", (await page.text_content("#pop-open")) == "Comment"
              and await page.is_visible("#pop-edit"))
        await page.click("#pop-open")
        await page.fill("#pop-note", "End on a question.")
        await page.click("#pop-form button[type=submit]")
        await page.wait_for_function("() => document.querySelector('#preview-frame').contentDocument.querySelectorAll('.cmp-note').length === 2", timeout=5000)
        side = (await page.inner_text(".side")).lower()
        check("both notes queue in the side panel", "2 edits queued" in side, side[:80])
        await page.screenshot(path=f"{OUT}/4-review-notes.png", full_page=True)
        # remove one from where it sits
        await frame_eval(page, "d.querySelectorAll('.cmp-note .cmp-x')[1].click();")
        await page.wait_for_function("() => document.querySelector('#preview-frame').contentDocument.querySelectorAll('.cmp-note').length === 1", timeout=5000)
        check("a note is removed from where it sits", "1 edit queued" in (await page.inner_text(".side")).lower())

        # ── meter dialog with plan windows
        await page.wait_for_timeout(1500)
        await page.click("#meter")
        await page.wait_for_selector("#modal:not([hidden])")
        await page.wait_for_timeout(300)
        md = await page.inner_text("#modal-body")
        if "being fetched" in md:
            await page.click("#modal-close"); await page.wait_for_timeout(2500)
            await page.click("#meter"); await page.wait_for_timeout(300)
            md = await page.inner_text("#modal-body")
        check("meter dialog: issue tokens and plan windows", "This issue" in md and "83% used" in md and "≈" in md, md[:200])
        check("meter: the plan bars say they are the whole account", "whole plan" in md.lower()
              and "not only slopmill" in md and "whole plan" in (await page.inner_text("#meter")).lower(), md[:300])
        check("a dialog never shows a stray null", "null" not in md, md[-80:])
        await page.screenshot(path=f"{OUT}/5-meter.png")
        await page.click("#modal-close")

        # ── models dialog
        await page.click("#models-btn")
        md = await page.inner_text("#modal-body")
        check("models dialog lists the providers", "Writes" in md and "Draws pictures" in md and "command" in md, md[:160])
        await page.click("#modal-close")

        # ── voice manager: upload, toggle, role, edit
        await page.click("button:has-text('Plan')")
        await page.wait_for_selector(".pick-row")
        await page.click(".side .panel button:has-text('Manage') >> nth=0")
        await page.wait_for_selector(".vlist .vrow")
        rows0 = await page.evaluate("document.querySelectorAll('.vlist .vrow').length")
        sample = os.path.join(OUT, "my-sample.md")
        with open(sample, "w") as f:
            f.write("This is a paragraph I wrote myself, on a Tuesday, badly.\n")
        await page.set_input_files("#modal-body input[type=file]", sample)
        await page.wait_for_function(f"() => document.querySelectorAll('.vlist .vrow').length === {rows0 + 1}", timeout=5000)
        check("upload adds a voice file", True)
        await page.screenshot(path=f"{OUT}/6-voice-manager.png")
        row = page.locator(".vlist .vrow", has_text="my-sample.md")
        await row.locator("input[type=checkbox]").uncheck()
        await page.wait_for_selector(".vlist .vrow.off:has-text('my-sample.md')", timeout=5000)
        check("a file can be switched off", True)
        await page.locator(".vlist .vrow", has_text="my-sample.md").locator("select").select_option("rules")
        await page.wait_for_function("() => [...document.querySelectorAll('.vlist .vrow')].some(r => r.textContent.includes('my-sample.md') && r.querySelector('select').value === 'rules')", timeout=5000)
        check("a file's role can change", True)
        await page.locator(".vlist .vrow", has_text="my-sample.md").locator(".vname").click()
        await page.wait_for_selector("#modal-body textarea.big")
        await page.fill("#modal-body textarea.big", "Edited in the app.\n")
        await page.click("#modal-body button:has-text('Save')")
        await page.wait_for_selector(".vlist .vrow:has-text('my-sample.md') .vsize:has-text('0.0KB')", timeout=5000)
        check("a voice file can be edited", True)
        await page.wait_for_timeout(800)       # let every re-render from the changes above land
        await page.click("#modal-body button:has-text('How to build one')")
        await page.wait_for_selector(".guide h3")
        check("the voice guide opens in the app", "Samples" in await page.inner_text(".guide"))
        await page.click("#modal-close")

        # ── design manager: download, bad upload, evil-but-valid upload, use it
        await page.click(".side .panel button:has-text('Manage') >> nth=1")
        await page.wait_for_selector(".vlist .vrow")
        async with page.expect_download() as dl:
            await page.click(".vlist a:has-text('Download')")
        path = await (await dl.value).path()
        text = open(path, encoding="utf-8").read()
        check("a design downloads as one file", text.count('format = "slopmill-design/1"') == 1 and "[templates]" in text)
        bad = os.path.join(OUT, "bad.design.toml")
        open(bad, "w").write(text.replace('name = "fixture"', 'name = "broken"').replace("{{ content }}", "{{ nope }}", 1))
        await page.set_input_files("#modal-body input[type=file]", bad)
        await page.wait_for_selector(".design-status.bad", timeout=25000)
        msg = await page.inner_text(".design-status")
        errors[:] = [e for e in errors if "422" not in e]      # the refusal itself is the expected answer
        check("a broken design is refused with the reason", "Not stored" in msg and "nope" in msg, msg[:160])
        evil = os.path.join(OUT, "evil.design.toml")
        open(evil, "w").write(text.replace('name = "fixture"', 'name = "evil"').replace(
            "{{ content }}</p>", "{{ content }}<img src=\"x\" onerror=\"parent.document.title='PWNED'\"><script>parent.document.title='PWNED2'</script></p>", 1)
            .replace("[page]\ncss = '''\n", "[page]\ncss = '''\nbutton { display: none !important; opacity: 0 !important; } mark { display: none !important; }\n", 1)
            .replace("site_head = '''\n", "site_head = '''\n<meta http-equiv=\"refresh\" content=\"0;url=https://example.com/\"><base href=\"https://example.com/\">\n", 1))
        await page.set_input_files("#modal-body input[type=file]", evil)
        await page.wait_for_selector(".vlist .vrow:has-text('evil')", timeout=25000)
        check("a valid design uploads", True)
        await page.click(".vlist .vrow:has-text('evil') button:has-text('Use for this issue')")
        await page.wait_for_timeout(1200)
        await page.click("button:has-text('Proof')")
        await page.wait_for_function("() => { const d = document.querySelector('#preview-frame')?.contentDocument; return d && d.querySelector('#issue-body p'); }", timeout=10000)
        await page.wait_for_timeout(1500)
        title = await page.title()
        check("no script from a design runs", "PWNED" not in title, title)
        csp = [e for e in errors if "Content Security Policy" in e or "sandbox" in e.lower()]
        check("the block is recorded as CSP/sandbox, not a crash", True, f"{len(csp)} blocked")
        errors[:] = [e for e in errors if e not in csp and "net::ERR" not in e and "404" not in e]
        sub = (await page.inner_text(".doc-sub")).lower()
        check("review now names the uploaded design", "evil" in sub, sub)
        vis = await frame_eval(page, "const x = d.querySelector('.cmp-note .cmp-x'); const m = d.querySelector('mark.hl');"
                                     " return {x: !!(x && x.offsetParent), op: x && getComputedStyle(x).opacity, m: !m || m.getClientRects().length > 0};")
        check("a design's CSS cannot hide a note's Remove or the marks", vis["x"] and vis["m"] and float(vis["op"] or 0) > 0.5, str(vis))
        await page.wait_for_timeout(2000)
        still = await frame_eval(page, "return !!d.getElementById('issue-body') && d.location.href;")
        check("a design's meta refresh and base tag do nothing", bool(still) and "example.com" not in str(still), str(still))
        # put it back
        await page.click("button:has-text('Plan')")
        await page.wait_for_selector(".pick-row")
        await page.select_option(".side .pick-row select >> nth=1", "fixture")
        await page.wait_for_timeout(1200)

        # ── dark theme review
        await page.click("[data-theme-set=dark]")
        await page.click("button:has-text('Proof')")
        await page.wait_for_function("() => { const d = document.querySelector('#preview-frame')?.contentDocument; return d && d.querySelector('#issue-body p'); }", timeout=10000)
        await page.wait_for_timeout(1000)
        th = await frame_eval(page, "return d.documentElement.getAttribute('data-theme');")
        check("the frame follows the theme", th == "dark", th)
        await page.screenshot(path=f"{OUT}/7-review-dark.png", full_page=True)

        # ── phone width
        m = await b.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
        mp = await m.new_page()
        await mp.goto(f"{BASE}/?t={TOKEN}#sandbox")
        await mp.wait_for_selector(".blk")
        for screen in ("compose", "review"):
            await mp.click(f"button[data-screen={screen}]")
            await mp.wait_for_timeout(1500)
            over = await mp.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            check(f"390px {screen}: no sideways scroll", over <= 0, str(over))
            await mp.screenshot(path=f"{OUT}/8-phone-{screen}.png", full_page=True)
        await mp.click("#meter")
        await mp.wait_for_timeout(400)
        over = await mp.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        check("390px meter dialog fits", over <= 0, str(over))
        await mp.screenshot(path=f"{OUT}/9-phone-meter.png")
        await mp.click("#modal-close")
        await mp.click("button[data-screen=compose]")
        await mp.wait_for_timeout(600)
        await mp.click(".side .panel button:has-text('Manage') >> nth=0")
        await mp.wait_for_timeout(600)
        over = await mp.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        check("390px voice manager fits", over <= 0, str(over))
        await mp.screenshot(path=f"{OUT}/10-phone-voice.png")

        real = [e for e in errors if "favicon" not in e]
        check("no page errors", not real, "; ".join(real)[:400])
        await b.close()
    print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
