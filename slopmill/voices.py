# SPDX-License-Identifier: MIT
"""Voice packs: how an issue sounds, as a folder of plain text files.

    WORKSPACE/voices/NAME/voice.json     which files, in what order, with what role, on or off
    WORKSPACE/voices/NAME/*.md|*.txt     the files themselves (flat: no subfolders)
    WORKSPACE/voices/NAME/.history/      every version a file had before it was replaced or deleted

Roles:
  brief   the standing instruction, sent as the system prompt. At most one.
  rules   what the writer does and never does, attached to every call.
  sample  the author's own unedited writing, attached to every call. The part that matters.

Every path is a bare filename checked against a pattern and then resolved: nothing outside
the pack's folder can be read or written, and a symlink is refused rather than followed.
"""
import json
import os
import re
import shutil
import threading
import time

from .ids import atomic_write

FORMAT = "slopmill-voice/1"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*\.(md|txt)$")
ROLES = ("brief", "rules", "sample")
MAX_FILE_BYTES = 60_000
MAX_FILES = 40

STARTER_BRIEF = """You are ghost-writing part of a newsletter for its author. Write the way the
author writes in the attached samples, not the way a helpful assistant writes.

Replace this with: who the author is, who reads the newsletter, and the few rules that
matter most (things they never say, how they handle numbers and links). Keep it short.
The samples do most of the work.
"""


class VoiceError(Exception):
    pass


def history_dir(parent):
    """parent/.history, created if missing, refused if it is a link or leads elsewhere."""
    hdir = os.path.join(parent, ".history")
    if os.path.islink(hdir):
        raise VoiceError(".history is a link; it must be a real folder inside the pack")
    os.makedirs(hdir, exist_ok=True)
    real_parent = os.path.realpath(parent)
    if os.path.dirname(os.path.realpath(hdir)) != real_parent:
        raise VoiceError(".history is outside the pack")
    return hdir


