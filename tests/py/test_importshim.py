"""portalkit.legacy.importshim: tracked imports of legacy generator modules."""

import hashlib
import os
import sys
import uuid

import pytest

from portalkit.legacy.importshim import LegacyImporter, LegacyImportError, sha256_file


@pytest.fixture
def legacy(tmp_path):
    """A legacy tree with uniquely named modules; sys.path/sys.modules restored afterwards."""
    tag = uuid.uuid4().hex[:8]
    d = tmp_path / "legacy"
    d.mkdir()
    names = {k: f"pk_legacy_{k}_{tag}" for k in ("good", "helper", "writer", "broken", "pkg")}
    (d / f"{names['helper']}.py").write_text("VALUE = 41\n")
    (d / f"{names['good']}.py").write_text(
        f"import {names['helper']} as _h\n"
        "def load():\n    return _h.VALUE + 1\n"
        "if __name__ == '__main__':\n    open('out.txt', 'w').write('x')\n")
    (d / f"{names['writer']}.py").write_text("open('side_effect.txt', 'w').write('x')\n")
    (d / f"{names['broken']}.py").write_text("def f(:\n")
    pkg = d / names["pkg"]
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "sub.py").write_text("X = 'sub'\n")
    saved_path = list(sys.path)
    saved_mods = set(sys.modules)
    cwd = os.getcwd()
    yield d, names
    sys.path[:] = saved_path
    for k in set(sys.modules) - saved_mods:
        sys.modules.pop(k, None)
    assert os.getcwd() == cwd


def test_sha256_file(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc" * 1000)
    assert sha256_file(p, chunk=7) == hashlib.sha256(b"abc" * 1000).hexdigest()


def test_module_records_shas(legacy):
    d, names = legacy
    logs = []
    imp = LegacyImporter([d], log=logs.append)
    mod = imp.module(names["good"])
    assert mod.load() == 42
    shas = {r["name"]: r for r in imp.shas()}
    assert set(shas) == {names["good"], names["helper"]}       # sibling import recorded
    assert shas[names["good"]]["sha256"] == sha256_file(d / f"{names['good']}.py")
    assert shas[names["good"]]["path"] == str(d / f"{names['good']}.py")
    assert logs and names["good"] in logs[0]
    # a second request returns the cached module and keeps one record
    assert imp.module(names["good"]) is mod
    assert len(imp.shas()) == 2


def test_dotted_and_attr(legacy):
    d, names = legacy
    imp = LegacyImporter([d])
    assert imp.attr(f"{names['pkg']}.sub", "X") == "sub"
    with pytest.raises(LegacyImportError, match="no attribute"):
        imp.attr(f"{names['pkg']}.sub", "missing")


def test_import_creating_files_is_rejected(legacy):
    d, names = legacy
    imp = LegacyImporter([d])
    with pytest.raises(LegacyImportError, match="created files"):
        imp.module(names["writer"])
    assert names["writer"] not in sys.modules
    assert not (d / "side_effect.txt").exists()


def test_import_errors(legacy):
    d, names = legacy
    imp = LegacyImporter([d])
    with pytest.raises(LegacyImportError, match="SyntaxError"):
        imp.module(names["broken"])
    with pytest.raises(LegacyImportError, match="ModuleNotFoundError"):
        imp.module("pk_no_such_module_" + uuid.uuid4().hex)
    with pytest.raises(LegacyImportError):
        imp.module("")
    assert LegacyImportError.exit_code == 6
    assert issubclass(LegacyImportError, ImportError)


def test_module_outside_legacy_paths(legacy):
    d, _ = legacy
    imp = LegacyImporter([d])
    with pytest.raises(LegacyImportError, match="outside legacy_paths"):
        imp.module("json")                  # already imported from the stdlib
    assert imp.shas() == []


def test_no_legacy_paths_records_anything(legacy):
    d, names = legacy
    sys.path.insert(0, str(d))
    imp = LegacyImporter()
    assert imp.module(names["helper"]).VALUE == 41
    assert [r["name"] for r in imp.shas()] == [names["helper"]]
