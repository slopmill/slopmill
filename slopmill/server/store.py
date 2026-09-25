# SPDX-License-Identifier: AGPL-3.0-or-later
"""Issues on disk. One directory per issue:

    issues/<slug>/issue.md       the document (the only source of truth for its text)
    issues/<slug>/settings.json  which design draws it and which voice writes it
    issues/<slug>/review.json    comments, proposals and the chat log
    issues/<slug>/history/       issue.md as it was before each model pass (for undo)
    issues/<slug>/images/        local images
    issues/<slug>/.runs/         what was sent to the model, and what came back
"""
import json
import os
import re
import threading
import time
import uuid

from .. import doc
from ..ids import atomic_write, new_id

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


class Conflict(Exception):
    """The file changed since the client last read it."""


class NotFound(Exception):
    pass


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60].strip("-") or "issue"


class Issue:
    def __init__(self, root, slug, ws):
        self.slug = slug
        self.dir = os.path.join(root, slug)
        self.path = os.path.join(self.dir, "issue.md")
        self.ws = ws
        self.lock = threading.RLock()

    @property
    def pack(self):
        """The design this issue is drawn with (the workspace default if its own is gone)."""
        return self.ws.design_of(self)

    # ── settings ──
    def settings(self):
        p = os.path.join(self.dir, "settings.json")
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        return {k: data.get(k) for k in ("design", "voice") if isinstance(data.get(k), str)}

    def save_settings(self, **changes):
        with self.lock:
            data = {**self.settings(), **{k: v for k, v in changes.items() if v is not None}}
            atomic_write(os.path.join(self.dir, "settings.json"), json.dumps(data, indent=1) + "\n")
            return data

    # ── the document ──
    def read(self):
        with open(self.path, encoding="utf-8", newline="") as f:
            text = f.read()
        return text, doc.revision(text)

    def load(self):
        with self.lock:
            text, rev = self.read()
            meta, blocks = doc.parse(text)
            return meta, blocks, rev, text

    def save(self, meta, blocks, base=None):
        """Write blocks to issue.md if the file is still at revision `base`."""
        with self.lock:
            text, rev = self.read()
            if base is not None and base != rev:
                raise Conflict(rev)
            current_meta, _ = doc.parse_meta(text)
            front = doc.front_matter_text(text) if meta == current_meta else None
            new_text = doc.save_text(meta, blocks, self.pack.meta_fields, front)
            if new_text != text:
                atomic_write(self.path, new_text)
            return doc.revision(new_text), new_text

    def snapshot(self, label):
        with self.lock:
            text, rev = self.read()
            hdir = os.path.join(self.dir, "history")
            os.makedirs(hdir, exist_ok=True)
            name = f"{time.strftime('%Y%m%d-%H%M%S')}-{label}-{rev}.md"
            atomic_write(os.path.join(hdir, name), text)
            return name

    def snapshots(self):
        hdir = os.path.join(self.dir, "history")
        if not os.path.isdir(hdir):
            return []
        return sorted(n for n in os.listdir(hdir) if n.endswith(".md"))

    def restore(self, name):
        with self.lock:
            if name not in self.snapshots():
                raise NotFound(name)
            with open(os.path.join(self.dir, "history", name), encoding="utf-8", newline="") as f:
                text = f.read()
            doc.parse(text)   # refuse to restore something that no longer reads
            self.snapshot("before-undo")
            atomic_write(self.path, text)
            return doc.revision(text)

    # ── review state ──
    def review(self):
        p = os.path.join(self.dir, "review.json")
        if not os.path.exists(p):
            return {"comments": [], "proposals": [], "chat": []}
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        for k in ("comments", "proposals", "chat"):
            data.setdefault(k, [])
        return data

    def save_review(self, data):
        atomic_write(os.path.join(self.dir, "review.json"), json.dumps(data, indent=1) + "\n")

    def update_review(self, fn):
        with self.lock:
            data = self.review()
            result = fn(data)
            self.save_review(data)
            return result

    def chat(self, role, text, **extra):
        entry = {"id": uuid.uuid4().hex[:10], "role": role, "text": text, "ts": time.time(), **extra}
        self.update_review(lambda d: d["chat"].append(entry))
        return entry

    def other_image_dirs(self, current):
        """Image folders in the issue other than `current`: after a change of design, the
        pictures drawn under the old one are still found."""
        out = []
        for n in sorted(os.listdir(self.dir)):
            p = os.path.join(self.dir, n)
            if (n != current and not n.startswith(".") and n != "history"
                    and os.path.isdir(p) and not os.path.islink(p)):
                out.append(p)
        return out

    def image_path(self, name):
        if not re.match(r"^[A-Za-z0-9._-]+$", name) or name.startswith("."):
            return None
        current = self.pack.assets_dir
        for d in [os.path.join(self.dir, current)] + self.other_image_dirs(current):
            p = os.path.join(d, name)
            if os.path.isfile(p) and not os.path.islink(p):
                return p
        return None


class Workspace:
    def __init__(self, root, pack, designs=None):
        self.root = os.path.abspath(root)
        self.issues_dir = os.path.join(self.root, "issues")
        self.pack = pack              # the default design
        self.designs = designs        # a DesignLibrary; None means only the default exists
        os.makedirs(self.issues_dir, exist_ok=True)
        self._issues = {}
        self._lock = threading.Lock()

    def design_of(self, iss):
        name = iss.settings().get("design")
        if not name or name == self.pack.name or self.designs is None:
            return self.pack
        try:
            return self.designs.get(name)
        except Exception:
            return self.pack

    def issue(self, slug):
        if not SLUG_RE.match(slug or ""):
            raise NotFound(slug)
        with self._lock:
            if slug not in self._issues:
                iss = Issue(self.issues_dir, slug, self)
                if not os.path.isfile(iss.path):
                    raise NotFound(slug)
                self._issues[slug] = iss
            return self._issues[slug]

    def list(self):
        out = []
        for slug in sorted(os.listdir(self.issues_dir), reverse=True):
            path = os.path.join(self.issues_dir, slug, "issue.md")
            if not SLUG_RE.match(slug) or not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    meta, _ = doc.parse_meta(f.read())
            except Exception:
                meta = {}
            out.append({"slug": slug, "title": meta.get("title") or slug,
                        "number": meta.get("number"), "updated": os.path.getmtime(path)})
        out.sort(key=lambda i: -i["updated"])
        return out

    def create(self, title, number=None, design=None, voice=None):
        """A new issue. `design` is a loaded design (default: the workspace default)."""
        design = design or self.pack
        title = (title or "").strip() or "Untitled issue"
        base = slugify(f"{int(number):03d}-{title}" if str(number or "").isdigit() else title)
        slug, n = base, 2
        while os.path.exists(os.path.join(self.issues_dir, slug)):
            slug, n = f"{base}-{n}", n + 1
        d = os.path.join(self.issues_dir, slug)
        os.makedirs(os.path.join(d, design.assets_dir), exist_ok=True)
        meta = {f: None for f in design.meta_fields}
        if "title" in meta:
            meta["title"] = title
        if "slug" in meta:
            meta["slug"] = slug
        if "number" in meta and str(number or "").isdigit():
            meta["number"] = str(int(number))
        first = doc.Block("prompt", new_id(set()), "What is this issue about? Write your notes "
                          "here, or switch this block to Prose and start writing.")
        text = doc.save_text(meta, [first], design.meta_fields)
        atomic_write(os.path.join(d, "settings.json"), json.dumps(
            {"design": design.name, **({"voice": voice} if voice else {})}, indent=1) + "\n")
        atomic_write(os.path.join(d, "issue.md"), text)
        return slug