def clean_filename(name):
    """A safe flat filename from whatever a browser sent, or VoiceError."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = re.sub(r"\s+", "-", base)
    base = re.sub(r"[^A-Za-z0-9._-]", "", base).lstrip("._-")
    if not base.lower().endswith((".md", ".txt")):
        raise VoiceError(f"{name!r}: only .md and .txt files go in a voice pack")
    stem, ext = base.rsplit(".", 1)
    base = stem[:76].rstrip("._-") + "." + ext.lower()
    if not FILE_RE.match(base) or ".." in base:
        raise VoiceError(f"{name!r} is not a usable filename")
    return base


class VoicePack:
    def __init__(self, root, name):
        if not NAME_RE.match(name or ""):
            raise VoiceError(f"{name!r} is not a voice pack name")
        self.name = name
        self.dir = os.path.join(root, name)
        self.lock = threading.RLock()

    # ── paths ──
    def path(self, fname):
        if not isinstance(fname, str) or not FILE_RE.match(fname) or ".." in fname:
            raise VoiceError(f"{fname!r} is not a file in this voice pack")
        full = os.path.join(self.dir, fname)
        if os.path.islink(full):
            raise VoiceError(f"{fname} is a link; voice files must be real files")
        real_dir = os.path.realpath(self.dir)
        if os.path.commonpath([real_dir, os.path.realpath(full)]) != real_dir:
            raise VoiceError(f"{fname} is outside the voice pack")
        return full

    # ── the manifest ──
    def manifest(self):
        p = os.path.join(self.dir, "voice.json")
        if os.path.islink(p):
            raise VoiceError(f"{self.name}/voice.json is a link; it must be a real file in the pack")
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except ValueError as e:
            raise VoiceError(f"{self.name}/voice.json is not valid JSON: {e}")
        files, seen = [], set()
        for e in data.get("files", []) if isinstance(data, dict) else []:
            if not isinstance(e, dict):
                continue
            path, role = e.get("path"), e.get("role", "rules")
            if not isinstance(path, str) or not FILE_RE.match(path) or path in seen \
                    or role not in ROLES:
                continue
            seen.add(path)
            files.append({"path": path, "role": role, "on": e.get("on", True) is not False})
        return {"format": FORMAT, "files": files}

    def _save_manifest(self, m):
        briefs = [e for e in m["files"] if e["role"] == "brief"]
        if len(briefs) > 1:
            raise VoiceError("a voice pack has at most one brief")
        atomic_write(os.path.join(self.dir, "voice.json"),
                     json.dumps({"format": FORMAT, "files": m["files"]}, indent=1) + "\n")

    def files(self):
        out = []
        for e in self.manifest()["files"]:
            p = os.path.join(self.dir, e["path"])
            ok = os.path.isfile(p) and not os.path.islink(p)
            out.append({**e, "size": os.path.getsize(p) if ok else None, "missing": not ok})
        return out

    # ── what the model gets ──
    def voice(self):
        """(system text, [(filename, absolute path)]): the brief, and every other file that
        is on, in manifest order."""
        system, attach = "", []
        for e in self.files():
            if not e["on"] or e["missing"]:
                continue
            p = self.path(e["path"])
            if e["role"] == "brief":
                with open(p, encoding="utf-8") as f:
                    system = f.read()
            else:
                attach.append((e["path"], p))
        return system, attach

    def request_bytes(self):
        system, files = self.voice()
        return len(system.encode()) + sum(os.path.getsize(p) for _, p in files)

    def summary(self):
        return {"name": self.name, "files": self.files(), "bytes": self.request_bytes()}

    # ── changes ──
    def _keep(self, fname):
        p = self.path(fname)
        if os.path.isfile(p):
            hdir = history_dir(self.dir)
            dest = os.path.join(hdir, f"{fname}.{time.strftime('%Y%m%d-%H%M%S')}"
                                      f"-{time.time_ns() % 1_000_000:06d}")
            with open(p, "rb") as src, open(dest, "xb") as out:   # x: never through a planted file
                shutil.copyfileobj(src, out)

    @staticmethod
    def _check_text(text, fname):
        if not isinstance(text, str):
            raise VoiceError(f"{fname}: the file must be text")
        if "\x00" in text:
            raise VoiceError(f"{fname}: not a text file")
        n = len(text.encode("utf-8"))
        if n > MAX_FILE_BYTES:
            raise VoiceError(f"{fname} is {n // 1024}KB; a voice file is at most "
                             f"{MAX_FILE_BYTES // 1000}KB")

    def read(self, fname):
        if fname not in {e["path"] for e in self.manifest()["files"]}:
            raise VoiceError(f"{fname} is not in this voice pack")
        with open(self.path(fname), encoding="utf-8") as f:
            return f.read()

    def write(self, fname, text):
        """Replace a file that is already in the pack."""
        self._check_text(text, fname)
        with self.lock:
            if fname not in {e["path"] for e in self.manifest()["files"]}:
                raise VoiceError(f"{fname} is not in this voice pack")
            self._keep(fname)
            atomic_write(self.path(fname), text)

    def add(self, fname, text, role=None):
        """Add an uploaded file (or replace one of the same name, keeping the old copy).
        Names that differ only in case are the same file: on macOS and Windows they are."""
        fname = clean_filename(fname)
        self._check_text(text, fname)
        with self.lock:
            m = self.manifest()
            names = [e["path"] for e in m["files"]]
            same = next((n for n in names if n.lower() == fname.lower()), None)
            fname = same or fname
            if fname not in names and len(names) >= MAX_FILES:
                raise VoiceError(f"a voice pack holds at most {MAX_FILES} files")
            role = role if role in ROLES else ("sample" if fname not in names else None)
            if fname in names:
                self._keep(fname)
                if role:
                    self._set_role(m, fname, role)
            else:
                if role == "brief" and any(e["role"] == "brief" for e in m["files"]):
                    role = "rules"
                m["files"].append({"path": fname, "role": role, "on": True})
            atomic_write(self.path(fname), text)
            self._save_manifest(m)
        return fname

    @staticmethod
    def _set_role(m, fname, role):
        for e in m["files"]:
            if role == "brief" and e["role"] == "brief" and e["path"] != fname:
                e["role"] = "rules"          # the old brief steps down
            if e["path"] == fname:
                e["role"] = role

    def arrange(self, entries):
        """Set order, roles and on/off from [{path, role, on}]. The set of files must be
        exactly the pack's files: this cannot add or remove one."""
        with self.lock:
            m = self.manifest()
            have = [e["path"] for e in m["files"]]
            if not isinstance(entries, list) or sorted(
                    e.get("path") for e in entries if isinstance(e, dict)) != sorted(have) \
                    or len(entries) != len(have):
                raise VoiceError("the list of files does not match the pack; reload and try again")
            new = []
            for e in entries:
                role = e.get("role")
                if role not in ROLES:
                    raise VoiceError(f"{e.get('path')}: role must be brief, rules or sample")
                new.append({"path": e["path"], "role": role, "on": e.get("on", True) is not False})
            self._save_manifest({"files": new})

    def delete(self, fname):
        with self.lock:
            m = self.manifest()
            if fname not in {e["path"] for e in m["files"]}:
                raise VoiceError(f"{fname} is not in this voice pack")
            self._keep(fname)
            p = self.path(fname)
            if os.path.isfile(p):
                os.remove(p)
            m["files"] = [e for e in m["files"] if e["path"] != fname]
            self._save_manifest(m)


