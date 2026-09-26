#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A stand-in search command for the browser suites (`[research] provider = "command"`):
one result on an address that never resolves, so slopmill keeps its snippet and nothing
on the internet is reached."""
import json
import sys

query = " ".join(sys.argv[1:])
print(json.dumps([{"title": "A page about it", "url": "https://source.invalid/page",
                   "snippet": f"What the page says about {query}: it is not quite so."}]))
