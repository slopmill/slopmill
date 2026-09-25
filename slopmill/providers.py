# SPDX-License-Identifier: AGPL-3.0-or-later
"""What actually runs a model. Three jobs, configured separately:

  writer  text in, text out: resolves prompts and revises against comments
  images  a description in, a picture file out
  plan    optional: a subscription's usage windows, for the meter

Each can be a local command (any program with the right argument shape) or an HTTP
API reached with the user's own key. Keys are read from environment variables that the
config file names. They are never written anywhere, never sent to the browser, and scrubbed
from every error message.

Config (WORKSPACE/slopmill.toml, or --config):

    [writer]
    provider = "command"                # command | openai | anthropic
    command = "my-writer --model gpt-6-sol"
    label = "GPT-6 Sol"

    [images]
    provider = "command"                # command | openai | none
    command = "openclaw infer image generate --prompt {prompt} --output {output} --json"

    [plan]
    command = "openclaw status --usage --json"
    provider = "openai"

See docs/PROVIDERS.md.
"""
import base64
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import tomllib

COMMAND_MAX_REQUEST = 115_000    # the prompt travels as ONE argv entry; Linux caps one at 128KB
HTTP_MAX_REQUEST = 400_000       # about 100k tokens; raise it in the config if your model takes more
OPENAI_BASE = "https://api.openai.com/v1"
IMAGE_TYPES = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png"}


class LLMError(Exception):
    pass


class Cancelled(Exception):
    pass


class ConfigError(Exception):
    pass


class Reply(str):
    """The model's text, carrying what the call cost. usage: {"in", "out", "exact"}."""

    def __new__(cls, text, usage=None):
        s = super().__new__(cls, text)
        s.usage = usage
        return s


def estimate_tokens(nchars):
    """About four characters a token: the usual rule of thumb for English text."""
    return max(0, round(nchars / 4))


def usage_of(reply, sent_chars):
    """Tokens for a reply from any writer: exact when the provider said, else estimated
    from the characters sent (system, prompt and attachments) and received."""
    u = getattr(reply, "usage", None)
    if u and u.get("exact"):
        return {"in": int(u.get("in") or 0), "out": int(u.get("out") or 0), "exact": True}
    return {"in": estimate_tokens(sent_chars), "out": estimate_tokens(len(str(reply))),
            "exact": False}


def _check_size(limit, system, prompt, files):
    """What one call sends, framing around each attached file included (FRAMING bytes is
    more than the header _attachments writes)."""
    size = len(system.encode()) + len(prompt.encode()) + sum(os.path.getsize(f) + 64 for f in files)
    if size > limit:
        raise LLMError(f"the request is {size // 1024}KB; the model call is capped at "
                       f"{limit // 1024}KB. Shorten the issue or the voice pack.")


def _scrub(text, key=None):
    text = str(text)
    if key:
        text = text.replace(key, "[key]")
    return re.sub(r"\b(sk-[A-Za-z0-9_-]{3})[A-Za-z0-9_-]{6,}", r"\1…", text)[:600]


def _key(env_name):
    val = os.environ.get(env_name or "", "")
    if not val:
        raise LLMError(f"the API key variable {env_name} is not set on the server")
    return val


def _cancellable(fn, cancel, timeout, what, on_stop=None):
    """Run fn(stop) in a thread. Stop (cancel) and the timeout return at once: `stop` is
    set and on_stop() closes the connection the call is waiting on."""
    stop = threading.Event()

    def halt():
        stop.set()
        if on_stop:
            try:
                on_stop()
            except Exception:
                pass
    box = {}

    def run():
        try:
            box["ok"] = fn(stop)
        except BaseException as e:      # handed to the caller below
            box["err"] = e

    t = threading.Thread(target=run, daemon=True, name=f"call-{what}")
    t.start()
    start = time.monotonic()
    while t.is_alive():
        t.join(0.25)
        if cancel is not None and cancel.is_set():
            halt()
            raise Cancelled()
        if time.monotonic() - start > timeout:
            halt()
            raise LLMError(f"the {what} did not answer within {timeout // 60} minutes")
    if "err" in box:
        e = box["err"]
        if isinstance(e, (LLMError, Cancelled)):
            raise e
        raise LLMError(f"the {what} call failed: {type(e).__name__}: {e}")
    return box["ok"]


