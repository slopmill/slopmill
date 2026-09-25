# SPDX-License-Identifier: AGPL-3.0-or-later
"""`slopmill setup`: the questions a new user answers once, written to the workspace.

Makes WORKSPACE/slopmill.toml (which model writes, which draws), WORKSPACE/.env (the API
key, readable only by this user), a voice pack from the starter design with the
newsletter's name in its brief, and a practice issue. Nothing is sent anywhere.
"""
import datetime
import getpass
import json
import os
import shutil
import sys

from .ids import atomic_write

PROVIDERS = {
    "openai": {"label": "OpenAI (GPT-6 Sol writes, gpt-image-2 draws pictures)",
               "model": "gpt-6-sol", "key_env": "OPENAI_API_KEY",
               "key_url": "https://platform.openai.com/api-keys"},
    "anthropic": {"label": "Anthropic (Claude writes; pictures off)",
                  "model": "claude-opus-5-5", "key_env": "ANTHROPIC_API_KEY",
                  "key_url": "https://console.anthropic.com/settings/keys"},
    "other": {"label": "Another service that speaks the OpenAI API (OpenRouter, Ollama, LM Studio...)",
              "model": "", "key_env": "WRITER_API_KEY", "key_url": ""},
}
ORDER = ["openai", "anthropic", "other"]


def _ask(question, default="", secret=False):
    shown = f" [{default}]" if default and not secret else ""
    try:
        got = (getpass.getpass if secret else input)(f"{question}{shown}: ")
    except EOFError:
        got = ""
    return got.strip() or default


def _toml_str(s):
    return json.dumps(s, ensure_ascii=False)       # a JSON string is a valid TOML basic string


def config_text(provider, model, key_env, base_url="", reasoning="high"):
    now = datetime.date.today().isoformat()
    lines = [f"# Written by `slopmill setup` on {now}. Edit freely: see docs/PROVIDERS.md.",
             "# The API key is not in this file; it is in .env next to it.", "",
             "[editor]", 'design = "starter"            # the default look for new issues', ""]
    if provider == "anthropic":
        lines += ["[writer]", 'provider = "anthropic"', f"model = {_toml_str(model)}",
                  f"api_key_env = {_toml_str(key_env)}", "", "[images]", 'provider = "none"']
    else:
        lines += ["[writer]", 'provider = "openai"', f"model = {_toml_str(model)}",
                  f"api_key_env = {_toml_str(key_env)}"]
        if base_url:
            lines.append(f"base_url = {_toml_str(base_url)}")
        if provider == "openai" and reasoning:
            lines.append(f'reasoning_effort = "{reasoning}"   # low, medium, high, xhigh')
        lines += ["", "[images]"]
        if provider == "openai":
            lines += ['provider = "openai"', 'model = "gpt-image-2"', 'size = "1536x1024"',
                      f"api_key_env = {_toml_str(key_env)}"]
        else:
            lines.append('provider = "none"')
    return "\n".join(lines) + "\n"


