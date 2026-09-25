#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A stand-in for `openclaw status --usage --json`."""
import json
import time
time.sleep(1)
now = time.time()
print(json.dumps({"usage": {"providers": [{"provider": "openai", "displayName": "OpenAI", "plan": "team",
      "windows": [{"label": "5h", "usedPercent": 12, "resetAt": (now + 3 * 3600) * 1000},
                  {"label": "Week", "usedPercent": 83, "resetAt": (now + 2.5 * 86400) * 1000}]}]}}))
