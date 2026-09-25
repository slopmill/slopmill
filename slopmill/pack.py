# SPDX-License-Identifier: MIT
"""A design: everything publication-specific about how an issue looks. Colours, inline
styles, URLs, the metadata schema, the component list and the Jinja templates that draw
each block.

The engine knows block *shapes* (paragraph, heading, quote, list, figure, container).
The design decides what each looks like and which fenced-div classes exist.

A design comes in two forms that load into the same object:
  - a folder (`pack.toml`, `templates/*.html`, optional site-head and page-CSS files),
    which is how one is written and kept in version control;
  - one portable file (`NAME.design.toml`), which is how one is shared and uploaded.
    `export_design` turns the first into the second.

Templates are read into memory when the design loads and rendered from there in a
sandboxed Jinja environment, so rendering never touches the filesystem.
"""
import json
import os
import re
import tomllib
from dataclasses import dataclass

import jinja2
from jinja2.sandbox import SandboxedEnvironment

from .errors import EnvironmentProblem

PACKS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packs")
SHAPES = ("figure", "container")
META_TYPES = ("string", "int")
DESIGN_FORMAT = "slopmill-design/1"
OLD_DESIGN_FORMATS = ("compositor-design/1",)      # files exported before the rename
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*\.html$")
MAX_DESIGN_BYTES = 512_000
MAX_TEMPLATE_BYTES = 64_000
# What a design file may contain. Anything else is refused rather than ignored, so a
# shared file cannot carry a setting (a command, a path) that some later version reads.
FILE_KEYS = {
    "": {"format", "pack", "page", "meta", "inline", "accents", "blocks", "components", "templates"},
    "pack": {"name", "asset_base", "read_url", "assets_dir"},
    "page": {"css", "site_head"},
    "meta": {"fields", "required", "types"},
    "blocks": {"paragraph", "quote", "list", "heading"},
}
COMPONENT_KEYS = {"shape", "template", "attrs", "params", "paragraphs", "describe"}
# Free-form tables in a design file hold text only: names to strings.
TEXT_TABLES = ("inline", "accents")


@dataclass
class Component:
    name: str
    shape: str
    template: str
    attrs: dict        # attribute name -> "required" | "optional"
    params: dict       # fixed values handed to the template
    min_paragraphs: int
    max_paragraphs: int
    describe: str = None   # offered to the writing model when set


