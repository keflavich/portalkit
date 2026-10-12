"""The hand-edited archive checklist is never written (WP28; ARCHITECTURE.md 14.3).

``mastDownload/JWST/jwst_archive_spectra_checklist.csv`` is classified by hand. The
jwst_archive adapter takes the checklist codes from the fluxes table (``ISOKN``), and the
checklist page only exports annotations; ``portalkit archive-checklist-diff`` prints a
unified diff for a person to apply. This module enforces that in three ways:

* static: the adapter source holds no file-writing call and no checklist path literal;
* runtime: every open / os-level write to the checklist path (or a hard link to it) is refused
  and recorded while the adapter runs (datasets, pages, record loaders), while the CLI diff
  runs, and (opt-in ``adapters`` group) during a fixture build;
* content: the checklist's sha256 and mtime are unchanged afterwards.
"""

from __future__ import annotations

import ast
import builtins
import csv
import hashlib
import io
import os
import pathlib
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ADAPTER = REPO / "adapters" / "jwst_archive.py"
CHECKLIST = Path("/orange/adamginsburg/jwst/spectra/mastDownload/JWST/jwst_archive_spectra_checklist.csv")
KEYS = ("Target", "Program", "Object", "SlitID", "Observation", "Visit", "VisitGroup", "Filename")

needs_data = pytest.mark.skipif(not CHECKLIST.is_file(), reason="JWST archive checklist not mounted")

#: call names that write files whatever the receiver
WRITE_CALLS = {"write", "writelines", "write_text", "write_bytes", "writeto", "to_csv", "savetxt", "save", "savez",
               "dump", "touch", "mkstemp", "NamedTemporaryFile", "copyfile", "copy2", "rmtree"}
#: names that write only on a filesystem module (``os.replace``; ``str.replace`` is fine)
FS_CALLS = {"replace", "rename", "remove", "unlink", "truncate", "copy", "move", "link", "symlink", "utime"}
FS_MODULES = {"os", "shutil", "pathlib", "Path"}


def _state(p: Path) -> tuple[str, int, int]:
    st = p.stat()
    return hashlib.sha256(p.read_bytes()).hexdigest(), st.st_mtime_ns, st.st_size


def _write_mode(mode) -> bool:
    return any(c in str(mode) for c in "wax+")


class ChecklistGuard:
    """Monkeypatches file-writing entry points; refuses (PermissionError) and records any
    write-capable access to the checklist."""

    def __init__(self, mp: pytest.MonkeyPatch, target: Path = CHECKLIST):
        self.mp = mp
        self.target = os.path.realpath(target)
        st = os.stat(self.target) if os.path.exists(self.target) else None
        self.ino = (st.st_dev, st.st_ino) if st else None
        self.hits: list[str] = []

    def _is_target(self, p) -> bool:
        if isinstance(p, int):
            return False
        try:
            fs = os.fspath(p)
        except TypeError:
            return False
        if isinstance(fs, bytes):
            fs = fs.decode(errors="replace")
        if os.path.realpath(fs) == self.target:
            return True
        if self.ino and os.path.exists(fs):
            st = os.stat(fs)
            return (st.st_dev, st.st_ino) == self.ino
        return False

    def _refuse(self, what: str, p) -> None:
        self.hits.append(f"{what}: {p}")
        raise PermissionError(f"refused {what} on the hand-edited checklist {p}")

    def install(self) -> "ChecklistGuard":
        g, mp = self, self.mp
        o_open, o_io_open, o_os_open, o_path_open = builtins.open, io.open, os.open, pathlib.Path.open

        def b_open(file, mode="r", *a, **k):
            if _write_mode(mode) and g._is_target(file):
                g._refuse("open(%s)" % mode, file)
            return o_open(file, mode, *a, **k)

        def i_open(file, mode="r", *a, **k):
            if _write_mode(mode) and g._is_target(file):
                g._refuse("io.open(%s)" % mode, file)
            return o_io_open(file, mode, *a, **k)

        wflags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

        def x_open(path, flags, *a, **k):
            if flags & wflags and g._is_target(path):
                g._refuse("os.open", path)
            return o_os_open(path, flags, *a, **k)

        def p_open(self, mode="r", *a, **k):
            if _write_mode(mode) and g._is_target(self):
                g._refuse("Path.open(%s)" % mode, self)
            return o_path_open(self, mode, *a, **k)

        mp.setattr(builtins, "open", b_open)
        mp.setattr(io, "open", i_open)
        mp.setattr(os, "open", x_open)
        mp.setattr(pathlib.Path, "open", p_open)

        def two(name, orig):
            def f(src, dst, *a, **k):
                if g._is_target(src) or g._is_target(dst):
                    g._refuse(name, dst)
                return orig(src, dst, *a, **k)
            return f

        def one(name, orig):
            def f(path, *a, **k):
                if g._is_target(path):
                    g._refuse(name, path)
                return orig(path, *a, **k)
            return f
        for name in ("replace", "rename", "link", "symlink"):
            mp.setattr(os, name, two(f"os.{name}", getattr(os, name)))
        for name in ("remove", "unlink", "truncate", "utime", "chmod"):
            mp.setattr(os, name, one(f"os.{name}", getattr(os, name)))
        for name in ("copy", "copy2", "copyfile", "move"):
            mp.setattr(shutil, name, two(f"shutil.{name}", getattr(shutil, name)))
        return self


