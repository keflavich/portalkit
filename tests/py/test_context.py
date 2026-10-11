"""portalkit.context: directory inputs are stamped without walking or stat'ing the tree."""

from __future__ import annotations

import os

import pytest

from portalkit import context as ctxmod
from portalkit.context import Cache, InputRegistry, PageConfig


def _tree(tmp_path):
    d = tmp_path / "grid"
    (d / "sub" / "deep").mkdir(parents=True)
    for i in range(3):
        (d / f"m{i}.fits").write_bytes(b"x" * (i + 1))
    (d / "sub" / "deep" / "a.txt").write_text("a")
    return d


def test_dir_record_is_a_stamp(tmp_path, monkeypatch):
    d = _tree(tmp_path)
    walked = []
    monkeypatch.setattr(ctxmod.os, "walk", lambda *a, **k: walked.append(a) or iter(()))
    reg = InputRegistry(True, cache_dir=tmp_path / "c")
    rec = reg.add(d)
    assert walked == []
    assert rec["kind"] == "dir" and rec["entries"] == 4 and rec["size"] == 0
    assert len(rec["sha256"]) == 64
    # no digest of a directory goes into the file sha cache
    assert not (tmp_path / "c" / "input_sha256.json").exists()


def test_dir_stamp_does_not_stat_entries(tmp_path, monkeypatch):
    d = _tree(tmp_path)
    seen = []
    real = os.stat

    def spy(p, *a, **k):
        seen.append(os.fspath(p))
        return real(p, *a, **k)
    monkeypatch.setattr(ctxmod.os, "stat", spy)
    ctxmod._dir_stamp(str(d))
    Cache(tmp_path / "c").key_hash("k", [d])
    assert all(os.path.normpath(p) == str(d) for p in seen), seen


def test_dir_stamp_changes_with_entries(tmp_path):
    d = _tree(tmp_path)
    a = ctxmod._dir_stamp(str(d))
    (d / "m9.fits").write_bytes(b"new")
    b = ctxmod._dir_stamp(str(d))
    assert a[0] != b[0] and b[2] == a[2] + 1
    os.rename(d / "m9.fits", d / "m8.fits")
    c = ctxmod._dir_stamp(str(d))
    assert c[0] != b[0] and c[2] == b[2]
    # a nested change leaves the top-level stamp alone (documented trade-off)
    (d / "sub" / "deep" / "b.txt").write_text("b")
    assert ctxmod._dir_stamp(str(d))[0] == c[0]


def test_memo_with_dir_input(tmp_path):
    d = _tree(tmp_path)
    reg = InputRegistry(True, cache_dir=tmp_path / "c")
    cache = Cache(tmp_path / "c", registry=reg)
    calls = []
    fn = lambda: calls.append(1) or [len(calls)]  # noqa: E731
    assert cache.memo("g", fn, inputs=[d]) == [1]
    assert cache.memo("g", fn, inputs=[d]) == [1] and len(calls) == 1
    (d / "m5.fits").write_bytes(b"5")
    assert cache.memo("g", fn, inputs=[d]) == [2]
    assert d.as_posix() in reg


def _old_key_hash(cache, key, inputs):
    """The pre-WPE1 Cache.key_hash, written out independently (tree digest per directory)."""
    import hashlib
    import json
    stats = []
    for p in map(str, inputs):
        if os.path.isdir(p):
            rows, newest, total = [], 0, 0
            for dp, dn, fns in os.walk(p):
                dn.sort()
                for fn in sorted(fns):
                    st = os.stat(os.path.join(dp, fn))
                    rows.append(f"{os.path.relpath(os.path.join(dp, fn), p)}\t{st.st_size}\t{st.st_mtime_ns}\n")
                    newest, total = max(newest, st.st_mtime_ns), total + st.st_size
            h = hashlib.sha256("".join(sorted(rows)).encode())
            stats.append([os.path.abspath(p), newest, total, h.hexdigest()])
        else:
            st = os.stat(p)
            stats.append([os.path.abspath(p), st.st_mtime_ns, st.st_size])
    parts = [key, stats] + ([cache.scope] if cache.scope else [])
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:24]