def _attachments(files):
    parts = []
    for p in files:
        with open(p, encoding="utf-8") as f:
            parts.append(f"=== ATTACHED FILE: {os.path.basename(p)} ===\n{f.read()}\n=== END FILE ===")
    return "\n\n".join(parts)


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)


def _run_command(cmd, cancel, timeout, what):
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, start_new_session=True)
    except OSError as e:
        raise LLMError(f"could not start the {what} command {cmd[0]!r}: {e}")
    start = time.monotonic()
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                _kill(proc)
                raise Cancelled()
            if time.monotonic() - start > timeout:
                _kill(proc)
                raise LLMError(f"the {what} did not answer within {timeout // 60} minutes")
    if proc.returncode != 0:
        tail = (err or out or "").strip().splitlines()[-3:]
        raise LLMError(f"the {what} failed (exit {proc.returncode}): {_scrub(' '.join(tail))[:400]}")
    return out


# ── writers ─────────────────────────────────────────────────────────────────────

class CommandLLM:
    """Runs `argv... -s SYSTEM PROMPT FILE...` (the shape the command route expects) and returns stdout.
    Reports no usage; the meter estimates it."""
    kind = "command"

    def __init__(self, argv, label="model", max_request_bytes=COMMAND_MAX_REQUEST):
        self.argv = list(argv)
        self.label = label
        self.max_request_bytes = max_request_bytes

    def __call__(self, system, prompt, files, cancel=None, timeout=1500):
        _check_size(self.max_request_bytes, system, prompt, files)
        return _run_command(self.argv + ["-s", system, prompt] + list(files), cancel,
                            timeout, "model")

    def describe(self):
        return {"provider": "command", "label": self.label,
                "program": os.path.basename(self.argv[0]) if self.argv else "",
                "max_request_bytes": self.max_request_bytes}


