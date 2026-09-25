# SPDX-License-Identifier: MIT
"""SPEC-CHAT-PICTURES in a real browser: Ask answers without leaving Plan, Add to Plan adds
the suggested prompt, + Chart writes a chart that shows on Draft, and + Your picture puts an
uploaded picture into Plan. Needs a server on :8441 started with tests/fake_llm.py
(FAKE_LLM_DELAY=2) and a workspace seeded with --seed first. Prints PASS/FAIL per check.

    python3 tests/ui/ui_chat_pictures.py --seed WORKSPACE     # before starting the server
    python3 tests/ui/ui_chat_pictures.py OUT_DIR TOKEN WORKSPACE
"""
import asyncio
import os
import struct
import sys
import zlib

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8441"
SLUG = "070-chat"
FAILS = []

ISSUE = """---
number: 70
slug: 070-chat
title: Chat test
subject: Chat test
preview_text: A test
og_image: https://example.com/x.jpg
issue_date: 2026-09-24
---

{#b-open}
The opening paragraph, in my own words.

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


def tiny_png(path):
    raw = b"".join(b"\x00" + b"\xc0\x60\x30" * 8 for _ in range(6))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 6, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(data)


async def main(out, token, ws):
    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(f"{BASE}/?t={token}#{SLUG}")
        await page.wait_for_selector(".doc-title")

        # Ask: an answer in the chat, still on Plan, nothing written
        before = open(os.path.join(ws, "issues", SLUG, "issue.md")).read()
        check("the second chat button says what it will do", (await page.text_content("#chat-do")).strip() == "Change the issue")
        await page.fill("#chat-input", "Can you suggest a chart for this?")
        await page.click("#chat-send")
        await page.wait_for_selector(".msg.thinking", timeout=3000)
        check("thinking shows while it answers", True)
        await page.wait_for_selector(".msg.model .sugg", timeout=20000)
        check("the answer arrived in the chat", "Happy to help" in (await page.text_content(".msg.model")))
        check("still on Plan", await page.get_attribute('[data-screen="compose"]', "aria-selected") == "true")
        check("the issue file is unchanged", open(os.path.join(ws, "issues", SLUG, "issue.md")).read() == before)
        check("the input was cleared", await page.input_value("#chat-input") == "")
        await page.screenshot(path=os.path.join(out, "chat-answer.png"))

        # Add to Plan
        await page.click(".msg.model .sugg button")
        await page.wait_for_selector('.blk.prompt.picture textarea', timeout=3000)
        vals = await page.eval_on_selector_all(".blk.prompt textarea", "n => n.map(x => x.value)")
        check("Add to Plan added the chart prompt", any(v.startswith("Chart: honey pots") for v in vals), str(vals))
        check("the button says it was added", "Added" in (await page.text_content(".msg.model .sugg button")))
        for _ in range(60):          # the page's policy forbids wait_for_function's eval
            if (await page.text_content("#save-state")).strip() == "Saved":
                break
            await asyncio.sleep(0.1)
        check("the added prompt saved", (await page.text_content("#save-state")).strip() == "Saved")

        # the chart gets written and drawn
        await page.click(".cta")
        await page.wait_for_selector('.dblk.draft img[src*="chart-"]', timeout=30000)
        tag = await page.text_content(".dblk.draft .tag")
        check("the chart shows on Draft with its own label", "chart drawn from your prompt" in tag, tag)
        check("a drawn chart offers Use my own picture", "Use my own picture" in tag)
        await page.screenshot(path=os.path.join(out, "chart-draft.png"))

        # your own picture on Plan
        await page.click('[data-screen="compose"]')
        await page.wait_for_selector(".add-end")
        png = os.path.join(out, "mine.png")
        tiny_png(png)
        await page.click('.add-end button:has-text("+ Your picture")')
        await page.wait_for_selector("#pic-file")
        await page.click(".pic-form .send")
        check("no file: it says choose one", "Choose a picture" in (await page.text_content(".pic-form .edit-msg")))
        await page.set_input_files("#pic-file", png)
        await page.wait_for_selector(".pic-preview:not([hidden])", timeout=3000)
        check("the preview shows the chosen picture", True)
        await page.click(".pic-form .send")
        check("no alt text: it says why it matters", "read out" in (await page.text_content(".pic-form .edit-msg")))
        await page.fill("#pic-alt", "A small orange square")
        await page.fill("#pic-cap", "My own *picture*")
        await page.click(".pic-form .send")
        await page.wait_for_selector("#modal[hidden]", state="attached", timeout=8000)
        await page.wait_for_selector('.blk.component img[src*="own-"]', timeout=8000)
        check("the uploaded picture is in Plan", True)
        text = open(os.path.join(ws, "issues", SLUG, "issue.md")).read()
        check("the caption was kept as words", "My own \\*picture\\*" in text)
        await page.screenshot(path=os.path.join(out, "own-picture.png"))

        # narrow screen: the chat buttons fit
        await page.set_viewport_size({"width": 390, "height": 844})
        w = await page.evaluate("document.documentElement.scrollWidth")
        check("no sideways scroll at 390px", w <= 390, str(w))
        check("no page errors", not errors, "; ".join(errors))
        await b.close()


if __name__ == "__main__":
    if sys.argv[1] == "--seed":
        seed(sys.argv[2])
        sys.exit(0)
    os.makedirs(sys.argv[1], exist_ok=True)
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
    print(f"{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    sys.exit(1 if FAILS else 0)
