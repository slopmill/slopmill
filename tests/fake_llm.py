#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A stand-in writer command with the argv shape (-s SYSTEM PROMPT FILES...), for driving
the UI without spending real model calls. FAKE_LLM_DELAY seconds before it answers."""
import os
import re
import sys
import time

args = sys.argv[1:]
system = args[args.index("-s") + 1]
prompt = args[args.index("-s") + 2]
time.sleep(float(os.environ.get("FAKE_LLM_DELAY", "4")))

if system.startswith("You are proofreading"):
    # A proof call: fix a few known mistakes wherever DOCUMENT.md has them.
    document = open(args[-1], encoding="utf-8").read()
    known = [("at at the", "at the", "doubled word"), ("git hub", "GitHub", "name: GitHub"),
             ("Their going", "They're going", "their/they're"), ("recieve", "receive", "spelling"),
             ("claude code", "Claude Code", "name: Claude Code")]
    found = 0
    for m in re.finditer(r"^\[(?:PROSE|DRAFT|COMPONENT) #([\w-]+)[^\]]*\]\n(.*?)(?=\n\n\[|\Z)", document, re.S | re.M):
        for find, rep, why in known:
            if m.group(2).count(find) == 1:
                print(f"=== FIX #{m.group(1)} ===\nFIND: {find}\nREPLACE: {rep}\nWHY: {why}\n=== END ===")
                found += 1
    if not found:
        print("=== NONE ===")
    sys.exit(0)
if "You are answering the author in the chat" in system:
    print("Happy to help. I can't see anything you haven't written yet, but here's a prompt for it:")
    print("=== PROMPT ===\nChart: honey pots by day. Monday 4, Tuesday 3, Wednesday 1\n=== END ===")
    sys.exit(0)
head = prompt.split("\n\nGeneral direction")[0]
pics = set(re.findall(r"#([A-Za-z][\w-]*)", head.split("PICTURE prompts")[1])) if "PICTURE prompts" in head else set()
ids = list(dict.fromkeys(re.findall(r"#([A-Za-z][\w-]*)", head.split("\nThese are PICTURE")[0])))
for i in pics:
    print(f"=== IMAGE {i} ===")
    print("DESCRIPTION: a quiet backyard at night, birds on a branch under a porch light")
    print("ALT: Three small birds on a branch under a warm porch light at night")
    print("CAPTION: The birds were **also** confused.")
    print("=== END ===")
charts = set(re.findall(r"#([A-Za-z][\w-]*)", re.search(r"These are CHART prompts[^\n]*", head).group(0))) \
    if "These are CHART prompts" in head else set()
ids = [i for i in ids if i not in charts] if "These are CHART" not in head else \
    list(dict.fromkeys(re.findall(r"#([A-Za-z][\w-]*)", re.split(r"\nThese are (?:PICTURE|CHART)", head)[0])))
for i in charts:
    print(f"=== CHART {i} ===\nTYPE: column\nTITLE: Honey pots by day\nUNIT: pots\nDATA:\n"
          "Monday | 4\nTuesday | 3\nWednesday | 1\nSOURCE: The author's prompt\n"
          "ALT: Four pots on Monday, three on Tuesday, one on Wednesday.\nCAPTION: Going, going.\n=== END ===")
for i in ids:
    if i in pics or i in charts:
        continue
    print(f"=== BLOCK {i} ===")
    print("Last week I did a revision of my voice file and dove deeper into removing the "
          "**AI-isms**{.blue} that make the newsletter sound more like talking to Claude than "
          "sitting down with your nerdy friend.\n\nApologies that this week's below the fold is "
          "more of an idea than a fully baked product. Send me what you are building.")
    print("=== END ===")
print("=== NOTE ===\nI kept the apology to one line.\n=== END ===")
