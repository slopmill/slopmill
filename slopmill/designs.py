# SPDX-License-Identifier: MIT
"""The designs an editor can use: the ones shipped as folders, and ones people upload.

    packs/NAME/                      shipped (the default design is one of these, or any folder)
    WORKSPACE/designs/NAME.design.toml   uploaded
    WORKSPACE/designs/.history/      an uploaded design as it was before it was replaced

An upload is checked in a separate process with a time limit before it is stored: the file
must load and must compile a sample issue that uses every block and component it declares.
A template that loops forever or fills memory is refused there, not in the editor.
"""
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

from . import render
from .errors import CompileError, EnvironmentProblem
from .ids import atomic_write
from .pack import NAME_RE, PACKS_DIR, design_from_text, export_design, load_folder
from .voices import VoiceError, history_dir

CHECK_SECONDS = 20
CHECK_MEMORY = 1 << 30         # 1 GiB of address space for the check process


def _limit_child():
    """In the child, before it runs: a design that builds a huge value runs out of its own
    memory, not the editor's."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (CHECK_MEMORY, CHECK_MEMORY))
    except (ImportError, ValueError, OSError):
        pass


class DesignError(Exception):
    pass


def sample_issue(pack):
    """Source text for an issue that exercises everything the design declares."""
    meta = []
    for f in pack.meta_fields:
        meta.append(f"{f}: {'1' if pack.meta_types.get(f) == 'int' else 'Sample ' + f}")
    blocks = []
    accents = " ".join(f"**{n} words**{{.{n}}}" for n in pack.accents)
    code = " `code`" if "code" in pack.inline else ""
    blocks.append(f"A paragraph with [a link](https://example.com), **strong**, *emphasis*"
                  f"{code} and {accents or 'no accents'}.")
    for level in sorted(pack.heading_templates):
        blocks.append("#" * level + " A heading")
    if pack.block_templates.get("quote"):
        blocks.append("> A quoted line.")
    if pack.block_templates.get("list"):
        blocks.append("- one\n- two")
        blocks.append("1. first\n2. second")
    for comp in pack.components.values():
        attrs = " ".join(f'{k}="Sample"' for k in comp.attrs)
        head = f"::: {{.{comp.name}{(' ' + attrs) if attrs else ''}}}"
        if comp.shape == "figure":
            blocks.append(f"{head}\n![Alt text](sample.jpg)\n\nA caption.\n:::")
            blocks.append(f"{head}\n![Alt text](sample.jpg)\n:::")
            blocks.append(f"{head}\n![A video](sample.mp4){{poster=sample.jpg}}\n\nA caption.\n:::")
        else:
            n = max(comp.min_paragraphs, min(2, comp.max_paragraphs))
            paras = "\n\n".join(f"Paragraph {i + 1} with **strong** text." for i in range(n))
            blocks.append(f"{head}\n{paras}\n:::" if n else f"{head}\n:::")
    body = "\n\n".join(f"{{#b-s{i:03d}}}\n{b}" if not b.startswith(":::") else
                       b.replace("}", f" #b-s{i:03d}}}", 1) for i, b in enumerate(blocks))
    return "---\n" + "\n".join(meta) + "\n---\n\n" + body + "\n"


def check_design_text(data, origin="design file"):
    """Load and compile the sample in THIS process. Returns the Pack or raises DesignError.
    Called by `slopmill design check`, which the server runs in a child process."""
    try:
        pack = design_from_text(data, origin=origin, trusted=False)
    except EnvironmentProblem as e:
        raise DesignError(str(e))
    text = sample_issue(pack)
    try:
        result = render.compile_text(text, pack, path="sample.md", mode="publish",
                                     check_assets=False)
    except CompileError as e:
        raise DesignError("the sample issue does not compile through it:\n"
                          + "\n".join(f"  - {p}" for p in e.problems))
    except EnvironmentProblem as e:
        raise DesignError(str(e))
    except Exception as e:           # a template error of any kind: report it, do not store
        raise DesignError(f"a template failed: {type(e).__name__}: {e}")
    if not result.body.strip():
        raise DesignError("the sample issue compiled to nothing")
    return pack


class DesignLibrary:
    def __init__(self, workspace_root, default_pack, builtin_dir=PACKS_DIR):
        self.root = os.path.join(os.path.abspath(workspace_root), "designs")
        os.makedirs(self.root, exist_ok=True)
        self.default = default_pack
        self.builtin_dir = builtin_dir
        self.lock = threading.Lock()
        self._cache = {}

    def _builtin(self):
        out = {self.default.name: self.default}
        if os.path.isdir(self.builtin_dir):
            for n in sorted(os.listdir(self.builtin_dir)):
                d = os.path.join(self.builtin_dir, n)
                if n not in out and NAME_RE.match(n) and os.path.isfile(os.path.join(d, "pack.toml")):
                    out[n] = d
        return out

    def _uploaded(self):
        out = {}
        for fn in sorted(os.listdir(self.root)):
            m = re.match(r"^([a-z0-9][a-z0-9-]{0,39})\.design\.toml$", fn)
            p = os.path.join(self.root, fn)
            if m and os.path.isfile(p) and not os.path.islink(p):
                out[m.group(1)] = p
        return out

    def list(self):
        built, up = self._builtin(), self._uploaded()
        return ([{"name": n, "source": "built-in", "default": n == self.default.name} for n in built]
                + [{"name": n, "source": "uploaded", "default": False} for n in up if n not in built])

    def get(self, name):
        if not name or name == self.default.name:
            return self.default
        built = self._builtin()
        if name in built:
            src, loader, trusted = built[name], load_folder, True
        else:
            up = self._uploaded()
            if name not in up:
                raise DesignError(f"no design named {name!r}")
            src, trusted = up[name], False
            loader = None
        st = os.stat(src) if isinstance(src, str) and os.path.isfile(src) else None
        key = (name, src, (st.st_mtime_ns, st.st_size, st.st_ino) if st else 0)
        with self.lock:
            if key in self._cache:
                return self._cache[key]
        try:
            if loader:
                pack = loader(src)
            else:
                with open(src, "rb") as f:
                    pack = design_from_text(f.read(), origin=os.path.basename(src), trusted=trusted)
        except EnvironmentProblem as e:
            raise DesignError(str(e))
        with self.lock:
            self._cache = {k: v for k, v in self._cache.items() if k[0] != name}
            self._cache[key] = pack
        return pack

    def export(self, name):
        return export_design(self.get(name))

    def upload(self, data):
        """Validate in a child process, then store. Returns the design's name."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        with tempfile.NamedTemporaryFile("wb", suffix=".design.toml", dir=self.root,
                                         prefix=".upload-", delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL")}
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env["PYTHONPATH"] = root
            try:
                proc = subprocess.run([sys.executable, "-m", "slopmill", "design", "check", tmp],
                                      capture_output=True, text=True, timeout=CHECK_SECONDS,
                                      env=env, cwd=root,
                                      # preexec_fn exists only on POSIX; elsewhere the time
                                      # limit alone applies
                                      **({"preexec_fn": _limit_child} if os.name == "posix" else {}))
            except subprocess.TimeoutExpired:
                raise DesignError(f"checking the design took over {CHECK_SECONDS}s "
                                  "(a template that never finishes?)")
            if proc.returncode != 0:
                msg = (proc.stderr or proc.stdout or "").strip()
                raise DesignError(msg.removeprefix("✗ ")[:2000] or "the design did not load")
            name = proc.stdout.strip().splitlines()[-1].removeprefix("✓ ").split()[0]
            if not NAME_RE.match(name):
                raise DesignError("the design did not report a usable name")
            if name in self._builtin():
                raise DesignError(f"{name} is the name of a design that ships with slopmill; "
                                  f"change name = \"...\" under [pack] and upload it again")
            dest = os.path.join(self.root, f"{name}.design.toml")
            with self.lock:
                if os.path.isfile(dest):
                    hdir = self._history()
                    os.replace(dest, os.path.join(
                        hdir, f"{name}.{time.strftime('%Y%m%d-%H%M%S')}.design.toml"))
                atomic_write(dest, data.decode("utf-8"))
                # Never serve the replaced version from memory, whatever the file times say.
                self._cache = {k: v for k, v in self._cache.items() if k[0] != name}
            return name
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def delete(self, name):
        up = self._uploaded()
        if name not in up or name in self._builtin():
            raise DesignError("only an uploaded design can be removed")
        os.replace(up[name], os.path.join(self._history(),
                                          f"{name}.{time.strftime('%Y%m%d-%H%M%S')}.design.toml"))

    def _history(self):
        try:
            return history_dir(self.root)
        except VoiceError as e:
            raise DesignError(str(e).replace("pack", "designs folder"))
