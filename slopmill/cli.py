# SPDX-License-Identifier: AGPL-3.0-or-later
"""slopmill setup | start | serve | build | design | ids"""
import argparse
import hashlib
import json
import os
import sys

from . import ids as ids_mod
from . import render, source as src
from .errors import CompileError, EnvironmentProblem
from .pack import load_pack

STATE = ".slopmill.json"
OLD_STATE = ".compositor.json"
OUTPUTS = ("body.html", "body-site.html", "body-email.html", "meta.json")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def plan_writes(out_dir, files, force=False):
    """Decide which files may be written. A file slopmill did not write, or one edited
    since it wrote it, is refused: in an existing issue directory that file is the only
    copy of somebody's hand-written work.

    Returns (writes, refusals, state).
    """
    state_path = os.path.join(out_dir, STATE)
    if not os.path.exists(state_path) and os.path.exists(os.path.join(out_dir, OLD_STATE)):
        state_path = os.path.join(out_dir, OLD_STATE)       # written before the rename
    state = {}
    if os.path.exists(state_path):
        try:
            state = json.loads(_read(state_path)).get("outputs", {})
        except (ValueError, AttributeError):
            state = {}
    writes, refusals = {}, []
    for name, text in files.items():
        path = os.path.join(out_dir, name)
        if os.path.exists(path):
            current = _read(path)
            if current == text:
                continue
            if not force and state.get(name) != sha(current):
                why = ("was edited after slopmill wrote it" if name in state
                       else "was not written by slopmill")
                refusals.append(f"{path} {why}")
                continue
        writes[name] = text
    return writes, refusals, state


def issue_design(source):
    """The design an issue in a workspace was set to (issues/SLUG/settings.json), found in
    packs/ or among the designs uploaded to that workspace; the starter design otherwise."""
    d = os.path.dirname(os.path.abspath(source))
    try:
        with open(os.path.join(d, "settings.json"), encoding="utf-8") as f:
            name = json.load(f).get("design") or ""
    except (OSError, ValueError, AttributeError):
        name = ""
    uploaded = os.path.join(os.path.dirname(os.path.dirname(d)), "designs", f"{name}.design.toml")
    if name and os.path.isfile(uploaded):
        return load_pack(uploaded)
    try:
        return load_pack(name) if name else load_pack("starter")
    except EnvironmentProblem:
        return load_pack("starter")


