#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A stand-in for an OpenAI-compatible API on localhost, for trying the API-key route with
no real key: `python3 tests/fake_openai_server.py PORT`. Answers /v1/chat/completions the
way the writer protocol expects and /v1/images/generations with a tiny JPEG. It checks
that a Bearer key was sent and never prints it."""
import base64
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        if not self.headers.get("authorization", "").startswith("Bearer "):
            return self._send(401, {"error": {"message": "no key"}})
        if self.path.endswith("/images/generations"):
            return self._send(200, {"data": [{"b64_json": base64.b64encode(JPEG).decode()}]})
        if not self.path.endswith("/chat/completions"):
            return self._send(404, {"error": {"message": "not here"}})
        prompt = body["messages"][1]["content"]
        head = prompt.split("=== ATTACHED FILE")[0].split("\n\nGeneral direction")[0]
        if body["messages"][0]["content"].startswith("You are proofreading"):
            text = "=== NONE ==="
        else:
            pics = set(re.findall(r"#([A-Za-z][\w-]*)", head.split("PICTURE prompts")[1])) if "PICTURE prompts" in head else set()
            ids = list(dict.fromkeys(re.findall(r"#([A-Za-z][\w-]*)", head.split("\nThese are PICTURE")[0])))
            out = []
            for i in ids:
                if i in pics:
                    out.append(f"=== IMAGE {i} ===\nDESCRIPTION: a desk by a window in the morning\n"
                               f"ALT: A wooden desk by a window\nCAPTION: Where the drafts get written.\n=== END ===")
                else:
                    out.append(f"=== BLOCK {i} ===\nA paragraph the stand-in model wrote for {i}, "
                               f"so the page has something to show.\n=== END ===")
            out.append("=== NOTE ===\nWritten by the stand-in model.\n=== END ===")
            text = "\n".join(out)
        self._send(200, {"model": body.get("model"), "choices": [{"message": {"role": "assistant", "content": text}}],
                         "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(text) // 4}})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