class Pack:
    """One design, loaded. Build with load_pack / load_design_file / design_from_text."""

    def __init__(self, cfg, templates, *, site_head="", page_css="", origin="", root=None,
                 trusted=True):
        self.config = cfg
        self.templates = dict(templates)
        self.site_head = site_head or ""
        self.page_css = page_css or ""
        self.origin = origin
        self.root = root              # the folder, for a folder design; None for a file
        self.trusted = trusted        # False for an uploaded file: see check_untrusted
        path = origin or "design"
        try:
            p = cfg["pack"]
            self.name = p["name"]
            self.asset_base = p["asset_base"]
            self.read_url = p["read_url"]
            self.assets_dir = p.get("assets_dir", "images")
            m = cfg["meta"]
            self.meta_fields = list(m["fields"])
            self.meta_required = list(m.get("required", []))
            self.meta_types = dict(m.get("types", {}))
            self.inline = dict(cfg.get("inline", {}))
            self.accents = dict(cfg.get("accents", {}))
            b = cfg["blocks"]
            self.block_templates = {
                "paragraph": b["paragraph"],
                "quote": b.get("quote"),
                "list": b.get("list"),
            }
            self.heading_templates = {int(k): v for k, v in b.get("heading", {}).items()}
            self.components = {}
            for name, c in cfg.get("components", {}).items():
                if c["shape"] not in SHAPES:
                    raise EnvironmentProblem(
                        f"{path}: component {name!r} has unknown shape {c['shape']!r}")
                lo, hi = c.get("paragraphs", [0, 1_000])
                self.components[name] = Component(
                    name=name, shape=c["shape"], template=c["template"],
                    attrs=dict(c.get("attrs", {})), params=dict(c.get("params", {})),
                    min_paragraphs=lo, max_paragraphs=hi, describe=c.get("describe"))
            v = cfg.get("voice", {})
            # The old place a folder design listed its voice files. Only read now to seed a
            # voice pack the first time (see voices.py); a design file cannot have one.
            self.voice_system = v.get("system")
            self.voice_files = list(v.get("files", []))
        except KeyError as e:
            raise EnvironmentProblem(f"{path}: missing required key {e}")
        except (TypeError, ValueError, AttributeError) as e:
            raise EnvironmentProblem(f"{path}: a setting has the wrong type ({e})")
        if not isinstance(self.name, str) or not NAME_RE.match(self.name):
            raise EnvironmentProblem(
                f"{path}: name must be lowercase letters, digits and dashes, got {self.name!r}")
        if (not isinstance(self.assets_dir, str) or os.path.isabs(self.assets_dir)
                or ".." in self.assets_dir.replace("\\", "/").split("/")):
            raise EnvironmentProblem(
                f"{path}: assets_dir must be a folder next to the issue, not {self.assets_dir!r}")
        bad = {k: v for k, v in self.meta_types.items() if v not in META_TYPES}
        if bad:
            raise EnvironmentProblem(f"{path}: meta.types {bad} (known: {', '.join(META_TYPES)})")
        clash = [n for n in self.components if n in ("prompt", "draft")]
        if clash:
            raise EnvironmentProblem(f"{path}: {clash} are slopmill's own blocks, not components")
        unknown = [f for f in self.meta_required if f not in self.meta_fields]
        if unknown:
            raise EnvironmentProblem(f"{path}: required meta {unknown} not in meta.fields")
        wanted = [t for t in [*self.block_templates.values(), *self.heading_templates.values(),
                              *(c.template for c in self.components.values())] if t]
        missing = sorted({t for t in wanted if t not in self.templates})
        if missing:
            raise EnvironmentProblem(f"{path}: template {', '.join(missing)} not found")
        if not trusted:
            self.check_untrusted()

        # Sandboxed: a design is someone else's code once designs are shared, and a
        # template should draw HTML, not reach Python. DictLoader: no filesystem at all.
        self.env = SandboxedEnvironment(
            loader=jinja2.DictLoader(self.templates),
            autoescape=True,
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
        )

    def check_untrusted(self):
        """Rules for a design that came from outside this machine."""
        where = self.origin or "design file"
        for key in ("asset_base", "read_url"):
            val = getattr(self, key)
            if not isinstance(val, str) or not val.startswith("https://"):
                raise EnvironmentProblem(f"{where}: {key} must be an https:// address")

    def inside(self, rel):
        """Absolute path of a file inside a folder design. Absolute paths, ../ and symlinks
        out are refused: the build reads the source, the design and the asset folder only."""
        if self.root is None:
            raise EnvironmentProblem(f"design {self.name} is a single file; it has no {rel!r}")
        return _inside(self.root, rel, self.name)

    def voice(self):
        """(system text, [(relative path, absolute path)]) from a folder design's old
        [voice] list. Used to seed a voice pack; the writing model reads voice packs."""
        system = ""
        if self.voice_system:
            with open(self.inside(self.voice_system), encoding="utf-8") as f:
                system = f.read()
        return system, [(rel, self.inside(rel)) for rel in self.voice_files]

    def template(self, name):
        try:
            return self.env.get_template(name)
        except jinja2.TemplateNotFound:
            raise EnvironmentProblem(f"design {self.name}: template {name} not found")

    def figure_component(self):
        """The name of the first component that draws a picture, or None."""
        return next((c.name for c in self.components.values() if c.shape == "figure"), None)


def _inside(root, rel, name):
    root = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root, rel))
    if os.path.isabs(rel) or os.path.commonpath([root, full]) != root:
        raise EnvironmentProblem(f"pack {name}: {rel!r} is outside the pack")
    return full


# ── the folder form ──────────────────────────────────────────────────────────────

