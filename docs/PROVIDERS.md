# Choosing a model

slopmill needs a model to write with, and optionally one to draw pictures and a way to
read your subscription's usage for the meter. Each is set in one file on the machine that
runs slopmill: `slopmill.toml` in the workspace folder (or pass `--config PATH`).
Nothing in the web app can change it. `uv run slopmill setup` writes it for you; this
page is for changing it by hand. Stop and start slopmill after an edit.

## Your own API key

Keys are never written in the config file. The file names an environment variable, and
the key itself goes in `workspace/.env` as `NAME=key` (setup does this, readable only by
you), or in that variable where slopmill runs. A variable already set wins over the file.

**OpenAI**

```toml
[writer]
provider = "openai"
model = "gpt-6-sol"               # or gpt-6-astra (strongest), gpt-6-luna (cheapest)
api_key_env = "OPENAI_API_KEY"
reasoning_effort = "high"         # low, medium, high, xhigh: more thinking, more cost

[images]
provider = "openai"
model = "gpt-image-2"
size = "1536x1024"
```

**Claude**

```toml
[writer]
provider = "anthropic"
model = "claude-opus-5-5"         # or claude-sonnet-5, faster and cheaper
api_key_env = "ANTHROPIC_API_KEY"
```

Claude does not draw pictures: keep `[images]` on OpenAI (with an OpenAI key as well) or
`provider = "none"`.

**Anything OpenAI-compatible**: OpenRouter, Ollama, LM Studio, vLLM, Groq...

```toml
[writer]
provider = "openai"
base_url = "https://openrouter.ai/api/v1"     # or http://localhost:11434/v1 for Ollama
model = "some/model"
api_key_env = "OPENROUTER_API_KEY"            # "" for a local server that takes no key
max_request_bytes = 400000                    # what one call may send, voice pack included
```

With an API key you pay the provider per call. The meter shows exact token counts because
the API reports them.

## A local command

Any program that takes `-s SYSTEM PROMPT FILE...` and prints the answer: a script around a
command-line tool you already use, or a model running on your own machine. With OpenClaw
installed, its image and usage commands fit the other two slots:

```toml
[writer]
provider = "command"
command = "my-writer --model gpt-6-sol"      # your program: -s SYSTEM PROMPT FILE...
label = "GPT-6 Sol"

[images]
provider = "command"
command = "openclaw infer image generate --model openai/gpt-image-2 --prompt {prompt} --output {output} --output-format jpeg --json"
label = "gpt-image-2"

[plan]                                  # the meter's 5-hour and weekly windows (whole account)
command = "openclaw status --usage --json"
provider = "openai"
```

`{prompt}` and `{output}` are filled in as single arguments; no shell ever reads them. A
command reports no token counts, so the meter estimates them (about four characters a
token) and marks them with ≈. The `[plan]` bars are different: they show your whole
subscription, so everything else signed in to that account counts there too. One call through a command is capped at about 115 KB,
because the whole prompt travels as one command-line argument.

Signing in with a subscription (the way OpenClaw does) borrows the sign-in of the
provider's own app. Whether a provider allows that for other tools is the provider's call
and can change, so for anything you share or publish, API keys are the supported route.

## The free spelling and grammar check

Proof has two checks. **Check with AI** uses your writer, knows from your voice files what
you do on purpose, and costs tokens. **Quick check (free)** uses
[LanguageTool](https://languagetool.org), which needs no account and no key: it looks for
spelling mistakes, doubled words, grammar and capitals, never style. Words that appear in
your voice pack (names, your own coinages) and words in `workspace/dictionary.txt` (one per
line) are never "corrected". The text of the issue goes to LanguageTool's free service,
which takes about 20 checks a minute. To use your own LanguageTool server instead, or to
turn the free check off:

```toml
[checker]
url = "http://localhost:8010/v2"   # "" turns it off
language = "en-US"
```

## Research

Say "research", "look up", "find out" or "latest numbers" in a prompt, a comment or a chat
question, or write a `Chart:` prompt, and slopmill looks things up before the writer
answers. What was found goes to the writer as numbered sources; a chart prints the sites it
used under it, and Draft links to them. Nothing found means the writer is told so, and a
chart made from its memory says "not checked" on it.

```toml
[research]
provider = "auto"      # the default
```

- **auto**: with an OpenAI key (on OpenAI's own endpoint) or an Anthropic key, the model
  searches the web itself, and every source it lists is checked against the pages its
  search actually returned. Searches are billed by the provider with your tokens.
  Otherwise, DuckDuckGo.
- **duckduckgo**: the writer suggests a few searches; slopmill searches DuckDuckGo (no key,
  no account) and reads the top pages itself, public https addresses only. DuckDuckGo
  slows down anyone who searches a lot, so a busy afternoon can come back with nothing.
- **command**: your own search program: `command = "my-search --json {query}"`, printing
  `[{"title", "url", "snippet"}]`.
- **off**: never searches. Search words go to a search engine only when research runs.

## No config file

`start` and `serve` both ask you to run `setup` first. `--llm "COMMAND"` and
`--model-label` on the command line still work and win over `[writer]`.