class VoiceLibrary:
    def __init__(self, workspace_root):
        self.root = os.path.join(os.path.abspath(workspace_root), "voices")
        os.makedirs(self.root, exist_ok=True)
        self.lock = threading.Lock()
        self._packs = {}

    def names(self):
        return sorted(n for n in os.listdir(self.root)
                      if NAME_RE.match(n) and os.path.isdir(os.path.join(self.root, n))
                      and not os.path.islink(os.path.join(self.root, n)))

    def get(self, name):
        with self.lock:
            if name not in self.names():
                raise VoiceError(f"no voice pack named {name!r}")
            return self._packs.setdefault(name, VoicePack(self.root, name))

    def create(self, name):
        if not NAME_RE.match(name or ""):
            raise VoiceError("a voice pack name is lowercase letters, digits and dashes")
        with self.lock:
            d = os.path.join(self.root, name)
            if os.path.exists(d):
                raise VoiceError(f"a voice pack named {name} already exists")
            os.makedirs(d)
            atomic_write(os.path.join(d, "writer.md"), STARTER_BRIEF)
            atomic_write(os.path.join(d, "voice.json"), json.dumps(
                {"format": FORMAT, "files": [{"path": "writer.md", "role": "brief", "on": True}]},
                indent=1) + "\n")
            return self._packs.setdefault(name, VoicePack(self.root, name))

    def seed_from_design(self, pack):
        """First run only: copy a folder design's old [voice] list into a voice pack of the
        same name, byte for byte. Returns the pack, or None if there was nothing to do."""
        if not (pack.voice_system or pack.voice_files) or pack.root is None:
            return None
        name = pack.name
        with self.lock:
            d = os.path.join(self.root, name)
            if os.path.exists(d):
                return None
            tmp = d + ".seeding"
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp)
            entries, used = [], set()
            rels = ([(pack.voice_system, "brief")] if pack.voice_system else []) + \
                [(r, "sample" if re.search(r"(^|/)(specimens?|samples?)/", r) else "rules")
                 for r in pack.voice_files]
            for rel, role in rels:
                fname = clean_filename(os.path.basename(rel))
                if fname.lower() in used:          # case-only twins collide on macOS/Windows
                    fname = clean_filename(rel.replace("/", "-"))
                used.add(fname.lower())
                shutil.copyfile(pack.inside(rel), os.path.join(tmp, fname))
                entries.append({"path": fname, "role": role, "on": True})
            atomic_write(os.path.join(tmp, "voice.json"),
                         json.dumps({"format": FORMAT, "files": entries}, indent=1) + "\n")
            os.replace(tmp, d)
        return self.get(name)
