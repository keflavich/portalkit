"""Deploy: ownership manifest, safe writes, exposure refusals, gc, htaccess, stage, status.

Everything runs on fake trees under pytest's tmp_path; the exposure map is scanned from fake
``web/public`` and ``web/secure`` roots, so no real served directory is touched.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import os
from pathlib import Path

import pytest

from portalkit import schema
from portalkit.deploy import (BlockedInput, DeployError, ExposureRefusal, OwnershipConflict,
                              SymlinkRefusal, UsageError)
dmod = importlib.import_module("portalkit.deploy.deploy")
hmod = importlib.import_module("portalkit.deploy.htaccess")
smod = importlib.import_module("portalkit.deploy.stage")
stmod = importlib.import_module("portalkit.deploy.status")
from portalkit.deploy.exposure import scan

FAM = "fam"
BID = "20261010T120000Z-0123abcd"
BID2 = "20261011T120000Z-89abcdef"
SEC = "https://data.rc.ufl.edu/secure/adamginsburg/"


def page_html(extra: dict | None = None, body: str = "") -> str:
    page = {"assets": "./assets/", "datasets": {"src": {"url": "data/src/manifest.json"}}}
    page.update(extra or {})
    return ('<!doctype html><title>map</title>\n<script id="pk-page" type="application/json">'
            + json.dumps(page) + "</script>\n<link rel=\"stylesheet\" href=\"assets/pk.css\">\n"
            + body + "\n")


def stage_portal(root: Path, bid: str = BID, pid: str = "main", files: dict | None = None,
                 report: dict | None = None, binname: str = "cols.0123abcd.bin") -> Path:
    d = root / FAM / bid / pid
    content = {
        "portal.json": json.dumps({"pages": [{"id": "map", "file": "map.html"}]}),
        "map.html": page_html(),
        "data/src/manifest.json": json.dumps({"columns": [{"name": "ra", "file": binname}]}),
        f"data/src/{binname}": bytes(range(64)) + bid.encode(),
        "assets/pk.css": "body{}\n",
    }
    content.update(files or {})
    for rel, data in content.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            p.write_bytes(data)
        else:
            p.write_text(data, encoding="utf-8")
    (d / "build_report.json").write_text(json.dumps(report or {
        "provenance": {"adapter_sha": "abc123", "portalkit": "0.1.0",
                       "legacy_modules": [], "inputs": []}}))
    return d


@pytest.fixture()
def world(tmp_path: Path) -> dict:
    o = tmp_path / "orange"
    for sub in ("sec/portal", "pub", "both", "hidden"):
        (o / sub).mkdir(parents=True)
    pub = tmp_path / "web" / "public"
    sec = tmp_path / "web" / "secure"
    pub.mkdir(parents=True)
    sec.mkdir(parents=True)
    os.symlink(o / "sec", sec / "sec")
    os.symlink(o / "pub", pub / "pub")
    os.symlink(o / "both", pub / "both")
    os.symlink(o / "both", sec / "both")
    emap = scan({"public": pub, "secure": sec}, cache_s=0)
    stage = tmp_path / "stage"
    stage.mkdir()
    return {"base": tmp_path, "orange": o, "emap": emap, "stage": stage}


def site(world: dict, vis: str = "secure", where: str = "sec/portal", *, pid: str = "main",
         served_by: str | None = None, pages: list | None = None, target: str | None = None) -> dict:
    p = {"id": pid, "visibility": vis, "legacy_dir": str(world["orange"] / where),
         "page": pages if pages is not None else [{"id": "map", "legacy": []}]}
    if served_by:
        p["served_by"] = served_by
    if target:
        p["target_dir"] = target
    return {"family": FAM, "portal": [p]}


def run(world: dict, s: dict, bid: str = BID, **kw):
    kw.setdefault("emap", world["emap"])
    return dmod.deploy(FAM, bid, s, stage_root=world["stage"], **kw)


def tgt(world: dict, where: str = "sec/portal") -> Path:
    return world["orange"] / where / "unified"


def leftovers(root: Path) -> list[str]:
    return [str(p) for p in root.rglob("*" + dmod.TMP_SUFFIX)]


# ---------------------------------------------------------------------------
# core deploy
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(world):
    stage_portal(world["stage"])
    lines = []
    res = run(world, site(world), dry_run=True, log=lines.append)
    assert res.dry_run and res.written == []
    assert not tgt(world).exists()
    assert {f.action for f in res.plan.files} == {"create"}
    assert "build_report.json" not in [f.rel for f in res.plan.files]
    assert any("5 new" in ln for ln in lines)


def test_deploy_writes_files_and_valid_manifest(world):
    src = stage_portal(world["stage"])
    res = run(world, site(world))
    t = tgt(world)
    assert sorted(res.written) == sorted(["portal.json", "map.html", "data/src/manifest.json",
                                          "data/src/cols.0123abcd.bin", "assets/pk.css"])
    for rel in res.written:
        assert (t / rel).read_bytes() == (src / rel).read_bytes()
    assert not (t / "build_report.json").exists()
    man = json.loads((t / dmod.MANIFEST_NAME).read_text())
    assert schema.validate_deploy(man) == []
    assert man["family"] == FAM and man["portal"] == "main"
    assert man["builds"][-1]["build_id"] == BID
    assert "note" not in man["builds"][-1]
    assert man["builds"][-1]["adapter_sha"] == "abc123"
    assert man["files"]["map.html"]["sha256"] == dmod.sha256_file(src / "map.html")[0]
    assert leftovers(t) == []
    assert dmod.deployed_builds(man) == [BID]


def test_write_order_and_fsync(world, monkeypatch):
    stage_portal(world["stage"])
    order: list[str] = []
    fsyncs = []
    real_replace, real_fsync = os.replace, os.fsync

    def rec_replace(a, b, *args, **kw):
        assert str(a).endswith(dmod.TMP_SUFFIX)
        order.append(os.path.relpath(b, tgt(world)))
        return real_replace(a, b, *args, **kw)

    def rec_fsync(fd):
        fsyncs.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "replace", rec_replace)
    monkeypatch.setattr(os, "fsync", rec_fsync)
    run(world, site(world))
    m = dmod.MANIFEST_NAME
    assert order[0] == m and order[-1] == m          # pre-registration first, final last
    body = order[1:-1]
    assert body.index("data/src/cols.0123abcd.bin") < body.index("data/src/manifest.json")
    assert body.index("data/src/manifest.json") < body.index("portal.json")
    assert body.index("portal.json") < body.index("map.html")
    assert len(fsyncs) >= len(order)


def test_redeploy_same_build_is_noop(world):
    stage_portal(world["stage"])
    run(world, site(world))
    res = run(world, site(world))
    assert res.written == []
    assert {f.action for f in res.plan.files} == {"same"}
    man = json.loads((tgt(world) / dmod.MANIFEST_NAME).read_text())
    assert [b["build_id"] for b in man["builds"]] == [BID]


def test_new_build_updates_and_keeps_old_hashed_file(world):
    stage_portal(world["stage"])
    run(world, site(world))
    stage_portal(world["stage"], BID2, binname="cols.89abcdef.bin",
                 files={"map.html": page_html({"title": "v2"})})
    res = run(world, site(world), BID2)
    acts = {f.rel: f.action for f in res.plan.files}
    assert acts["map.html"] == "update"
    assert acts["assets/pk.css"] == "same"
    assert acts["data/src/cols.89abcdef.bin"] == "create"
    t = tgt(world)
    assert (t / "data/src/cols.0123abcd.bin").exists()     # removed only by --gc
    man = res.manifest
    assert man["files"]["data/src/cols.0123abcd.bin"]["build_id"] == BID
    assert man["files"]["map.html"]["build_id"] == BID2
    assert dmod.deployed_builds(man) == [BID, BID2]


def test_owned_file_edited_is_overwritten_with_warning(world):
    stage_portal(world["stage"])
    run(world, site(world))
    (tgt(world) / "map.html").write_text("edited by hand")
    logs = []
    res = run(world, site(world), log=logs.append)
    assert "map.html" in res.written
    assert any("changed since the last deploy" in ln for ln in logs)


# ---------------------------------------------------------------------------
# ownership and symlink refusals
# ---------------------------------------------------------------------------

def test_foreign_file_is_not_clobbered(world):
    stage_portal(world["stage"])
    t = tgt(world)
    t.mkdir()
    (t / "map.html").write_text("someone else's page")
    with pytest.raises(OwnershipConflict) as ei:
        run(world, site(world))
    assert ei.value.exit_code == 4
    assert "map.html" in ei.value.conflicts
    assert (t / "map.html").read_text() == "someone else's page"
    assert not (t / dmod.MANIFEST_NAME).exists()
    with pytest.raises(OwnershipConflict):          # the dry run refuses the same way
        run(world, site(world), dry_run=True)


def test_symlink_component_refused(world):
    stage_portal(world["stage"])
    t = tgt(world)
    t.mkdir()
    elsewhere = world["orange"] / "hidden" / "data"
    elsewhere.mkdir()
    os.symlink(elsewhere, t / "data")
    with pytest.raises(SymlinkRefusal) as ei:
        run(world, site(world))
    assert ei.value.exit_code == 3
    assert list(elsewhere.iterdir()) == []


def test_symlink_target_refused(world):
    stage_portal(world["stage"])
    real = world["orange"] / "sec" / "portal" / "realdir"
    real.mkdir()
    os.symlink(real, tgt(world))
    with pytest.raises(SymlinkRefusal):
        run(world, site(world))


def test_planted_tmp_symlink_refused(world, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    d = tmp_path / "w"
    d.mkdir()
    os.symlink(victim, d / ("f.txt" + dmod.TMP_SUFFIX))
    with pytest.raises(SymlinkRefusal):
        dmod.atomic_write(d / "f.txt", b"data")
    assert victim.read_text() == "keep"


def test_staged_symlink_and_reserved_names(world):
    d = stage_portal(world["stage"])
    os.symlink(d / "map.html", d / "alias.html")
    (d / ".htaccess").write_text("x")
    with pytest.raises(OwnershipConflict) as ei:
        run(world, site(world))
    text = " ".join(ei.value.conflicts)
    assert "alias.html" in text and ".htaccess" in text


def test_invalid_manifest_is_conflict(world):
    stage_portal(world["stage"])
    t = tgt(world)
    t.mkdir()
    (t / dmod.MANIFEST_NAME).write_text(json.dumps({"schema": "pk-deploy/1", "files": 3}))
    with pytest.raises(OwnershipConflict):
        run(world, site(world))


def test_secondary_manifest_for_second_portal(world):
    t = world["orange"] / "both" / "shared"
    stage_portal(world["stage"], pid="a")
    stage_portal(world["stage"], pid="b", files={"portal.json": "{}"})
    sa = site(world, "public", "both", pid="a", target=str(t))
    sb = site(world, "public", "both", pid="b", target=str(t))
    run(world, sa)
    with pytest.raises(OwnershipConflict) as ei:      # b would overwrite a's files
        run(world, sb)
    assert "map.html" in ei.value.conflicts
    d = world["stage"] / FAM / BID / "c"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "c.html").write_text("<p>c</p>")
    sc = site(world, "public", "both", pid="c", target=str(t))
    res = run(world, sc)
    assert res.plan.manifest_path.name == ".portalkit-deploy.c.json"
    assert json.loads((t / dmod.MANIFEST_NAME).read_text())["portal"] == "a"
    assert dmod.manifest_path_for(t, FAM, "a").name == dmod.MANIFEST_NAME


# ---------------------------------------------------------------------------
# exposure and input refusals
# ---------------------------------------------------------------------------

def test_secure_portal_on_public_target_refused(world):
    stage_portal(world["stage"])
    s = site(world, "secure", "both")
    with pytest.raises(ExposureRefusal) as ei:
        run(world, s, yes=True)
    assert ei.value.exit_code == 3
    assert not tgt(world, "both").exists()


def test_unserved_target(world):
    stage_portal(world["stage"])
    with pytest.raises(ExposureRefusal):
        run(world, site(world, "secure", "hidden"))
    res = run(world, site(world, "secure", "hidden", served_by="external"))
    assert res.written


def test_public_portal_on_secure_target_needs_yes(world):
    stage_portal(world["stage"])
    s = site(world, "public", "sec/portal")
    with pytest.raises(ExposureRefusal):
        run(world, s)
    logs = []
    res = run(world, s, yes=True, log=logs.append)
    assert res.written and any("warning" in ln for ln in logs)


@pytest.mark.parametrize("report", [
    {"allow_tmp_inputs": True},
    {"provenance": {"inputs": [{"path": "/tmp/x.fits", "sha256": "0"}]}},
    {"provenance": {"inputs": ["/blue/someone/tmp/cat.fits"]}},
])
def test_tmp_inputs_blocked(world, report):
    stage_portal(world["stage"], report=report)
    with pytest.raises(BlockedInput) as ei:
        run(world, site(world), dry_run=True)
    assert ei.value.exit_code == 5


def test_public_page_fetching_secure_data_refused(world):
    html = page_html({"datasets": {"src": {"url": SEC + "w51/portal/data/manifest.json"}}})
    stage_portal(world["stage"], files={"map.html": html})
    with pytest.raises(ExposureRefusal) as ei:
        run(world, site(world, "public", "pub"))
    assert ei.value.exit_code == 3
    assert "map.html" in str(ei.value)


def test_public_page_linking_secure_portal_warns(world):
    html = page_html({"related": [{"title": "W51", "url": SEC + "w51/portal/", "auth": "basic"}]},
                     body=f'<a href="{SEC}w51/portal/index.html">secure portal</a>')
    stage_portal(world["stage"], files={"map.html": html})
    logs = []
    res = run(world, site(world, "public", "pub"), log=logs.append)
    assert res.written
    assert any("login required" in ln for ln in logs)


def test_collect_references_link_rules(tmp_path):
    html = page_html({"products": [{"url": SEC + "a/b.fits", "link_only": True}],
                      "overlay": {"url": SEC + "a/c.json"}},
                     body=f'<a href="{SEC}x.html">x</a><img src="{SEC}y.png">')
    (tmp_path / "p.html").write_text(html)
    refs = {u: f for _, u, f in dmod.collect_references(tmp_path, ["p.html"])}
    assert refs[SEC + "a/b.fits"] is False
    assert refs[SEC + "a/c.json"] is True
    assert refs[SEC + "x.html"] is False
    assert refs[SEC + "y.png"] is True
    assert refs["data/src/manifest.json"] is True


def test_every_url_attribute_and_root_relative_urls(world, tmp_path):
    body = ('<img src="logo.png" data-src="' + SEC + 'x/cat.bin">'
            '<img alt="a > b src=\'q.bin\'" src=/secure/adamginsburg/x/cat2.bin>')
    (tmp_path / "p.html").write_text(page_html(body=body))
    got = [(u, f) for _, u, f in dmod.collect_references(tmp_path, ["p.html"])]
    assert (SEC + "x/cat.bin", True) in got
    assert ("/secure/adamginsburg/x/cat2.bin", True) in got
    assert not any(u == "q.bin" for u, _ in got)
    refs = dmod.build_references([("p.html", u, f) for u, f in got], tmp_path / "t")
    assert {r.url for r in refs} >= {SEC + "x/cat.bin", "/secure/adamginsburg/x/cat2.bin"}
    stage_portal(world["stage"], files={"map.html": page_html(body=body.split("<img alt")[0])})
    with pytest.raises(ExposureRefusal):
        run(world, site(world, "public", "pub"))
    stage_portal(world["stage"], BID2,
                 files={"map.html": page_html(body='<img src="/secure/adamginsburg/x/c.bin">')})
    with pytest.raises(ExposureRefusal):
        run(world, site(world, "public", "pub"), bid=BID2)


def test_submodules_not_shadowed():
    import portalkit.deploy as pkg
    import portalkit.deploy.deploy as dm
    import portalkit.deploy.status as sm
    assert dm is dmod and sm is stmod
    assert pkg.run_deploy is dmod.deploy and pkg.status_rows is stmod.status


def test_publish_immutable(tmp_path):
    src = tmp_path / "src"
    (src / "js").mkdir(parents=True)
    (src / "js" / "a.js").write_text("a")
    (src / "pk.css").write_text("css")
    dest_root = tmp_path / "web" / "public" / "portalkit"
    dry = dmod.publish_assets(src, dest_root, "1.0.0", dry_run=True)
    assert sorted(dry.created) == ["js/a.js", "pk.css"] and not dest_root.exists()
    res = dmod.publish_assets(src, dest_root, "1.0.0")
    dest = dest_root / "v1.0.0"
    assert (dest / "js" / "a.js").read_text() == "a" and res.dest == dest
    again = dmod.publish_assets(src, dest_root, "1.0.0")
    assert again.created == [] and sorted(again.same) == ["js/a.js", "pk.css"]
    (src / "pk.css").write_text("changed")
    (src / "new.js").write_text("n")
    with pytest.raises(OwnershipConflict) as ei:
        dmod.publish_assets(src, dest_root, "1.0.0")
    assert ei.value.exit_code == 4 and "pk.css" in str(ei.value)
    assert not (dest / "new.js").exists() and (dest / "pk.css").read_text() == "css"
    dmod.publish_assets(src, dest_root, "1.0.1")
    assert (dest_root / "v1.0.1" / "pk.css").read_text() == "changed"
    with pytest.raises(UsageError):
        dmod.publish_assets(src, dest_root, "latest")
    os.symlink(tmp_path, dest_root / "v2.0.0")
    with pytest.raises(SymlinkRefusal):
        dmod.publish_assets(src, dest_root, "2.0.0")


def test_bad_arguments(world):
    stage_portal(world["stage"])
    with pytest.raises(UsageError) as ei:
        run(world, site(world), replace="move")
    assert ei.value.exit_code == 2
    with pytest.raises(UsageError):
        run(world, site(world), bid="latest")
    with pytest.raises(UsageError):
        run(world, site(world), BID2)                  # not staged
    two = site(world)
    two["portal"].append(dict(two["portal"][0], id="other"))
    with pytest.raises(UsageError):
        run(world, two)


# ---------------------------------------------------------------------------
# gc and htaccess
# ---------------------------------------------------------------------------

def test_gc_removes_old_unreferenced_files(world):
    stage_portal(world["stage"])
    run(world, site(world))
    stage_portal(world["stage"], BID2, binname="cols.89abcdef.bin")
    run(world, site(world), BID2)
    t = tgt(world)
    old = t / "data/src/cols.0123abcd.bin"
    assert dmod.gc_target(t, FAM, "main") == []            # too new
    later = dmod.utcnow() + dt.timedelta(days=8)
    assert dmod.gc_target(t, FAM, "main", now=later, dry_run=True) == ["data/src/cols.0123abcd.bin"]
    assert old.exists()
    assert dmod.gc_target(t, FAM, "main", now=later) == ["data/src/cols.0123abcd.bin"]
    assert not old.exists()
    assert (t / "data/src/cols.89abcdef.bin").exists()
    man = dmod.load_manifest(t / dmod.MANIFEST_NAME)
    assert "data/src/cols.0123abcd.bin" not in man["files"]
    assert (t / "assets/pk.css").exists()                  # part of the current build


def test_htaccess_written_and_foreign_refused(world):
    stage_portal(world["stage"])
    run(world, site(world), htaccess=True)
    h = tgt(world) / ".htaccess"
    assert h.read_text().startswith(hmod.MARKER)
    assert hmod.htaccess_owned(h)
    assert hmod.write_htaccess(tgt(world)) is None         # unchanged
    h.write_text("AuthType Basic\n")
    with pytest.raises(OwnershipConflict):
        run(world, site(world), htaccess=True)
    assert h.read_text() == "AuthType Basic\n"


def test_htaccess_keeps_cache_block_and_denies_manifest(world):
    """WPE3: the section 11.5 block is kept verbatim and a mod_alias rule denies deploy's
    dot-files (ownership manifests with absolute paths, backups, temporaries)."""
    text = hmod.HTACCESS_TEXT
    assert text.startswith(hmod.MARKER + "\n" + hmod.HTACCESS_BODY)
    assert 'RedirectMatch 403 "' + hmod.DENY_RE + '"' in text
    assert "<IfModule mod_alias.c>" in text
    # no directive that needs the AuthConfig/Limit overrides (a 500 for the whole directory
    # when AllowOverride lacks them)
    for word in ("Require", "Deny from", "Order ", "AuthType"):
        assert word not in text, word
    assert hmod.denied(dmod.MANIFEST_NAME)
    assert hmod.denied(".portalkit-deploy.other.json")


@pytest.mark.parametrize("rel,deny", [
    (".portalkit-deploy.json", True),
    (".portalkit-deploy.c.json", True),
    ("sub/.portalkit-deploy.json", True),
    (".portalkit_backup/20261010T120000Z-0123abcd/backup.json", True),
    (".portalkit_backup/20261010T120000Z-0123abcd/sub/a.html", True),
    (".portalkit_backup", True),
    ("map.html.pk-tmp", True),
    ("data/src/cols.0123abcd.bin.pk-tmp", True),
    ("/secure/adamginsburg/x/unified/.portalkit-deploy.json", True),
    ("map.html", False),
    ("portal.json", False),
    ("data/src/manifest.json", False),
    ("data/src/cols.0123abcd.bin", False),
    ("portalkit-res/v1/filters.json", False),     # a name containing 'portalkit'
    ("data/x.portalkit-deploy.json", False),      # not at a segment start
    ("a.pk-tmp.html", False),
    ("ünï/.portalkit-deploy.json", True),
    ("", False),
])
def test_htaccess_denied(rel, deny):
    assert hmod.denied(rel) is deny


def test_htaccess_denies_every_deploy_dotfile_in_target(world):
    """After deploys of two portals into one target (primary and secondary manifests) with
    --htaccess, every dot-file deploy left in the target except .htaccess itself is denied,
    and no manifest-owned (served) file is."""
    t = world["orange"] / "both" / "shared"
    stage_portal(world["stage"], pid="a")
    d = world["stage"] / FAM / BID / "c"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "c.html").write_text("<p>c</p>")
    run(world, site(world, "public", "both", pid="a", target=str(t)), htaccess=True)
    run(world, site(world, "public", "both", pid="c", target=str(t)), htaccess=True)
    h = t / ".htaccess"
    assert h.read_text() == hmod.HTACCESS_TEXT
    dot = sorted(str(p.relative_to(t)) for p in t.rglob(".*") if p.name != ".htaccess")
    assert dot == sorted([dmod.MANIFEST_NAME, ".portalkit-deploy.c.json"])
    assert all(hmod.denied(r) for r in dot)
    for mname in dot:
        man = json.loads((t / mname).read_text())
        assert man["target"] == str(t)              # absolute: the reason it is denied
        assert not any(hmod.denied(r) for r in man["files"]), mname


def test_htaccess_upgrades_previous_owned_file(world):
    """An .htaccess written by an earlier portalkit (marker, no deny block) is rewritten."""
    stage_portal(world["stage"])
    t = tgt(world)
    t.mkdir(parents=True)
    old = hmod.MARKER + "\n" + hmod.HTACCESS_BODY
    (t / ".htaccess").write_text(old)
    assert hmod.htaccess_owned(t / ".htaccess")
    assert hmod.write_htaccess(t, dry_run=True) == t / ".htaccess"
    assert (t / ".htaccess").read_text() == old                  # dry run writes nothing
    run(world, site(world), htaccess=True)
    assert (t / ".htaccess").read_text() == hmod.HTACCESS_TEXT
    assert hmod.write_htaccess(t) is None


def test_backup_htaccess(world, tmp_path):
    ld = world["orange"] / "sec/portal"
    assert hmod.write_backup_htaccess(ld) is None                 # no backup tree yet
    b = ld / ".portalkit_backup"
    b.mkdir()
    assert hmod.write_backup_htaccess(ld, dry_run=True) == b / ".htaccess"
    assert not (b / ".htaccess").exists()
    assert hmod.write_backup_htaccess(ld) == b / ".htaccess"
    text = (b / ".htaccess").read_text()
    assert text == hmod.BACKUP_HTACCESS_TEXT and hmod.htaccess_owned(b / ".htaccess")
    assert "Cache-Control" not in text and hmod.DENY_RE in text
    assert hmod.write_backup_htaccess(ld) is None                 # unchanged
    (b / ".htaccess").write_text("AuthType Basic\n")
    with pytest.raises(OwnershipConflict):
        hmod.write_backup_htaccess(ld)
    (b / ".htaccess").unlink()
    os.symlink(tmp_path, b / ".htaccess")
    with pytest.raises(SymlinkRefusal):
        hmod.write_backup_htaccess(ld)
    ld2 = world["orange"] / "pub"
    os.symlink(world["orange"] / "hidden", ld2 / ".portalkit_backup")
    with pytest.raises(SymlinkRefusal):
        hmod.write_backup_htaccess(ld2)


@pytest.mark.parametrize("rel,cc", [
    ("data/src/cols.0123abcd.bin", hmod.IMMUTABLE),
    ("data/src/stats.0123abcd.json", hmod.IMMUTABLE),
    ("data/src/manifest.json", hmod.NO_CACHE),
    ("portal.json", hmod.NO_CACHE),
    ("map.html", hmod.NO_CACHE),
    ("assets/pk.css", None),
])
def test_expected_cache_control(rel, cc):
    assert hmod.expected_cache_control(rel) == cc


# ---------------------------------------------------------------------------
# inplace URL rewrite
# ---------------------------------------------------------------------------

def test_rewrite_relative_urls():
    html = page_html({"res": "../res/", "links": {"home": "index.html", "ext": "https://x.org/"},
                      "portal": {"nav": "portal.json"}},
                     body='<img src="./a.png"><a href="#top">t</a><script src="/abs.js"></script>')
    out = dmod.rewrite_relative_urls(html, "unified")
    page = dmod.extract_page_json(out)
    assert page["assets"] == "unified/assets/"
    assert page["res"] == "unified/../res/"
    assert page["datasets"]["src"]["url"] == "unified/data/src/manifest.json"
    assert page["links"] == {"home": "unified/index.html", "ext": "https://x.org/"}
    assert page["portal"]["nav"] == "unified/portal.json"
    assert 'href="unified/assets/pk.css"' in out
    assert 'src="unified/a.png"' in out
    assert 'href="#top"' in out and 'src="/abs.js"' in out
    two = dmod.rewrite_relative_urls('<video poster="p.png" src=\'v.mp4\' data-src=d.bin>', "u")
    assert two == '<video poster="u/p.png" src=\'u/v.mp4\' data-src=u/d.bin>'


# ---------------------------------------------------------------------------
# stage helpers
# ---------------------------------------------------------------------------

def test_build_ids():
    when = dt.datetime(2026, 10, 10, 12, 0, 0, tzinfo=dt.timezone.utc)
    a = smod.make_build_id("sha", ["b", "a"], when)
    assert a == smod.make_build_id("sha", ["a", "b"], when)
    assert a != smod.make_build_id("sha2", ["a", "b"], when)
    assert a.startswith("20261010T120000Z-") and smod.is_build_id(a)
    assert smod.build_time(a) == when
    assert not smod.is_build_id("20261010-abc")


def test_tmp_paths():
    assert smod.is_tmp_path("/tmp/x.fits")
    assert smod.is_tmp_path("/blue/adamginsburg/adamginsburg/tmp/a/b.fits")
    assert smod.is_tmp_path("/home/u/scratchpad/c.fits")
    assert not smod.is_tmp_path("/orange/adamginsburg/jwst/x.fits")


def test_prune_and_latest(world):
    root = world["stage"]
    ids = [f"2026101{i}T120000Z-0000000{i}" for i in range(6)]
    for b in ids:
        (root / FAM / b / "main").mkdir(parents=True)
    (root / FAM / "notabuild").mkdir()
    assert smod.list_builds(root, FAM) == ids
    assert smod.latest_build(root, FAM, "main") == ids[-1]
    assert smod.latest_build(root, FAM, "nope") is None
    gone = smod.prune_stage(root, FAM, keep=2, protect=[ids[0]], dry_run=True)
    assert [p.name for p in gone] == ids[1:4]
    smod.prune_stage(root, FAM, keep=2, protect=[ids[0]])
    assert smod.list_builds(root, FAM) == [ids[0]] + ids[4:]
    assert (root / FAM / "notabuild").is_dir()


def test_stage_root_must_be_unserved(world, monkeypatch):
    with pytest.raises(ExposureRefusal):
        smod.check_stage_root(world["orange"] / "pub" / "stage", world["emap"])
    smod.check_stage_root(world["stage"], world["emap"])
    d = smod.create_portal_stage_dir(world["stage"], FAM, BID, "main", world["emap"])
    assert d == world["stage"] / FAM / BID / "main" and d.is_dir()
    monkeypatch.setenv("PK_STAGE_ROOT", str(world["stage"]))
    assert smod.stage_root() == world["stage"]
    with pytest.raises(DeployError):
        smod.build_dir(world["stage"], FAM, "../x")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_status_flags(world, tmp_path):
    s = site(world)
    entry = s["portal"][0]
    row = stmod.portal_status(FAM, s, entry, stage_root=world["stage"])
    assert row.flags == ["NOT-STAGED", "NOT-DEPLOYED"]

    mod = tmp_path / "legacy_mod.py"
    mod.write_text("x = 1\n")
    stage_portal(world["stage"], report={"provenance": {
        "legacy_modules": [{"name": "legacy_mod", "path": str(mod),
                            "sha256": dmod.sha256_file(mod)[0]}]}})
    run(world, s)
    row = stmod.portal_status(FAM, s, entry, stage_root=world["stage"])
    assert row.flags == [] and row.staged_sha == row.served_sha
    assert row.deployed_build == BID

    (tgt(world) / "assets/pk.css").write_text("tampered")
    mod.write_text("x = 2\n")
    row = stmod.portal_status(FAM, s, entry, stage_root=world["stage"])
    assert "MODIFIED" in row.flags and "LEGACY-CHANGED" in row.flags and "DRIFT" in row.flags
    shallow = stmod.portal_status(FAM, s, entry, stage_root=world["stage"], deep=False)
    assert "MODIFIED" not in shallow.flags and "DRIFT" not in shallow.flags

    stage_portal(world["stage"], BID2, files={"map.html": page_html({"title": "v2"})})
    row = stmod.portal_status(FAM, s, entry, stage_root=world["stage"], deep=False)
    assert row.staged_build == BID2 and "DRIFT" in row.flags

    excl = dict(s, status="excluded")
    rows = stmod.status([excl], stage_root=world["stage"])
    assert rows[0].flags == ["excluded"]
    txt = stmod.format_status(stmod.status(s, stage_root=world["stage"]))
    assert "main" in txt and BID2 in txt
    assert json.loads(stmod.format_status(rows, as_json=True))[0]["status"] == "excluded"


# ---------------------------------------------------------------------------
# per-legacy-file preset and replace policy (WPE1 item 1)
# ---------------------------------------------------------------------------

def test_replace_per_file_preset_and_replace_false(world):
    stage_portal(world["stage"])
    ld = world["orange"] / "sec/portal"
    (ld / "a.html").write_text("old a")
    (ld / "sub").mkdir()
    (ld / "sub/b.html").write_text("old b")
    real = world["orange"] / "hidden" / "c.html"
    real.write_text("behind a symlink")
    os.symlink(real, ld / "c.html")
    pages = [{"id": "map", "preset": "p0", "legacy": [
        "a.html", {"file": "sub/b.html", "preset": "role=c2221"},
        {"file": "c.html", "replace": False}]}]
    s = site(world, pages=pages)
    plan = dmod.plan_deploy(FAM, BID, s, stage_root=world["stage"], replace="stub",
                            emap=world["emap"])
    assert plan.conflicts == []          # the symlinked c.html is skipped, not refused
    by = {ra.legacy_file: ra for ra in plan.replace}
    assert sorted(by) == ["a.html", "sub/b.html"]
    assert by["a.html"].preset == "p0" and by["sub/b.html"].preset == "role=c2221"
    assert any("replace=false" in w and "c.html" in w for w in plan.warnings)
    run(world, s, replace="stub")
    assert "'#role=c2221'" in (ld / "sub/b.html").read_text()
    assert "'#p0'" in (ld / "a.html").read_text()
    assert real.read_text() == "behind a symlink"
    # without replace=false the symlinked file is a refusal
    pages[0]["legacy"][2] = "c.html"
    plan = dmod.plan_deploy(FAM, BID, site(world, pages=pages), stage_root=world["stage"],
                            replace="stub", emap=world["emap"])
    assert any("symlink" in c for c in plan.conflicts)


def test_legacy_entries_forms():
    from portalkit.context import PageConfig
    raw = {"id": "g", "legacy": ["a.html", {"file": "b.html", "preset": ""},
                                 {"file": "c.html", "replace": False}]}
    want = [("a.html", None, True), ("b.html", "", True), ("c.html", None, False)]
    assert dmod.legacy_entries(raw) == want
    pc = PageConfig(id="g", files=raw["legacy"])
    assert pc.legacy == ["a.html", "b.html", "c.html"]
    assert dmod.legacy_entries(pc) == want
    assert pc.presets() == {"b.html": ""}
    assert dmod.legacy_entries(PageConfig(id="h", legacy=["x.html"])) == [("x.html", None, True)]


def test_not_deployed_any_depth(world):
    assert dmod.not_deployed("build_report.json")
    assert dmod.not_deployed("sub/build_report.json")
    assert dmod.not_deployed("data/x.json" + dmod.TMP_SUFFIX)
    assert not dmod.not_deployed("portal.json")
    stage_portal(world["stage"], files={"sub/build_report.json": "{}"})
    run(world, site(world))
    assert not (tgt(world) / "build_report.json").exists()
    assert not (tgt(world) / "sub/build_report.json").exists()
