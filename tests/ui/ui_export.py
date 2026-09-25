# SPDX-License-Identifier: MIT
"""SPEC-EXPORT in a real browser: the Download panel on Proof, a real download for each
choice, an unsaved edit saved before the file is made, and a refusal shown as a toast (not
saved as a file). Needs a server on :8441 (tests/ui/start_test_server.sh.example) and a
workspace seeded with --seed first. Prints PASS/FAIL per check, exits non-zero on a failure.

    python3 tests/ui/ui_export.py --seed WORKSPACE     # before starting the server
    python3 tests/ui/ui_export.py OUT_DIR TOKEN WORKSPACE
"""
import asyncio
import io
import os
import sys
import zipfile

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8441"
SLUG = "070-export"
TODO = "071-export-todo"
FAILS = []

FRONT = """---
number: {n}
slug: {slug}
title: Export test
subject: Export test
preview_text: A test
og_image: https://example.com/x.jpg
issue_date: 2026-09-24
---

"""
ISSUE = FRONT.format(n=70, slug=SLUG) + """{#b-open}
The opening paragraph, in my own words.

{#b-last}
The closing line, also mine.
"""
UNWRITTEN = FRONT.format(n=71, slug=TODO) + """{#b-open}
Words first.

::: {.prompt #b-pr1}
Say something about the weather.
:::
"""


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        FAILS.append(name)


def seed(ws):
    for slug, text in ((SLUG, ISSUE), (TODO, UNWRITTEN)):
        d = os.path.join(ws, "issues", slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "issue.md"), "w", encoding="utf-8") as f:
            f.write(text)


async def main(out, token, ws):
    os.makedirs(out, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for width in (1360, 390):
            ctx = await b.new_context(viewport={"width": width, "height": 900}, accept_downloads=True)
            page = await ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(f"{BASE}/?t={token}#{SLUG}")
            await page.wait_for_selector(".blk")

            # an edit on Plan, then straight to Proof before the save timer runs
            await page.click(".blk.prose .view")
            await page.fill(".blk.editing textarea", f"The opening paragraph, edited at {width}.")
            await page.click("button[data-screen=review]")
            await page.wait_for_selector("[data-download=html]")
            labels = await page.eval_on_selector_all("[data-download]", "bs => bs.map(b => b.textContent)")
            check(f"{width}: 1 the panel offers three downloads", labels == ["Web page", "Word", "All files"], labels)

            got = {}
            for kind in ("html", "docx", "zip"):
                async with page.expect_download(timeout=30000) as dl:
                    await page.click(f"[data-download={kind}]")
                d = await dl.value
                path = os.path.join(out, f"{width}-{d.suggested_filename}")
                await d.save_as(path)
                got[kind] = (d.suggested_filename, open(path, "rb").read())
            check(f"{width}: 1 file names come from the slug",
                  [g[0] for g in got.values()] == [f"{SLUG}.html", f"{SLUG}.docx", f"{SLUG}.zip"],
                  [g[0] for g in got.values()])
            html = got["html"][1].decode()
            check(f"{width}: 1 the edit made just before is in the download", f"edited at {width}" in html)
            check(f"{width}: 2 the web page has the words", "The closing line, also mine." in html
                  and "data-block" not in html)
            check(f"{width}: 5 the Word file is a document", got["docx"][1][:2] == b"PK"
                  and "word/document.xml" in zipfile.ZipFile(io.BytesIO(got["docx"][1])).namelist())
            names = zipfile.ZipFile(io.BytesIO(got["zip"][1])).namelist()
            check(f"{width}: 6 the zip holds the four files", sorted(names) == sorted(
                f"{SLUG}/{SLUG}{x}" for x in (".html", "-email.html", ".md", ".docx")), names)
            idle = await page.eval_on_selector_all("[data-download]", "bs => bs.map(b => b.disabled)")
            check(f"{width}: the buttons are usable again", idle == [False, False, False], idle)

            # 8, 12: a refusal is a toast, never a saved file
            await page.goto(f"{BASE}/#{TODO}")
            await page.wait_for_selector(".blk")
            await page.click("button[data-screen=review]")
            await page.wait_for_selector("[data-download=html]")
            downloads = []
            page.on("download", lambda d: downloads.append(d))
            await page.click("[data-download=html]")
            toast = page.locator(".toast.bad")
            await toast.wait_for(timeout=10000)
            msg = await toast.text_content()
            check(f"{width}: 8/12 unfinished work: the toast says what to do",
                  msg == "Not downloaded. 1 prompt is not written yet: write it on Draft, or delete it.", msg)
            await page.wait_for_timeout(800)
            check(f"{width}: 12 nothing was saved as a file", downloads == [])
            over = await page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
            check(f"{width}: no sideways scroll", not over)
            await page.screenshot(path=os.path.join(out, f"proof-{width}.png"), full_page=False)
            check(f"{width}: no page errors", not errors, errors)
            await ctx.close()
        await b.close()
    print("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS")
    return 1 if FAILS else 0


if __name__ == "__main__":
    if sys.argv[1] == "--seed":
        seed(sys.argv[2])
        sys.exit(0)
    sys.exit(asyncio.run(main(*sys.argv[1:4])))