@pytest.fixture
def guard():
    mp = pytest.MonkeyPatch()
    before = _state(CHECKLIST) if CHECKLIST.is_file() else None
    g = ChecklistGuard(mp).install()
    try:
        yield g
    finally:
        mp.undo()
    assert not g.hits, g.hits
    if before is not None:
        assert _state(CHECKLIST) == before, "checklist content or mtime changed"


def _adapter():
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from adapters import jwst_archive
    return jwst_archive


# -- static ------------------------------------------------------------------------------------
def scan_source(src: str) -> list[str]:
    """File-writing calls and checklist path literals (outside docstrings) in ``src``."""
    tree = ast.parse(src)
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            recv = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else None
            if name in WRITE_CALLS or (name in FS_CALLS and (recv in FS_MODULES or isinstance(f, ast.Name))):
                bad.append(f"line {n.lineno}: {name}()")
            if name == "open":
                mode = n.args[1] if len(n.args) > 1 else next((k.value for k in n.keywords if k.arg == "mode"), None)
                if mode is not None and not (isinstance(mode, ast.Constant) and not _write_mode(mode.value)):
                    bad.append(f"line {n.lineno}: open(mode={ast.unparse(mode)})")
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            if "checklist.csv" in n.value:
                bad.append(f"line {n.lineno}: checklist path literal")
    return bad


def test_adapter_source_has_no_write_calls():
    assert scan_source(ADAPTER.read_text(encoding="utf-8")) == []


def test_static_scan_detects_writes():
    """The scan flags write calls and checklist literals, and leaves str.replace alone."""
    src = ("import os, shutil\nP = '/x/jwst_archive_spectra_checklist.csv'\n"
           "def f(t, mode):\n    t.write(P, overwrite=True)\n    open(P, 'w')\n    open(P, mode)\n"
           "    os.remove(P)\n    shutil.move(P, P)\n    return 'a_b'.replace('_', ' '), open(P).read()\n")
    bad = scan_source(src)
    assert len(bad) == 6, bad
    assert scan_source("def g(s):\n    \"\"\"checklist.csv is read-only\"\"\"\n    return s.replace('a', 'b')\n") == []
    assert _write_mode("w") and _write_mode("r+") and _write_mode("ab") and not _write_mode("rb")


def test_adapter_reads_codes_from_the_fluxes_table():
    mod = _adapter()
    assert "CHECKLIST" not in vars(mod) and mod.TABLE.name == "jwst_archive_spectra_as_fluxes.ecsv"