def load_folder(root):
    root = os.path.abspath(root)
    path = os.path.join(root, "pack.toml")
    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        raise EnvironmentProblem(f"no pack.toml in {root}")
    except tomllib.TOMLDecodeError as e:
        raise EnvironmentProblem(f"{path}: {e}")
    name = (cfg.get("pack") or {}).get("name", os.path.basename(root))

    def read(rel):
        if not rel:
            return ""
        with open(_inside(root, rel, name), encoding="utf-8") as f:
            return f.read()

    p = cfg.get("pack") or {}
    for rel in [(cfg.get("voice") or {}).get("system"), *(cfg.get("voice") or {}).get("files", [])]:
        if rel:
            _inside(root, rel, name)
    site_head = read(p.get("site_head"))
    page_css = read(p.get("page_css"))
    templates = {}
    tdir = os.path.join(root, "templates")
    real_tdir = os.path.realpath(tdir)
    if os.path.isdir(tdir):
        for dirpath, _, files in os.walk(tdir):
            for fn in sorted(files):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, tdir).replace(os.sep, "/")
                real = os.path.realpath(full)
                if os.path.commonpath([real_tdir, real]) != real_tdir:
                    raise EnvironmentProblem(
                        f"template {rel} resolves outside the pack: {real}")
                with open(real, encoding="utf-8") as f:
                    templates[rel] = f.read()
    return Pack(cfg, templates, site_head=site_head, page_css=page_css, origin=path,
                root=root, trusted=True)


def load_pack(name_or_path):
    """A folder path, a .design.toml path, or the name of a design shipped in packs/."""
    if os.path.isdir(name_or_path):
        return load_folder(name_or_path)
    if os.path.isfile(name_or_path) and name_or_path.endswith(".toml"):
        return load_design_file(name_or_path, trusted=True)
    candidate = os.path.join(PACKS_DIR, name_or_path)
    if os.sep not in name_or_path and os.path.isdir(candidate):
        return load_folder(candidate)
    raise EnvironmentProblem(f"no pack at {name_or_path!r} (and none named that in {PACKS_DIR})")


# ── the one-file form ────────────────────────────────────────────────────────────

def load_design_file(path, trusted=False):
    with open(path, "rb") as f:
        data = f.read()
    return design_from_text(data, origin=os.path.basename(path), trusted=trusted)