def write_env(path, key_env, key):
    """KEY=VALUE, created 0600 before anything is written into it."""
    existing = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            existing = [l for l in f.read().splitlines() if not l.startswith(key_env + "=")]
    body = "\n".join(existing + [f"{key_env}={key}"]) + "\n"
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(path + ".tmp", path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _env_names(path):
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [l.split("=", 1)[0].strip() for l in f if "=" in l and not l.lstrip().startswith("#")]


def load_env(path, environ=os.environ):
    """Read WORKSPACE/.env into the environment. A variable already set wins, so a key
    exported in the shell is never overridden by the file. Returns the names it set."""
    if not os.path.isfile(path):
        return []
    set_ = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            if k and k not in environ:
                environ[k] = v
                set_.append(k)
    return set_


def personalise_voice(workspace, pack, name, author):
    """Seed the starter voice pack (as the editor would on first start) and put the
    newsletter's name in its brief."""
    from .voices import VoiceLibrary
    lib = VoiceLibrary(os.path.join(workspace))
    lib.seed_from_design(pack)
    brief = os.path.join(workspace, "voices", pack.name, "writer.md")
    if not (name or author) or not os.path.isfile(brief):
        return
    with open(brief, encoding="utf-8") as f:
        text = f.read()
    who = "You are ghost-writing parts of " + (f"{name}, a newsletter" if name else "a newsletter") \
        + (f" by {author}" if author else " for its author") + "."
    first, _, rest = text.partition("\n")
    if first.startswith("You are ghost-writing"):
        text = who + rest if rest.startswith("\n") else who + "\n" + rest
    else:
        text = who + "\n\n" + text
    atomic_write(brief, text)


def add_practice_issue(workspace, pack):
    issues = os.path.join(workspace, "issues")
    if os.path.isdir(issues) and any(not n.startswith(".") for n in os.listdir(issues)):
        return None
    src = os.path.join(pack.root, "welcome.md") if pack.root else None
    if not src or not os.path.isfile(src):
        return None
    d = os.path.join(issues, "001-welcome")
    os.makedirs(os.path.join(d, pack.assets_dir), exist_ok=True)
    shutil.copyfile(src, os.path.join(d, "issue.md"))
    atomic_write(os.path.join(d, "settings.json"),
                 json.dumps({"design": pack.name, "voice": pack.name}, indent=1) + "\n")
    return "001-welcome"


def run(args):
    from .pack import load_pack
    ws = os.path.abspath(args.workspace)
    from .cli import workspace_config
    cfg_path = workspace_config(ws)
    env_path = os.path.join(ws, ".env")
    interactive = not args.yes and sys.stdin.isatty()
    print("slopmill setup\n")
    if os.path.isfile(cfg_path) and not args.force:
        print(f"✓ already set up: {cfg_path}\n  Run again with --force to answer the questions again.")
        return 0
    provider = args.provider
    if not provider:
        if not interactive:
            provider = "openai"
        else:
            print("Which AI service should write for you?")
            for i, p in enumerate(ORDER, 1):
                print(f"  {i}. {PROVIDERS[p]['label']}")
            pick = _ask("Choose 1-3", "1")
            provider = ORDER[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= 3 else "openai"
    if provider not in PROVIDERS:
        print(f"✗ --provider must be one of {', '.join(ORDER)}", file=sys.stderr)
        return 2
    info = PROVIDERS[provider]
    base_url = args.base_url or ""
    if provider == "other" and not base_url:
        base_url = _ask("The service's address (for Ollama: http://localhost:11434/v1)",
                        "http://localhost:11434/v1") if interactive else ""
        if not base_url:
            print("✗ another service needs --base-url", file=sys.stderr)
            return 2
    model = args.model or (_ask("Which model", info["model"]) if interactive and provider == "other"
                           else info["model"])
    if not model:
        print("✗ say which model with --model", file=sys.stderr)
        return 2
    key_env = args.key_env or info["key_env"]
    key = os.environ.get(key_env, "")
    if not key and interactive:
        hint = f" (get one at {info['key_url']})" if info["key_url"] else " (leave empty if it needs none)"
        key = _ask(f"Paste your API key{hint}; typing is hidden", secret=True)
    if not key and provider != "other":
        print(f"✗ no API key: paste one when asked, or set {key_env} and run setup again",
              file=sys.stderr)
        return 2
    name = args.name if args.name is not None else (_ask("Your newsletter's name", "") if interactive else "")
    author = args.author if args.author is not None else (_ask("Your name, as its author", "") if interactive else "")

    os.makedirs(ws, exist_ok=True)
    atomic_write(cfg_path, config_text(provider, model, key_env, base_url))
    print(f"✓ settings: {cfg_path}")
    if key:
        write_env(env_path, key_env, key)
        where = ("only your user account can read it" if os.name == "posix"
                 else "inside your own user folder")
        print(f"✓ your key: {env_path} ({where})")
    elif provider == "other":
        cfg = open(cfg_path, encoding="utf-8").read().replace(
            f"api_key_env = {_toml_str(key_env)}", 'api_key_env = ""   # this service takes no key')
        atomic_write(cfg_path, cfg)
    others = [k for k in _env_names(env_path) if k != key_env]
    if others:
        print(f"  also still in {env_path} from before: {', '.join(others)}. Delete the line if "
              f"you no longer want it stored.")
    pack = load_pack("starter")
    personalise_voice(ws, pack, name, author)
    print(f"✓ a voice pack to make your own: {os.path.join(ws, 'voices', pack.name)}")
    # The practice issue is written in the example: A. A. Milne's essay voice, a storybook design.
    example = load_pack("storybook")
    from .voices import VoiceLibrary
    VoiceLibrary(ws).seed_from_design(example)
    slug = add_practice_issue(ws, example)
    if slug:
        print("✓ a practice issue: In Which We Begin a Newsletter")
    print("\nNext, start it:\n  uv run slopmill start\n")
    return 0