def cmd_build(args):
    pack = load_pack(args.pack) if args.pack else issue_design(args.source)
    print(f"· drawn with the design {pack.name}", file=sys.stderr)
    source = src.read_source(args.source)
    result = render.compile_source(source, pack, check_assets=not args.no_asset_check)
    out_dir = args.out or os.path.dirname(os.path.abspath(args.source))
    os.makedirs(out_dir, exist_ok=True)
    files = dict(zip(OUTPUTS, (result.body, result.site, result.email, result.meta_json)))
    writes, refusals, state = plan_writes(out_dir, files, force=args.force)
    if refusals:
        print("✗ refusing to overwrite — nothing was written:", file=sys.stderr)
        for r in refusals:
            print(f"    {r}", file=sys.stderr)
        print("  Move the file aside, or pass --force to replace it.", file=sys.stderr)
        return 1
    state = {name: sha(text) for name, text in files.items()}
    writes[STATE] = json.dumps({"version": 1, "outputs": state}, indent=2) + "\n"
    ids_mod.write_all(out_dir, writes)
    kinds = {}
    for b in result.blocks:
        kinds[b.kind] = kinds.get(b.kind, 0) + 1
    print(f"✓ {len(result.blocks)} blocks: "
          + ", ".join(f"{k} x{v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])))
    for name in OUTPUTS:
        state_word = "written" if name in writes else "unchanged"
        print(f"  {os.path.join(out_dir, name)}  ({len(files[name])} bytes, {state_word})")
    if args.no_asset_check:
        print(f"  asset check SKIPPED ({len(result.assets)} local files not verified)")
    else:
        print(f"  {len(result.assets)} local assets found in {pack.assets_dir}/")
    return 0


def quickcheck_from(cfg):
    """[checker] in the config: LanguageTool's free service unless url = "" turns it off."""
    from .quickcheck import DEFAULT_URL, LanguageTool
    t = cfg.get("checker") or {}
    url = t.get("url", DEFAULT_URL)
    return LanguageTool(url, t.get("language", "en-US")) if url else None


def workspace_config(ws):
    """WORKSPACE/slopmill.toml; a workspace set up before the rename has compositor.toml."""
    new = os.path.join(ws, "slopmill.toml")
    old = os.path.join(ws, "compositor.toml")
    return old if not os.path.isfile(new) and os.path.isfile(old) else new


def _port_free(host, port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # The same test uvicorn makes: a port left in TIME_WAIT by a server that just
        # stopped is free. (On Windows SO_REUSEADDR would let two servers share it.)
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host if host != "localhost" else "127.0.0.1", port))
            return True
        except OSError:
            return False


def cmd_serve(args, open_browser=False):
    import shlex
    import shutil
    import socket

    import uvicorn

    from . import providers
    from .server.app import create_app
    from .setup import load_env
    config_path = args.config or workspace_config(args.workspace)
    if args.config and not os.path.isfile(args.config):
        print(f"✗ no config file at {args.config}", file=sys.stderr)
        return 2
    if not os.path.isfile(config_path) and args.llm is None:
        print("✗ no model is set up for this workspace yet. Run:\n    uv run slopmill setup",
              file=sys.stderr)
        return 2
    # The API key lives in WORKSPACE/.env (written by setup); a variable already set wins.
    load_env(os.path.join(args.workspace, ".env"))
    try:
        cfg = providers.load_config(config_path)
    except providers.ConfigError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    pack = load_pack(args.pack or (cfg.get("editor") or {}).get("design") or "starter")
    os.makedirs(args.workspace, exist_ok=True)
    # One editor per workspace: its jobs, locks and event stream live in this process.
    lock = open(os.path.join(args.workspace, ".serve.lock"), "w")
    try:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        pass
    except OSError:
        print(f"✗ another slopmill is already serving {args.workspace}", file=sys.stderr)
        return 2
    try:
        writer, images, plan = providers.from_config(cfg, llm_cmd=args.llm,
                                                     model_label=args.model_label)
        checker = quickcheck_from(cfg)
        from . import research as research_mod
        searcher = research_mod.from_config(cfg, writer)
    except (providers.ConfigError, ValueError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    app = create_app(workspace=args.workspace, pack=pack, llm=writer,
                     model_label=writer.label, images=images, plan=plan,
                     config_path=config_path if os.path.isfile(config_path) else None,
                     checker=checker, research=searcher,
                     lint_argv=shlex.split(args.lint) if args.lint else None)
    token = app.state.token
    hosts = ["127.0.0.1"]
    if args.host == "0.0.0.0":
        try:
            out = __import__("subprocess").run(["hostname", "-I"], capture_output=True,
                                               text=True).stdout.split()
            hosts = [h for h in out if "." in h] or [socket.gethostname()]
        except OSError:
            hosts = [socket.gethostname()]
    elif args.host not in ("127.0.0.1", "localhost"):
        hosts = [args.host]
    print(f"Writer: {writer.describe().get('provider')} ({writer.label}) · pictures: "
          f"{images.describe().get('provider') + ' (' + images.label + ')' if images else 'off'}"
          f" · plan meter: {'on' if plan else 'off'}", flush=True)
    if not _port_free(args.host, args.port):
        print(f"✗ port {args.port} is in use (is slopmill already running?). "
              f"Pick another with --port", file=sys.stderr)
        return 2
    print("slopmill is running. Open:", flush=True)
    for h in hosts:
        print(f"  http://{h}:{args.port}/?t={token}", flush=True)
    if open_browser:
        import threading
        import webbrowser
        link = f"http://127.0.0.1:{args.port}/?t={token}"
        threading.Timer(1.2, lambda: webbrowser.open(link)).start()
        print("Opening it in your browser. Press Ctrl+C here to stop slopmill.", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_start(args):
    """serve, with the defaults a person running it on their own computer wants: this
    workspace, this computer only, a free port, and the browser opened on the link."""
    if not os.path.isfile(workspace_config(args.workspace)):
        print("✗ slopmill is not set up here yet. Run this first:\n    uv run slopmill setup",
              file=sys.stderr)
        return 2
    port = args.port
    for p in range(args.port, args.port + 20):
        if _port_free("127.0.0.1", p):
            port = p
            break
    ns = argparse.Namespace(workspace=args.workspace, pack=None, host="127.0.0.1", port=port,
                            config=None, llm=None, model_label=None, lint=None)
    return cmd_serve(ns, open_browser=not args.no_browser)


def cmd_demo(args):
    """The public click-through demo (see slopmill/demo.py): no model, no key."""
    import json as _json

    import uvicorn

    from .demo import build
    extra = {}
    meta = os.path.join(args.template, "demo.json")
    if os.path.isfile(meta):
        with open(meta, encoding="utf-8") as f:
            extra = _json.load(f)
    tpl = os.path.join(args.template, "workspace")
    picture = args.picture or os.path.join(args.template, "picture.jpg")
    for need in (tpl, picture):
        if not os.path.exists(need):
            print(f"✗ the demo needs {need}", file=sys.stderr)
            return 2
    app = build(tpl, picture, args.root, site_dir=args.site, prepared=extra.get("prepared"),
                pictures=extra.get("pictures"), prompts=extra.get("prompts"), home_url=args.home_url,
                limit=args.limit, idle=args.idle, per_ip=args.per_ip)
    print(f"slopmill demo on {args.host}:{args.port} · intro pages: {args.site or 'none'}", flush=True)
    # proxy headers are read by the demo itself (demo.visitor_ip), which checks where they came from
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", proxy_headers=False)
    return 0


def cmd_setup(args):
    from .setup import run
    return run(args)


def cmd_design(args):
    from .designs import DesignError, check_design_text
    from .pack import export_design
    if args.action == "export":
        text = export_design(load_pack(args.target))
        if args.out:
            ids_mod.write_all(os.path.dirname(os.path.abspath(args.out)) or ".",
                              {os.path.basename(args.out): text})
            print(f"✓ wrote {args.out} ({len(text.encode()) // 1024}KB)")
        else:
            sys.stdout.write(text)
        return 0
    with open(args.target, "rb") as f:
        data = f.read()
    try:
        pack = check_design_text(data, origin=os.path.basename(args.target))
    except DesignError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(f"✓ {pack.name} loads and compiles the sample issue "
          f"({len(pack.components)} components, {len(pack.templates)} templates)")
    return 0


def cmd_ids(args):
    added = ids_mod.stamp(args.source, write=not args.dry_run)
    if not added:
        print(f"✓ every block in {args.source} already has an ID")
    else:
        verb = "would stamp" if args.dry_run else "stamped"
        print(f"✓ {verb} {len(added)} ID{'s' if len(added) != 1 else ''} into {args.source}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="slopmill")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="compile an issue source into site + email HTML")
    b.add_argument("source")
    b.add_argument("--pack", default=None,
                   help="a design folder, file or name (default: the design the issue is set to)")
    b.add_argument("--out", help="output directory (default: next to the source)")
    b.add_argument("--no-asset-check", action="store_true",
                   help="do not require referenced images to exist locally")
    b.add_argument("--force", action="store_true",
                   help="overwrite output files slopmill did not write")
    b.set_defaults(fn=cmd_build)

    su = sub.add_parser("setup", help="first run: choose the model, save your API key, make a practice issue")
    su.add_argument("--workspace", default="workspace", help="folder for your issues and settings")
    su.add_argument("--provider", choices=["openai", "anthropic", "other"], default=None)
    su.add_argument("--model", default=None)
    su.add_argument("--base-url", default=None, help="for another OpenAI-compatible service")
    su.add_argument("--key-env", default=None, help="the environment variable that holds the key")
    su.add_argument("--name", default=None, help="your newsletter's name")
    su.add_argument("--author", default=None, help="your name")
    su.add_argument("--yes", action="store_true", help="ask nothing; take the key from the environment")
    su.add_argument("--force", action="store_true", help="answer the questions again")
    su.set_defaults(fn=cmd_setup)

    st = sub.add_parser("start", help="open the editor on this computer (after setup)")
    st.add_argument("--workspace", default="workspace")
    st.add_argument("--port", type=int, default=8440)
    st.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    st.set_defaults(fn=cmd_start)

    dm = sub.add_parser("demo", help="the public click-through demo: prepared text, no model, no key")
    dm.add_argument("--template", required=True, help="folder with workspace/, picture.jpg and demo.json")
    dm.add_argument("--picture", default=None)
    dm.add_argument("--root", default="/tmp/slopmill-demo", help="where visitors' copies live (wiped at start)")
    dm.add_argument("--site", default=None, help="static intro pages, served on hosts not starting with demo.")
    dm.add_argument("--home-url", default="/")
    dm.add_argument("--host", default="127.0.0.1")
    dm.add_argument("--port", type=int, default=8095)
    dm.add_argument("--limit", type=int, default=60, help="demo sessions at once")
    dm.add_argument("--idle", type=int, default=1800, help="seconds before an idle session is removed")
    dm.add_argument("--per-ip", type=int, default=20, help="new sessions an hour from one address")
    dm.set_defaults(fn=cmd_demo)

    s = sub.add_parser("serve", help="run the editor (all the options; most people want start)")
    s.add_argument("--workspace", default="workspace", help="folder holding issues/")
    s.add_argument("--pack", default=None,
                   help="the default design (default: [editor] design in the config, else starter)")
    s.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 to reach it from other computers on the network")
    s.add_argument("--port", type=int, default=8440)
    s.add_argument("--config", default=None,
                   help="provider config (default: WORKSPACE/slopmill.toml if it exists); "
                        "see docs/PROVIDERS.md")
    s.add_argument("--llm", default=None,
                   help="command that runs the writing model: CMD -s SYSTEM PROMPT FILES... "
                        "(overrides [writer] in the config)")
    s.add_argument("--model-label", default=None)
    s.add_argument("--lint", default=None,
                   help="lint command run on the compiled body; {body} and {subject} are filled in")
    s.set_defaults(fn=cmd_serve)

    dz = sub.add_parser("design", help="export a design as one file, or check a design file")
    dz.add_argument("action", choices=["export", "check"])
    dz.add_argument("target", help="export: a pack folder or name; check: a .design.toml file")
    dz.add_argument("--out", help="export: write here instead of printing")
    dz.set_defaults(fn=cmd_design)

    i = sub.add_parser("ids", help="stamp an ID onto every block that lacks one")
    i.add_argument("source")
    i.add_argument("--dry-run", action="store_true")
    i.set_defaults(fn=cmd_ids)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except CompileError as e:
        print(f"✗ {e.render()}", file=sys.stderr)
        return 1
    except EnvironmentProblem as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