# -- runtime -----------------------------------------------------------------------------------
def test_guard_refuses_checklist_writes(tmp_path):
    """The guard refuses writes to its target (and hard links to it) and lets reads through."""
    target = tmp_path / "check.csv"
    target.write_text("a,b\n1,2\n", encoding="utf-8")
    os.link(target, tmp_path / "hard.csv")
    mp = pytest.MonkeyPatch()
    g = ChecklistGuard(mp, target).install()
    try:
        with open(target, encoding="utf-8") as fh:
            assert fh.read().startswith("a,b")
        for fn in (lambda: open(target, "w"), lambda: io.open(target, "a"), lambda: target.write_text("x"),
                   lambda: os.open(target, os.O_WRONLY), lambda: os.replace(tmp_path / "hard.csv", target),
                   lambda: os.remove(target), lambda: open(tmp_path / "hard.csv", "r+"),
                   lambda: shutil.copy(tmp_path / "hard.csv", target)):
            with pytest.raises(PermissionError):
                fn()
        with open(tmp_path / "other.csv", "w", encoding="utf-8") as fh:
            fh.write("ok")
    finally:
        mp.undo()
    assert len(g.hits) == 8
    assert target.read_text(encoding="utf-8") == "a,b\n1,2\n"


@needs_data
def test_adapter_run_never_writes_checklist(guard, tmp_path, monkeypatch):
    from portalkit.context import BuildContext, load_site
    mod = _adapter()
    if not mod.TABLE.is_file():
        pytest.skip("fluxes table not mounted")
    # a private memo cache: the Level-0 suite must not write ~/.cache/portalkit
    monkeypatch.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    site = load_site("jwst_archive")
    for tier in ("A", "B"):
        ctx = BuildContext(family="jwst_archive", site=site, staging=tmp_path / tier, tier=tier, limit=3,
                           log=lambda m: None)
        ds = mod.ADAPTER.datasets(ctx)
        mod.ADAPTER.pages(ctx, ds)
        for i in ds["spec"].ids:
            ds["spec"].loader(i)
        recs = [r["path"] for r in ctx.inputs.records()]
        assert str(CHECKLIST) not in recs


def _export(tmp_path, rows: list[dict]) -> Path:
    """An annotation export as the checklist gallery writes it (id, keys, ISOKN)."""
    p = tmp_path / "export.csv"
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", *KEYS, "ISOKN"])
        for r in rows:
            w.writerow([r["id"], *(r[k] for k in KEYS), r["ISOKN"]])
    return p


@needs_data
def test_checklist_diff_cli_reads_only(guard, tmp_path, capsys):
    from portalkit import cli
    with open(CHECKLIST, encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["ISOKN"] in ("y", "n")][:3]
    assert rows
    flip = [{**r, "id": f"s{i}", "ISOKN": "n" if r["ISOKN"] == "y" else "y"} for i, r in enumerate(rows)]
    rc = cli.main(["archive-checklist-diff", str(_export(tmp_path, flip))])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.startswith("---") and out.out.count("\n+") >= len(rows) + 1
    assert "3 checklist row(s) change" in out.err


@pytest.mark.adapters
@needs_data
def test_fixture_build_never_writes_checklist(guard, tmp_path):
    from portalkit.build import build_family
    from portalkit.deploy import ExposureMap
    mp = pytest.MonkeyPatch()
    mp.setenv("PK_STAGE_ROOT", str(tmp_path))
    mp.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    try:
        res = build_family("jwst_archive", limit=3, stage_root=tmp_path, log=lambda m: None,
                           emap=ExposureMap(roots={}, url_prefixes={}))
    finally:
        mp.undo()
    assert res.ok, res.report.get("errors")
    served = [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix in (".json", ".html")
              and p.name != "build_report.json"]
    assert served and not any("checklist.csv" in p.read_text(encoding="utf-8", errors="replace") for p in served)