class OpenAIText:
    """Any OpenAI-compatible Chat Completions endpoint: OpenAI, OpenRouter, Ollama,
    LM Studio, vLLM... The attached files go in the user message."""
    kind = "openai"

    def __init__(self, model, key_env="OPENAI_API_KEY", base_url=OPENAI_BASE, label=None,
                 max_request_bytes=HTTP_MAX_REQUEST, reasoning_effort=None, transport=None):
        self.model = model
        self.key_env = key_env
        self.base_url = base_url.rstrip("/")
        self.label = label or model
        self.max_request_bytes = max_request_bytes
        self.reasoning_effort = reasoning_effort
        self.transport = transport       # tests hand in an httpx.MockTransport

    def __call__(self, system, prompt, files, cancel=None, timeout=1500):
        import httpx
        _check_size(self.max_request_bytes, system, prompt, files)
        key = _key(self.key_env) if self.key_env else ""
        body = {"model": self.model, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt + ("\n\n" + _attachments(files) if files else "")}]}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        client = httpx.Client(timeout=httpx.Timeout(timeout, connect=20), transport=self.transport)

        def call(stop):
            with client:
                try:
                    r = client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
                except (httpx.HTTPError, RuntimeError) as e:
                    if stop.is_set():
                        raise Cancelled()
                    raise LLMError(f"could not reach {self.base_url}: {_scrub(e, key)}")
            if stop.is_set():
                raise Cancelled()
            if r.status_code != 200:
                raise LLMError(f"the model returned HTTP {r.status_code}: {_scrub(_error_text(r), key)}")
            try:
                data = r.json()
                text = data["choices"][0]["message"]["content"] or ""
            except (ValueError, KeyError, IndexError, TypeError):
                raise LLMError("the model's answer was not in the Chat Completions shape")
            if key and key in text:          # a proxy that echoes headers must not leak it
                text = text.replace(key, "[key]")
            u = data.get("usage") or {}
            usage = None
            if isinstance(u.get("prompt_tokens"), int) and isinstance(u.get("completion_tokens"), int):
                usage = {"in": u["prompt_tokens"], "out": u["completion_tokens"], "exact": True}
            return Reply(text, usage)

        return _cancellable(call, cancel, timeout, "model", on_stop=client.close)

    @property
    def web_search(self):
        """OpenAI's own endpoint searches the web itself (the Responses API's web_search
        tool). OpenRouter, Ollama and the rest are not assumed to."""
        return self.base_url == OPENAI_BASE

    def web_research(self, system, prompt, cancel=None, timeout=900):
        """One call in which the model searches the web itself. Returns (Reply, [{url,
        title}]) where the list is every page OpenAI says the search saw or the answer
        cited: the only addresses a source may be taken from."""
        import httpx
        key = _key(self.key_env) if self.key_env else ""
        body = {"model": self.model, "instructions": system, "input": prompt,
                "tools": [{"type": "web_search"}], "include": ["web_search_call.action.sources"]}
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        client = httpx.Client(timeout=httpx.Timeout(timeout, connect=20), transport=self.transport)

        def call(stop):
            with client:
                try:
                    r = client.post(f"{self.base_url}/responses", json=body, headers=headers)
                except (httpx.HTTPError, RuntimeError) as e:
                    if stop.is_set():
                        raise Cancelled()
                    raise LLMError(f"could not reach {self.base_url}: {_scrub(e, key)}")
            if stop.is_set():
                raise Cancelled()
            if r.status_code != 200:
                raise LLMError(f"the web search call returned HTTP {r.status_code}: {_scrub(_error_text(r), key)}")
            try:
                data = r.json()
                items = data.get("output") or []
            except (ValueError, AttributeError):
                raise LLMError("the web search answer was not in the Responses shape")
            text, seen = [], []
            for it in items if isinstance(items, list) else []:
                if not isinstance(it, dict):
                    continue
                if it.get("type") == "web_search_call":
                    for src in ((it.get("action") or {}).get("sources") or []):
                        if isinstance(src, dict) and src.get("url"):
                            seen.append({"url": str(src["url"]), "title": str(src.get("title") or "")})
                if it.get("type") != "message":
                    continue
                for part in it.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        text.append(part.get("text") or "")
                        for a in part.get("annotations") or []:
                            if isinstance(a, dict) and a.get("type") == "url_citation" and a.get("url"):
                                seen.append({"url": str(a["url"]), "title": str(a.get("title") or "")})
            out = "\n".join(text)
            if key and key in out:
                out = out.replace(key, "[key]")
            u = data.get("usage") or {}
            usage = None
            if isinstance(u.get("input_tokens"), int) and isinstance(u.get("output_tokens"), int):
                usage = {"in": u["input_tokens"], "out": u["output_tokens"], "exact": True}
            return Reply(out, usage), seen

        return _cancellable(call, cancel, timeout, "model", on_stop=client.close)

    def describe(self):
        return {"provider": "openai", "label": self.label, "model": self.model,
                "base_url": self.base_url, "key_env": self.key_env,
                "key_set": bool(os.environ.get(self.key_env or "")),
                "max_request_bytes": self.max_request_bytes}