def design_from_text(data, origin="design file", trusted=False):
    """Load a design from the bytes of a .design.toml file. Refuses (EnvironmentProblem)
    anything that is not exactly a design: unknown keys, oversized files, bad names."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > MAX_DESIGN_BYTES:
        raise EnvironmentProblem(f"{origin}: {len(data) // 1024}KB; a design file is at most "
                                 f"{MAX_DESIGN_BYTES // 1000}KB")
    try:
        cfg = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise EnvironmentProblem(f"{origin}: not UTF-8 text")
    except tomllib.TOMLDecodeError as e:
        raise EnvironmentProblem(f"{origin}: not valid TOML: {e}")
    if cfg.get("format") != DESIGN_FORMAT and cfg.get("format") not in OLD_DESIGN_FORMATS:
        raise EnvironmentProblem(f'{origin}: the first line must be format = "{DESIGN_FORMAT}"')
    for section, allowed in FILE_KEYS.items():
        table = cfg if section == "" else cfg.get(section, {})
        if not isinstance(table, dict):
            raise EnvironmentProblem(f"{origin}: [{section}] must be a table")
        extra = sorted(set(table) - allowed)
        if extra:
            where = f"[{section}]" if section else "the top level"
            raise EnvironmentProblem(f"{origin}: {where} does not take {', '.join(extra)}")
    comps = cfg.get("components", {})
    if not isinstance(comps, dict):
        raise EnvironmentProblem(f"{origin}: [components] must be a table")
    for cname, c in comps.items():
        if not isinstance(c, dict):
            raise EnvironmentProblem(f"{origin}: [components.{cname}] must be a table")
        extra = sorted(set(c) - COMPONENT_KEYS)
        if extra:
            raise EnvironmentProblem(f"{origin}: [components.{cname}] does not take {', '.join(extra)}")
        for sub in ("attrs", "params"):
            if not _text_table(c.get(sub, {})):
                raise EnvironmentProblem(f"{origin}: components.{cname}.{sub} must map names to text")
    for tname in TEXT_TABLES:
        if not _text_table(cfg.get(tname, {})):
            raise EnvironmentProblem(f"{origin}: [{tname}] must map names to text")
    if not _text_table(cfg.get("blocks", {}).get("heading", {})) or not _text_table(
            cfg.get("meta", {}).get("types", {})):
        raise EnvironmentProblem(f"{origin}: blocks.heading and meta.types must map names to text")
    templates = cfg.get("templates", {})
    if not isinstance(templates, dict) or not templates:
        raise EnvironmentProblem(f"{origin}: [templates] is missing or empty")
    for name, text in templates.items():
        if not TEMPLATE_NAME_RE.match(name) or ".." in name:
            raise EnvironmentProblem(f"{origin}: template name {name!r} must look like name.html")
        if not isinstance(text, str):
            raise EnvironmentProblem(f"{origin}: template {name} must be text")
        if len(text.encode()) > MAX_TEMPLATE_BYTES:
            raise EnvironmentProblem(f"{origin}: template {name} is over "
                                     f"{MAX_TEMPLATE_BYTES // 1000}KB")
    page = cfg.get("page", {})
    for key in ("css", "site_head"):
        if not isinstance(page.get(key, ""), str):
            raise EnvironmentProblem(f"{origin}: page.{key} must be text")
    body = {k: v for k, v in cfg.items() if k not in ("format", "templates", "page")}
    return Pack(body, templates, site_head=page.get("site_head", ""),
                page_css=page.get("css", ""), origin=origin, root=None, trusted=trusted)


def _text_table(t):
    return isinstance(t, dict) and all(isinstance(v, str) for v in t.values())


def export_design(pack):
    """The design as one .design.toml file. Loading it gives the same design."""
    cfg = pack.config
    out = {"format": DESIGN_FORMAT,
           "pack": {k: cfg["pack"][k] for k in ("name", "asset_base", "read_url", "assets_dir")
                    if k in cfg["pack"]}}
    for key in ("meta", "inline", "accents", "blocks", "components"):
        if key in cfg:
            out[key] = cfg[key]
    out["page"] = {"css": pack.page_css, "site_head": pack.site_head}
    out["templates"] = dict(sorted(pack.templates.items()))
    head = ("# A slopmill design file: everything about how an issue looks, in one file.\n"
            "# Upload it in slopmill (Designs), or build with it:\n"
            "#   slopmill build issue.md --pack this-file.design.toml\n"
            "# How to write one: docs/DESIGN-FILES.md\n\n")
    return head + dump_toml(out)


# ── a small TOML writer (the standard library reads TOML but cannot write it) ───────

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k):
    k = str(k)
    return k if _BARE.match(k) else json.dumps(k, ensure_ascii=False)


def _str(s, multiline_ok=True):
    if multiline_ok and "\n" in s and "'''" not in s and not s.endswith("'") \
            and not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", s.replace("\r\n", "\n")) and "\r" not in s:
        return "'''\n" + s + "'''"
    return json.dumps(s, ensure_ascii=False)


def _value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return _str(v, multiline_ok=False)
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def _is_table(v):
    """Written as a [table] section: a dict holding at least one multi-line string or
    another table. Small flat dicts stay inline, as the hand-written pack.toml has them."""
    return isinstance(v, dict) and any(
        isinstance(x, dict) or (isinstance(x, str) and "\n" in x) for x in v.values())


def dump_toml(d, prefix=()):
    lines, tables = [], []
    for k, v in d.items():
        # Top-level tables, and each component, get a [section] of their own; that is how
        # a person reading the file finds them.
        if isinstance(v, dict) and (prefix in ((), ("components",)) or _is_table(v)):
            tables.append((k, v))
        elif isinstance(v, str):
            lines.append(f"{_key(k)} = {_str(v)}")
        else:
            lines.append(f"{_key(k)} = {_value(v)}")
    out = "\n".join(lines) + ("\n" if lines else "")
    for k, v in tables:
        name = ".".join(_key(x) for x in (*prefix, k))
        body = dump_toml(v, (*prefix, k))
        sub = (*prefix, k)
        simple = [x for x in v.values()
                  if not (isinstance(x, dict) and (sub == ("components",) or _is_table(x)))]
        out += ("\n" if out else "") + (f"[{name}]\n" if simple or not v else "") + body
    return out