def test_memo_adopts_value_under_pre_wpe1_key(tmp_path, monkeypatch):
    """A value memoised before the directory-stamp change (old tree-digest key) is reused,
    its files are left untouched, and an alias makes the next lookup walk-free."""
    import numpy as np
    d = _tree(tmp_path)
    f = tmp_path / "zp.ecsv"
    f.write_text("zp")
    cdir = tmp_path / "c"
    cache = Cache(cdir, scope={"tier": "B", "limit": 50})
    old = _old_key_hash(cache, "loop", [d, f])
    assert cache.legacy_key_hash("loop", [d, f]) == old
    assert cache.legacy_key_hash("loop", [f]) is None          # no directory: keys agree
    assert cache.key_hash("loop", [f]) == _old_key_hash(cache, "loop", [f])
    # an entry as the old cache wrote it
    cdir.mkdir()
    np.savez(cdir / f"loop.{old}.npz", a=np.arange(3))
    (cdir / f"loop.{old}.meta.json").write_text(
        '{"kind": "arrays", "file": "loop.%s.npz", "key": "loop"}' % old)
    before = {p.name: p.stat().st_mtime_ns for p in cdir.iterdir()}

    def boom():
        pytest.fail("the old value must be adopted, not recomputed")
    v = cache.memo("loop", boom, inputs=[d, f])
    assert v["a"].tolist() == [0, 1, 2] and cache.hits == 1
    assert {p.name: p.stat().st_mtime_ns for p in cdir.iterdir() if p.name in before} == before
    alias = cdir / f"loop.{cache.key_hash('loop', [d, f])}.meta.json"
    assert alias.is_file() and old in alias.read_text()
    # second lookup: served by the alias, no tree walk
    walked = []
    real_walk = os.walk
    monkeypatch.setattr(ctxmod.os, "walk", lambda *a, **k: walked.append(a) or real_walk(*a, **k))
    assert cache.memo("loop", boom, inputs=[d, f])["a"].tolist() == [0, 1, 2]
    assert walked == []
    monkeypatch.undo()
    # a changed tree matches neither key: recomputed
    (d / "new.fits").write_bytes(b"n")
    assert cache.memo("loop", lambda: {"a": np.arange(2)}, inputs=[d, f])["a"].tolist() == [0, 1]


def _spy_digest(monkeypatch):
    calls = []
    real = ctxmod._legacy_tree_digest
    monkeypatch.setattr(ctxmod, "_legacy_tree_digest", lambda p: calls.append(p) or real(p))
    walks = []
    real_walk = os.walk
    monkeypatch.setattr(ctxmod.os, "walk", lambda *a, **k: walks.append(a) or real_walk(*a, **k))
    return calls, walks


def test_memo_without_legacy_entry_computes(tmp_path, monkeypatch):
    """A cold cache, a new key, and a changed tree whose only entries were written by this
    version never walk the tree."""
    import numpy as np
    d = _tree(tmp_path)
    calls, walks = _spy_digest(monkeypatch)
    cache = Cache(tmp_path / "c")
    assert cache.memo("k", lambda: {"v": 1}, inputs=[d]) == {"v": 1}
    assert cache.misses == 1 and cache.hits == 0
    assert len(list((tmp_path / "c").glob("k.*.meta.json"))) == 1
    assert cache.memo("k2", lambda: {"a": np.arange(2)}, inputs=[d])["a"].tolist() == [0, 1]
    (d / "new.fits").write_bytes(b"n")
    c2 = Cache(tmp_path / "c")
    assert c2.memo("k", lambda: {"v": 2}, inputs=[d]) == {"v": 2}
    assert calls == [] and walks == []


def test_failed_adoption_is_not_repeated(tmp_path, monkeypatch):
    """A pre-WPE1 entry that does not match is walked against once per scope and input
    paths; later misses (a tree that keeps changing) skip the walk, another scope does not."""
    import numpy as np
    d = _tree(tmp_path)
    cdir = tmp_path / "c"
    cdir.mkdir()
    stale = "0" * 24
    np.savez(cdir / f"loop.{stale}.npz", a=np.arange(3))
    (cdir / f"loop.{stale}.meta.json").write_text(
        '{"kind": "arrays", "file": "loop.%s.npz", "key": "loop"}' % stale)
    calls, _ = _spy_digest(monkeypatch)
    scope = {"tier": "B", "limit": None}
    assert Cache(cdir, scope=scope).memo("loop", lambda: [1], inputs=[d]) == [1]
    assert len(calls) == 1
    assert (cdir / "loop.legacy-checked.json").is_file()
    (d / "new.fits").write_bytes(b"n")
    assert Cache(cdir, scope=scope).memo("loop", lambda: [2], inputs=[d]) == [2]
    assert len(calls) == 1                       # already checked for this scope
    assert Cache(cdir, scope={"tier": "A", "limit": 5}).memo("loop", lambda: [3], inputs=[d]) == [3]
    assert len(calls) == 2                       # a fixture scope checks once on its own
    assert (cdir / f"loop.{stale}.meta.json").is_file()   # old entries are never removed


def test_missing_input(tmp_path):
    reg = InputRegistry(True)
    with pytest.raises(FileNotFoundError, match="does not exist"):
        reg.add(tmp_path / "nope")


def test_page_config_legacy_forms():
    pc = PageConfig(id="g", files=["a.html", {"file": "b.html", "preset": "role=x"},
                                   {"file": "c.html", "replace": False}])
    assert pc.legacy == ["a.html", "b.html", "c.html"]
    assert pc.presets() == {"b.html": "role=x"}
    assert [f.replace for f in pc.files] == [True, True, False]
    assert PageConfig(id="h", legacy=["x.html"]).files[0].file == "x.html"