class AnthropicText:
    """Claude through the official SDK (pip install anthropic). Streams, so a long answer
    does not hit an HTTP timeout and Stop closes the connection."""
    kind = "anthropic"

    def __init__(self, model="claude-opus-5", key_env="ANTHROPIC_API_KEY", base_url=None,
                 label=None, max_request_bytes=HTTP_MAX_REQUEST, effort=None, fallbacks=None,
                 max_tokens=64000, client_factory=None):
        self.model = model
        self.key_env = key_env
        self.base_url = base_url
        self.label = label or model
        self.max_request_bytes = max_request_bytes
        self.effort = effort
        # Server-side refusal fallbacks: on by default for the models that take them.
        self.fallbacks = (model in ("claude-opus-5", "claude-fable-5-1")) if fallbacks is None else fallbacks
        self.max_tokens = max_tokens
        self.client_factory = client_factory    # tests hand in a fake client

    def _client(self, key):
        if self.client_factory:
            return self.client_factory(key)
        try:
            import anthropic
        except ImportError:
            raise LLMError("the anthropic package is not installed on the server: pip install anthropic")
        kw = {"api_key": key}
        if self.base_url:
            kw["base_url"] = self.base_url
        return anthropic.Anthropic(**kw)

    def __call__(self, system, prompt, files, cancel=None, timeout=1500):
        _check_size(self.max_request_bytes, system, prompt, files)
        key = _key(self.key_env)
        content = prompt + ("\n\n" + _attachments(files) if files else "")
        kw = {"model": self.model, "max_tokens": self.max_tokens, "system": system,
              "messages": [{"role": "user", "content": content}]}
        # Sent as extra body fields so an older SDK that has no named parameter for them
        # still passes them through.
        extra = {}
        if self.effort:
            extra["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            kw["betas"] = ["server-side-fallback-2026-07-01"]
            extra["fallbacks"] = "default"
        if extra:
            kw["extra_body"] = extra

        live = {}

        def close():
            # Stop and the timeout land here from the waiting thread: closing the stream and
            # the client ends the HTTP call even while no event is arriving.
            for thing in (live.get("stream"), live.get("client")):
                close_ = getattr(thing, "close", None)
                if close_:
                    close_()

        def call(stop):
            client = live["client"] = self._client(key)
            try:
                api = client.beta.messages if self.fallbacks else client.messages
                with api.stream(**kw) as stream:
                    live["stream"] = stream
                    if stop.is_set():
                        raise Cancelled()
                    for _ in stream.text_stream:
                        if stop.is_set():
                            raise Cancelled()
                    msg = stream.get_final_message()
            except (LLMError, Cancelled):
                raise
            except Exception as e:        # the SDK's own error classes; the key is scrubbed
                if stop.is_set():
                    raise Cancelled()
                raise LLMError(f"Claude call failed: {type(e).__name__}: {_scrub(e, key)}")
            if getattr(msg, "stop_reason", None) == "refusal":
                raise LLMError("Claude declined this request")
            text = "".join(getattr(b, "text", "") for b in msg.content
                           if getattr(b, "type", "") == "text")
            if key and key in text:
                text = text.replace(key, "[key]")
            u = msg.usage
            used_in = sum(int(getattr(u, k, 0) or 0) for k in
                          ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            return Reply(text, {"in": used_in, "out": int(getattr(u, "output_tokens", 0) or 0),
                                "exact": True})

        return _cancellable(call, cancel, timeout, "model", on_stop=close)

    # Models that take the dynamic-filtering web search; older ones get the basic tool.
    NEW_WEB_SEARCH = ("claude-opus-5", "claude-fable-5", "claude-mythos-5", "claude-sonnet-5",
                      "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-4-6")
    web_search = True

    def web_research(self, system, prompt, cancel=None, timeout=900, max_turns=5):
        """One conversation in which Claude searches the web itself (the web_search server
        tool), resumed on pause_turn. Returns (Reply, [{url, title}]): every page a search
        returned or the answer cited, the only addresses a source may be taken from."""
        key = _key(self.key_env)
        tool = "web_search_20260209" if self.model.startswith(self.NEW_WEB_SEARCH) else "web_search_20250305"
        messages = [{"role": "user", "content": prompt}]
        kw = {"model": self.model, "max_tokens": self.max_tokens, "system": system,
              "tools": [{"type": tool, "name": "web_search", "max_uses": 8}]}
        if self.effort:
            kw["extra_body"] = {"output_config": {"effort": self.effort}}
        live = {}

        def close():
            for thing in (live.get("stream"), live.get("client")):
                close_ = getattr(thing, "close", None)
                if close_:
                    close_()

        def call(stop):
            client = live["client"] = self._client(key)
            seen, text, used_in, used_out = [], [], 0, 0
            for _ in range(max_turns):
                try:
                    with client.messages.stream(messages=messages, **kw) as stream:
                        live["stream"] = stream
                        for _ in stream.text_stream:
                            if stop.is_set():
                                raise Cancelled()
                        msg = stream.get_final_message()
                except (LLMError, Cancelled):
                    raise
                except Exception as e:
                    if stop.is_set():
                        raise Cancelled()
                    raise LLMError(f"Claude web search failed: {type(e).__name__}: {_scrub(e, key)}")
                u = msg.usage
                used_in += sum(int(getattr(u, k, 0) or 0) for k in
                               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                used_out += int(getattr(u, "output_tokens", 0) or 0)
                for b in msg.content:
                    kind = getattr(b, "type", "")
                    if kind == "web_search_tool_result" and isinstance(getattr(b, "content", None), list):
                        for r in b.content:           # an error comes back as one object, not a list
                            if getattr(r, "url", None):
                                seen.append({"url": str(r.url), "title": str(getattr(r, "title", "") or "")})
                    elif kind == "text":
                        text.append(getattr(b, "text", ""))
                        for c in getattr(b, "citations", None) or []:
                            if getattr(c, "url", None):
                                seen.append({"url": str(c.url), "title": str(getattr(c, "title", "") or "")})
                if getattr(msg, "stop_reason", None) == "refusal":
                    raise LLMError("Claude declined this request")
                if getattr(msg, "stop_reason", None) != "pause_turn":
                    break
                # the server's search loop paused: send its turn back and it carries on
                messages.append({"role": "assistant", "content": msg.content})
            out = "".join(text)
            if key and key in out:
                out = out.replace(key, "[key]")
            return Reply(out, {"in": used_in, "out": used_out, "exact": True}), seen

        return _cancellable(call, cancel, timeout, "model", on_stop=close)

    def describe(self):
        return {"provider": "anthropic", "label": self.label, "model": self.model,
                "key_env": self.key_env, "key_set": bool(os.environ.get(self.key_env or "")),
                "max_request_bytes": self.max_request_bytes}


# ── images ──────────────────────────────────────────────────────────────────────

def sniff_image(path):
    """The file's real extension if it is a JPEG, PNG or WebP, else None."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    for magic, ext in IMAGE_TYPES.items():
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


class CommandImages:
    """Runs a command that writes a picture to {output}. {prompt} and {output} are
    substituted into single arguments; there is no shell, so the prompt is never parsed."""
    kind = "command"

    def __init__(self, argv, label="image model", timeout=600):
        self.argv = list(argv)
        if not any("{output}" in a for a in self.argv) or not any("{prompt}" in a for a in self.argv):
            raise ConfigError("[images] command must contain {prompt} and {output}")
        self.label = label
        self.timeout = timeout

    def __call__(self, prompt, out_path, cancel=None):
        """Returns the path of the picture the command wrote. That is not always out_path:
        `openclaw infer image generate` swaps the extension for the format it produced
        (asked for x.img, it writes x.jpg) and reports the real path in its JSON."""
        cmd = [a.replace("{prompt}", prompt).replace("{output}", out_path) for a in self.argv]
        out = _run_command(cmd, cancel, self.timeout, "image model")
        folder = os.path.realpath(os.path.dirname(out_path))
        stem = os.path.splitext(os.path.basename(out_path))[0]
        candidates = [out_path]
        try:
            start = out.find("{")
            data = json.loads(out[start:]) if start >= 0 else {}
            candidates += [o.get("path") for o in data.get("outputs", []) if isinstance(o, dict)]
        except ValueError:
            pass
        candidates += sorted(os.path.join(folder, f) for f in os.listdir(folder)
                             if os.path.splitext(f)[0] == stem)
        for c in candidates:
            if not isinstance(c, str) or not c:
                continue
            real = os.path.realpath(c)
            # Only a file the command wrote next to where it was asked to: never a path
            # its output merely names somewhere else on the machine.
            if (os.path.dirname(real) == folder and os.path.splitext(os.path.basename(real))[0] == stem
                    and os.path.isfile(real) and not os.path.islink(c) and os.path.getsize(real) > 0):
                return real
        raise LLMError("the image command finished but wrote no picture")

    def describe(self):
        return {"provider": "command", "label": self.label,
                "program": os.path.basename(self.argv[0]) if self.argv else ""}


class OpenAIImages:
    """The OpenAI Images API (or a compatible one): POST /images/generations."""
    kind = "openai"

    def __init__(self, model="gpt-image-1", key_env="OPENAI_API_KEY", base_url=OPENAI_BASE,
                 size="1536x1024", label=None, timeout=600, transport=None):
        self.model = model
        self.key_env = key_env
        self.base_url = base_url.rstrip("/")
        self.size = size
        self.label = label or model
        self.timeout = timeout
        self.transport = transport

    def __call__(self, prompt, out_path, cancel=None):
        import httpx
        key = _key(self.key_env)
        body = {"model": self.model, "prompt": prompt, "size": self.size, "n": 1}
        if self.model.startswith("gpt-image"):
            body["output_format"] = "jpeg"      # dall-e models reject this field
        client = httpx.Client(timeout=httpx.Timeout(self.timeout, connect=20),
                              transport=self.transport)

        def call(stop):
            with client:
                try:
                    r = client.post(f"{self.base_url}/images/generations", json=body,
                                    headers={"Authorization": f"Bearer {key}"})
                except (httpx.HTTPError, RuntimeError) as e:
                    if stop.is_set():
                        raise Cancelled()
                    raise LLMError(f"could not reach {self.base_url}: {_scrub(e, key)}")
            if stop.is_set():
                raise Cancelled()
            if r.status_code != 200:
                raise LLMError(f"the image model returned HTTP {r.status_code}: "
                               f"{_scrub(_error_text(r), key)}")
            try:
                item = r.json()["data"][0]
                if item.get("b64_json"):
                    raw = base64.b64decode(item["b64_json"], validate=True)
                elif str(item.get("url", "")).startswith("https://"):
                    got = httpx.get(item["url"], timeout=120, follow_redirects=True)
                    if got.status_code != 200 or len(got.content) > 50_000_000:
                        raise LLMError(f"the picture could not be fetched ({got.status_code})")
                    raw = got.content
                else:
                    raise KeyError("b64_json")
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                raise LLMError("the image model's answer held no picture")
            with open(out_path, "wb") as f:
                f.write(raw)
            return out_path

        return _cancellable(call, cancel, self.timeout, "image model", on_stop=client.close)

    def describe(self):
        return {"provider": "openai", "label": self.label, "model": self.model,
                "base_url": self.base_url, "key_env": self.key_env,
                "key_set": bool(os.environ.get(self.key_env or ""))}


# ── plan usage ──────────────────────────────────────────────────────────────────

class CommandPlan:
    """A subscription's usage windows from `openclaw status --usage --json` (or anything
    printing the same shape). Slow (~20s), so it is cached and refreshed in the background."""

    def __init__(self, argv, provider="openai", ttl=300):
        self.argv = list(argv)
        self.provider = provider
        self.ttl = ttl
        self.state = {"windows": None, "error": None, "ts": 0, "plan": None}
        self.lock = threading.Lock()
        self.busy = False

    def fetch(self):
        out = _run_command(self.argv, None, 90, "plan usage")
        start = out.find("{")
        data = json.loads(out[start:]) if start >= 0 else {}
        usage = data.get("usage") if isinstance(data, dict) else None
        provs = usage.get("providers") if isinstance(usage, dict) else None
        for p in provs if isinstance(provs, list) else []:
            if isinstance(p, dict) and p.get("provider") == self.provider:
                if p.get("error"):
                    raise LLMError(p["error"])
                wins = [{"label": str(w.get("label", "")), "used": float(w.get("usedPercent") or 0),
                         "reset_at": (float(w.get("resetAt") or 0) / 1000) or None}
                        for w in p.get("windows", []) if isinstance(w, dict)]
                return {"windows": wins, "plan": p.get("plan"), "name": p.get("displayName")}
        raise LLMError(f"no usage reported for {self.provider}")

    def refresh(self, force=False):
        """Start a background refresh if the cache is stale; never waits for it."""
        with self.lock:
            if self.busy or (not force and time.time() - self.state["ts"] < self.ttl):
                return
            self.busy = True

        def run():
            new = None
            try:
                got = self.fetch()
                new = {**got, "error": None, "ts": time.time()}
            except Exception as e:          # any shape of failure shows as "unavailable"
                new = {**self.state, "error": _scrub(e)[:200] or type(e).__name__, "ts": time.time()}
            finally:
                with self.lock:
                    if new is not None:
                        self.state = new
                    self.busy = False

        threading.Thread(target=run, daemon=True, name="plan-usage").start()

    def snapshot(self):
        self.refresh()
        with self.lock:
            return dict(self.state, busy=self.busy)


def _error_text(r):
    try:
        d = r.json()
        e = d.get("error", d)
        return e.get("message") if isinstance(e, dict) else str(e)
    except ValueError:
        return r.text[:300]


# ── config ──────────────────────────────────────────────────────────────────────

def _int(v, name):
    if v is None:
        return None
    if not isinstance(v, int) or v <= 0:
        raise ConfigError(f"{name} must be a positive whole number")
    return v


def _table(cfg, name):
    t = cfg.get(name) or {}
    if not isinstance(t, dict):
        raise ConfigError(f"[{name}] must be a table")
    return t


def from_config(cfg, *, llm_cmd=None, model_label=None):
    """(writer, images or None, plan or None) from a parsed config dict. llm_cmd and
    model_label are the command-line flags; given explicitly, they win for the writer."""
    w = _table(cfg, "writer")
    kind = w.get("provider", "command")
    if not w and llm_cmd is None:
        raise ConfigError("no [writer] is set up: run `uv run slopmill setup`")
    if llm_cmd is not None or kind == "command":
        cmd = llm_cmd if llm_cmd is not None else w.get("command")
        if not cmd:
            raise ConfigError("[writer] provider command needs command = \"...\"")
        writer = CommandLLM(shlex.split(cmd),
                            label=model_label or w.get("label") or "writer command",
                            max_request_bytes=_int(w.get("max_request_bytes"), "writer.max_request_bytes")
                            or COMMAND_MAX_REQUEST)
    elif kind == "openai":
        if not w.get("model"):
            raise ConfigError("[writer] provider openai needs model = \"...\"")
        writer = OpenAIText(w["model"], key_env=w.get("api_key_env", "OPENAI_API_KEY"),
                            base_url=w.get("base_url", OPENAI_BASE), label=model_label or w.get("label"),
                            max_request_bytes=_int(w.get("max_request_bytes"), "writer.max_request_bytes")
                            or HTTP_MAX_REQUEST, reasoning_effort=w.get("reasoning_effort"))
        if w.get("api_key_env") == "":
            writer.key_env = None      # a local server (Ollama) that takes no key
    elif kind == "anthropic":
        writer = AnthropicText(w.get("model", "claude-opus-5"),
                               key_env=w.get("api_key_env", "ANTHROPIC_API_KEY"),
                               base_url=w.get("base_url"), label=model_label or w.get("label"),
                               max_request_bytes=_int(w.get("max_request_bytes"), "writer.max_request_bytes")
                               or HTTP_MAX_REQUEST, effort=w.get("effort"),
                               fallbacks=w.get("fallbacks"))
    else:
        raise ConfigError(f"[writer] provider must be command, openai or anthropic, not {kind!r}")

    i = _table(cfg, "images")
    ikind = i.get("provider", "none")
    if ikind == "none":
        images = None
    elif ikind == "command":
        if not i.get("command"):
            raise ConfigError("[images] provider command needs command = \"...\"")
        images = CommandImages(shlex.split(i["command"]), label=i.get("label", "image model"))
    elif ikind == "openai":
        images = OpenAIImages(i.get("model", "gpt-image-1"),
                              key_env=i.get("api_key_env", "OPENAI_API_KEY"),
                              base_url=i.get("base_url", OPENAI_BASE),
                              size=i.get("size", "1536x1024"), label=i.get("label"))
    else:
        raise ConfigError(f"[images] provider must be command, openai or none, not {ikind!r}")

    pl = _table(cfg, "plan")
    plan = CommandPlan(shlex.split(pl["command"]), provider=pl.get("provider", "openai")) \
        if pl.get("command") else None
    return writer, images, plan


def load_config(path):
    if not path or not os.path.isfile(path):
        return {}
    with open(path, "rb") as f:
        try:
            return tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}")
