"""portalkit build / check / caps and the CLI around them (sections 9.4 and 10).

One synthetic family ("clismoke") with a tmp site registry and adapter module; builds go to
a tmp stage root with an empty exposure map, so nothing here touches served directories.
Shared resources take a few seconds to write, so the module builds the family once and the
other tests read that build.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import struct
import sys
import textwrap
import uuid
import zlib
from pathlib import Path

import pytest

from portalkit import cli
from portalkit import build as buildmod
from portalkit.context import Cache, InputRegistry, expand_inputs
from portalkit.deploy import BlockedInput, ExposureMap, UsageError
from portalkit.legacy.extract import script_json
from portalkit.version import VERSION

SNIP = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_snippets"
FAMILY = "clismoke"

ADAPTER_SRC = '''
import os
import numpy as np
from portalkit.spec import (Spectrum, RecordSetSpec, ReadmeSpec, TemplateSpec,
                            catalog_from_rows, standard_pages, TierUnavailable)
from portalkit.legacy import dirgallery


class Adapter:
    name = "clismoke"
    tiers = ("A", "B")

    def datasets(self, ctx):
        if ctx.tier not in self.tiers:
            raise TierUnavailable("tier " + ctx.tier)
        rng = np.random.default_rng(1)
        n = 30 if ctx.limit is None else min(ctx.limit, 30)
        rows = [{"id": f"S{i:03d}", "ra": 266.0 + 0.01 * i, "dec": -29.0 + 0.01 * i,
                 "f200w": 15 + rng.random(), "f356w": 14 + rng.random(),
                 "f444w": 13 + rng.random(), "cls": ["a", "b"][i % 2]} for i in range(n)]
        cat = catalog_from_rows(rows, "id", "ra", "dec",
                                roles={"f200w": "mag", "f356w": "mag", "f444w": "mag"},
                                columns={"f200w": {"filter": "JWST/NIRCam.F200W"},
                                         "f356w": {"filter": "JWST/NIRCam.F356W"},
                                         "f444w": {"filter": "JWST/NIRCam.F444W"}},
                                key="src", title="Smoke sources")

        def load(i):
            w = np.linspace(1.0, 5.0, 100)
            return Spectrum(wave=w, channels={"flux": 1e-3 * w ** -1})
        ds = {"src": cat,
              "spec": RecordSetSpec(key="spec", kind="pks1", catalog="src",
                                    ids=[r["id"] for r in rows[:5]], loader=load,
                                    noun=("trace", "traces")),
              "readme": ReadmeSpec(markdown="# Smoke\\n\\nA test portal."),
              "tpl": TemplateSpec()}
        img = os.environ.get("PK_CLI_IMG")
        if img:
            ds["img"] = dirgallery.scan(img, r"(?P<id>S\\d+)\\.png", key="img",
                                        url_prefix=img.rstrip("/") + "/")
        return ds

    def pages(self, ctx, ds):
        return standard_pages(ds, spectra=True)


ADAPTER = Adapter()
'''


def png_bytes(w: int, h: int) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes((x * 7) % 256 for x in range(w)) for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("clibuild")
    tag = uuid.uuid4().hex[:8]
    mod = f"pk_cli_adapter_{tag}"
    adir = root / "adapters"
    adir.mkdir()
    (adir / f"{mod}.py").write_text(ADAPTER_SRC)
    legacy = root / "legacy"
    legacy.mkdir()
    (legacy / "old_map.html").write_text("<html><body>legacy</body></html>")
    (legacy / "input1.txt").write_text("1 2 3\n")
    sites = root / "sites"
    sites.mkdir()
    (sites / f"{FAMILY}.toml").write_text(textwrap.dedent(f'''
        family = "{FAMILY}"
        adapter = "{mod}:ADAPTER"
        tier_default = "A"

        [[portal]]
        id = "main"
        title = "CLI smoke portal"
        visibility = "public"
        legacy_dir = "{legacy}"
        url = "https://data.rc.ufl.edu/pub/adamginsburg/clismoke/"
        inputs = ["{legacy}/*.txt", "{legacy}/nothing_*.dat"]

        [[portal.page]]
        id = "explorer"
        legacy = ["old_map.html", {{file = "old_map_b.html", preset = "view=b"}},
                  {{file = "linked/index.html", replace = false}}]
        grammar = ["kv-view"]
        '''))
    stage = root / "stage"
    stage.mkdir()
    emap = ExposureMap(roots={}, url_prefixes={})
    mp = pytest.MonkeyPatch()
    mp.syspath_prepend(str(adir))
    mp.setenv("PK_CACHE_DIR", str(root / "cache"))
    mp.setenv("PK_STAGE_ROOT", str(stage))
    mp.setenv("PK_SITES_DIR", str(sites))
    mp.delenv("PK_CLI_IMG", raising=False)
    mp.setattr(cli, "_emap", lambda: emap)
    yield {"root": root, "sites": sites, "stage": stage, "legacy": legacy, "emap": emap,
           "module": mod}
    mp.undo()
    sys.modules.pop(mod, None)


@pytest.fixture(scope="module")
def built(env):
    """``portalkit build`` through the CLI (exit code and staged tree)."""
    code = cli.main(["--sites-dir", str(env["sites"]), "build", FAMILY, "--allow-tmp-inputs",
                     "--stage-root", str(env["stage"])])
    builds = sorted(p for p in (env["stage"] / FAMILY).iterdir() if p.is_dir())
    return {"code": code, "dir": builds[-1], "portal": builds[-1] / "main"}


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# ---------------------------------------------------------------------------
# build + check
# ---------------------------------------------------------------------------

def test_build_ok(built):
    assert built["code"] == 0
    pdir = built["portal"]
    for page in ("index", "explorer", "spectra"):
        assert (pdir / f"{page}.html").is_file()
    assert (pdir / "portal.json").is_file()
    assert (built["dir"] / "_res").is_dir()


def test_build_report(built, env):
    rep = json.loads((built["portal"] / "build_report.json").read_text())
    assert rep["schema"] == "pk-build-report/1"
    assert rep["family"] == FAMILY and rep["portal"] == "main" and rep["tier"] == "A"
    assert rep["build_id"] == built["dir"].name
    assert len(rep["adapter_sha"]) == 64
    assert rep["portalkit"] == VERSION
    assert rep["allow_tmp_inputs"] is True
    assert any(i["path"].endswith("input1.txt") for i in rep["inputs"])
    assert any("nothing_" in u for u in rep["unmatched_inputs"])
    assert isinstance(rep["legacy_modules"], list)
    assert rep["ok"] is True and rep["errors"] == []
    assert rep["budgets"]["total_bytes"] > 0


def test_page_json_refs_and_legacy(built):
    page = script_json(built["portal"] / "explorer.html", "pk-page")
    assert page["page"]["id"] == "explorer"
    urls = {k: v["url"] for k, v in page["datasets"].items()}
    assert urls["src"] == "data/src/manifest.json"
    assert any(u.startswith("{res}") for u in urls.values())
    assert page["page"]["legacy"]["grammar"] == ["kv-view"]
    # per-file legacy tables (section 4.1): files stay strings, presets map file -> preset
    assert page["page"]["legacy"]["files"] == ["old_map.html", "old_map_b.html",
                                               "linked/index.html"]
    assert page["page"]["legacy"]["presets"] == {"old_map_b.html": "view=b"}
    assert (built["portal"] / "data/src/manifest.json").is_file()
    portal = json.loads((built["portal"] / "portal.json").read_text())
    assert [p["id"] for p in portal["pages"]] == ["index", "explorer", "spectra"]


def test_portal_counts_cover_every_page(built):
    """portal.json ``counts``: one entry per non-landing page with the size of its primary
    dataset (catalog rows, record ids, image items)."""
    portal = json.loads((built["portal"] / "portal.json").read_text())
    by = {c["page"]: c for c in portal["counts"]}
    assert set(by) == {"explorer", "spectra"}
    assert by["explorer"] == {"page": "explorer", "ds": "src", "kind": "catalog", "n": 30,
                              "unit": "source", "units": "sources"}
    assert by["spectra"]["kind"] == "records" and by["spectra"]["ds"] == "spec"
    # a spec's ``noun`` replaces the kind's default nouns
    assert by["spectra"]["n"] == 5 and (by["spectra"]["unit"], by["spectra"]["units"]) == ("trace", "traces")
    assert [s["ds"] for s in portal["search"]] == ["src"]


def test_shared_resources_attached_by_the_caps_rule(built):
    """IMPLEMENTATION_NOTES 110: build attaches the shared resources that
    ``caps.attachable_resources`` names (refs named by ``caps.resource_refs``), so
    extinction_vectors_on_ccd stays on with the shared G23 vector on a page that has no
    extinction dataset, and every NEEDS capability a page keeps has its document."""
    from portalkit import caps
    page = script_json(built["portal"] / "explorer.html", "pk-page")
    enabled = page["caps"]["enabled"]
    assert "extinction_vectors_on_ccd" in enabled
    kinds = {r["kind"] for r in page["datasets"].values()}
    assert "extinction" in kinds
    ext = [k for k, r in page["datasets"].items() if r["kind"] == "extinction"]
    assert ext == ["ext"] and page["datasets"]["ext"]["url"].startswith("{res}")
    # "tpl" is an adapter dataset id, so the attached templates ref (when any) is "_tpl"
    if "templates" in kinds and "tpl" not in page["datasets"]:
        assert page["datasets"]["_tpl"]["kind"] == "templates"
    ov = [p.get("options", {}).get("overlays") for p in caps.iter_panes(page["layout"])
          if p["pane"] in ("scatter", "manifold")]
    assert ov and all(o and "g23_vector" in o for o in ov), ov
    for p in ("index", "explorer", "spectra"):
        doc = script_json(built["portal"] / f"{p}.html", "pk-page")
        have = {r["kind"] for r in doc["datasets"].values()}
        assert caps.unmet_needs(doc["caps"]["enabled"], have) == {}, p
        for cap, (kind, reason) in caps.NEEDS.items():
            if kind not in have and cap in doc["caps"]["disabled"]:
                why = doc["caps"]["disabled"][cap]
                assert why == reason or why.startswith("no pane"), (p, cap, why)


def test_check_cli(built, capsys):
    code, out, _ = run(capsys, "check", str(built["dir"]))
    assert code == 0 and "check" in out
    code, out, _ = run(capsys, "check", str(built["portal"]), "--gate", "G2", "--json")
    assert code == 0
    rep = json.loads(out)
    assert rep["ok"] is True and rep["gate"] == "G2" and "main" in rep["portals"]


def test_check_detects_damage(built, tmp_path, capsys):
    bad = tmp_path / "bad"
    shutil.copytree(built["portal"], bad)
    (bad / "data/src/manifest.json").write_text('{"schema": "pk-catalog/1"}')
    html = (bad / "spectra.html").read_text()
    (bad / "spectra.html").write_text(html.replace('id="pk-page"', 'id="pk-gone"'))
    rep = buildmod.check_build(bad)
    assert not rep.ok and rep.exit_code == 1
    text = "\n".join(rep.errors)
    assert "dataset src" in text and "page spectra" in text
    code, _, _ = run(capsys, "check", str(bad))
    assert code == 1
    code, _, err = run(capsys, "check", str(tmp_path / "missing"))
    assert code in (1, 2)


def test_served_files_carry_labelled_paths(built, env):
    """Provenance in served documents uses ``<label>:<relpath>``; build_report.json keeps the
    full paths (section 9.3)."""
    legacy = str(env["legacy"])
    man = json.loads((built["portal"] / "data/src/manifest.json").read_text())
    paths = [i["path"] for i in man["provenance"]["inputs"]]
    assert "legacy_dir:input1.txt" in paths and not any(p.startswith("/") for p in paths)
    for f in built["portal"].rglob("*"):
        if f.suffix in (".html", ".json") and f.name != "build_report.json":
            assert legacy not in f.read_text(), f
    rep = json.loads((built["portal"] / "build_report.json").read_text())
    assert any(i["path"] == f"{legacy}/input1.txt" for i in rep["inputs"])


def test_check_flags_absolute_local_paths(built, tmp_path):
    bad = tmp_path / "abs"
    shutil.copytree(built["portal"], bad)
    (bad / "data/extra.json").write_text(json.dumps({"p": "/orange/x/y.fits",
                                                     "q": "file:///home/u/z.py"}))
    (bad / "build_report.json").write_text(json.dumps({"inputs": [{"path": "/blue/a"}]}))
    g1 = buildmod.check_build(bad, gate="G1")
    assert g1.ok and any("data/extra.json: 2 local path(s)" in w for w in g1.warnings), g1.warnings
    assert not any("build_report" in w for w in g1.warnings)
    g2 = buildmod.check_build(bad, gate="G2")
    assert not g2.ok and any("absolute local path" in e for e in g2.errors)
    assert buildmod.check_build(built["portal"], gate="G2").ok


def test_check_local_paths_in_shared_res(built, tmp_path):
    """Shared-resource documents (vendored res/, the build's _res/) follow the same rule as
    every served file: a warning at G1, an error at G2."""
    root = tmp_path / "b"
    pdir = root / "main"
    shutil.copytree(built["portal"], pdir)
    (pdir / "res" / "templates").mkdir(parents=True)
    (pdir / "res" / "templates" / "index.json").write_text(json.dumps({"meta": {"source": "/orange/t.fits"}}))
    (root / "_res" / "templates").mkdir(parents=True)
    (root / "_res" / "templates" / "index.abc.json").write_text(json.dumps({"meta": {"source": "/blue/t.fits"}}))
    g1 = buildmod.check_build(pdir, gate="G1")
    assert g1.ok and any("res/templates/index.json" in w for w in g1.warnings), g1.warnings
    assert any("shared resource ../_res/templates/index.abc.json" in w for w in g1.warnings), g1.warnings
    g2 = buildmod.check_build(pdir, gate="G2")
    assert not g2.ok
    assert any("res/templates/index.json" in e for e in g2.errors), g2.errors
    assert any("../_res/templates/index.abc.json" in e for e in g2.errors), g2.errors


def test_shared_resources_are_labelled(built):
    """The default template source (an absolute /orange/ path in physics' meta.source) is
    labelled when the build writes the shared _res/ tree."""
    res = built["dir"] / "_res"
    docs = [p for p in res.rglob("*.json")]
    assert docs
    for p in docs:
        assert not buildmod.LOCAL_PATH_RE.search(p.read_text()), p
    idx = [json.loads(p.read_text()) for p in (res / "templates").glob("index.*.json")]
    assert idx and all(not str(d.get("meta", {}).get("source", "")).startswith("/") for d in idx)


def test_build_vendor_assets_page_filter(env):
    res = buildmod.build_family(FAMILY, pages=["explorer"], vendor_assets=True,
                                allow_tmp_inputs=True, stage_root=env["stage"],
                                sites_dir=env["sites"], emap=env["emap"], limit=10,
                                log=lambda s: None)
    assert res.ok, res.report["portals"]["main"]["errors"] + res.report["portals"]["main"].get("check", {}).get("errors", [])
    assert res.pages == ["main/explorer.html"]
    pdir = res.staging / "main"
    assert (pdir / "_pk" / f"v{VERSION}" / "js" / "core" / "boot.js").is_file()
    assert not (pdir / "spectra.html").exists()
    assert buildmod.check_build(pdir).ok
    assert buildmod.check_build(pdir, gate="G2").ok       # vendored res/ is labelled too


# ---------------------------------------------------------------------------
# build refusals
# ---------------------------------------------------------------------------

def test_tmp_inputs_refused(env, capsys):
    code, _, err = run(capsys, "--sites-dir", str(env["sites"]), "build", FAMILY)
    assert code == 5, err
    assert "tmp" in err.lower()


@pytest.mark.parametrize("extra, needle", [
    (["--pages", "nope"], "nope"),
    (["--tier", "C"], "tier"),
])
def test_usage_errors(env, capsys, extra, needle):
    code, _, err = run(capsys, "--sites-dir", str(env["sites"]), "build", FAMILY,
                       "--allow-tmp-inputs", *extra)
    assert code == 2 and needle in err


def test_unknown_family_and_bad_args(env, capsys):
    code, _, _ = run(capsys, "--sites-dir", str(env["sites"]), "build", "nosuchfamily")
    assert code == 2
    code, _, _ = run(capsys, "build")
    assert code == 2
    code, _, _ = run(capsys, "frobnicate")
    assert code == 2
    code, _, _ = run(capsys, "build", FAMILY, "--jobs", "0")
    assert code == 2


def test_load_adapter_errors():
    with pytest.raises(UsageError):
        buildmod.load_adapter("pk_no_such_module_xyz:ADAPTER")
    with pytest.raises(UsageError):
        buildmod.load_adapter("json:NOPE")


# ---------------------------------------------------------------------------
# caps, extract-legacy, thumbs
# ---------------------------------------------------------------------------

def test_caps_cli(env, capsys):
    code, out, _ = run(capsys, "--sites-dir", str(env["sites"]), "caps", FAMILY, "--json")
    assert code == 0
    rows = json.loads(out)
    assert {r["page"] for r in rows} == {"index", "explorer", "spectra"}
    exp = next(r for r in rows if r["page"] == "explorer")
    assert exp["enabled"] and isinstance(exp["disabled"], dict)
    code, out, _ = run(capsys, "--sites-dir", str(env["sites"]), "caps", FAMILY)
    assert code == 0 and "main/explorer (explorer)" in out


def test_extract_legacy_cli(tmp_path, capsys):
    out = tmp_path / "payload.json"
    code, _, err = run(capsys, "extract-legacy", str(SNIP / "assign_const.html"), "--out", str(out))
    assert code == 0 and "assign DATA" in err
    data = json.loads(out.read_text())
    assert data["rows"][1]["f200w"] is None            # NaN -> null
    code, stdout, err = run(capsys, "extract-legacy", str(SNIP / "manifold.html"))
    assert code == 0 and "manifold payload: 8 rows" in err
    assert len(json.loads(stdout)["stars"]) == 8
    code, _, err = run(capsys, "extract-legacy", str(SNIP / "no_payload.html"))
    assert code == 1 and "no embedded payload" in err
    code, _, _ = run(capsys, "extract-legacy", str(tmp_path / "missing.html"))
    assert code in (1, 2)


def test_thumbs(env, tmp_path, monkeypatch, capsys):
    img = tmp_path / "img"
    img.mkdir()
    (img / "S001.png").write_bytes(png_bytes(600, 40))
    (img / "S002.png").write_bytes(png_bytes(20, 20))
    monkeypatch.setenv("PK_CLI_IMG", str(img))
    counts = buildmod.make_thumbs(FAMILY, max_px=100, sites_dir=env["sites"], log=lambda s: None)
    assert counts == {"made": 1, "cached": 0, "small": 1, "remote": 0}
    code, out, _ = run(capsys, "--sites-dir", str(env["sites"]), "thumbs", FAMILY, "--max-px", "100")
    assert code == 0 and "1 cached" in out
    thumbs = list(buildmod.thumb_cache_dir(FAMILY).glob("*.png"))
    assert len(thumbs) == 1


def test_build_thumb_urls_resolve_from_page(env, tmp_path, monkeypatch):
    """Thumbnail URLs in images.json are page-relative (IMPLEMENTATION_NOTES 81) and pass check."""
    img = tmp_path / "img"
    img.mkdir()
    (img / "S001.png").write_bytes(png_bytes(900, 40))
    monkeypatch.setenv("PK_CLI_IMG", str(img))
    monkeypatch.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    assert buildmod.make_thumbs(FAMILY, max_px=480, sites_dir=env["sites"], log=lambda s: None)["made"] == 1
    stage = tmp_path / "stage"
    code = cli.main(["--sites-dir", str(env["sites"]), "build", FAMILY, "--allow-tmp-inputs",
                     "--stage-root", str(stage)])
    assert code == 0
    portal = sorted(p for p in (stage / FAMILY).iterdir() if p.is_dir())[-1] / "main"
    docs = list((portal / "data" / "img").glob("*.json"))
    thumbs = [rv["thumb"] for d in docs for it in json.loads(d.read_text()).get("items", [])
              for rv in it.get("roles", {}).values() if rv.get("thumb")]
    assert len(thumbs) == 1 and thumbs[0].startswith("data/img/")
    assert (portal / thumbs[0]).is_file()
    # a thumb written relative to the dataset dir would 404 from the page: check reports it
    errors: list[str] = []
    buildmod._check_files(buildmod._portal_root(portal / "data" / "img"),
                          [thumbs[0][len("data/img/"):]], "img", errors, label="thumbnail")
    assert errors


# ---------------------------------------------------------------------------
# status / deploy guards / prune-stage
# ---------------------------------------------------------------------------

def test_status_cli(env, built, capsys):
    code, out, _ = run(capsys, "--sites-dir", str(env["sites"]), "status", FAMILY, "--json")
    assert code == 0
    assert FAMILY in out


def test_deploy_refuses_tmp_input_build(env, built, capsys, tmp_path):
    target = tmp_path / "target"
    code, _, err = run(capsys, "--sites-dir", str(env["sites"]), "deploy", FAMILY,
                       built["dir"].name, "--target", str(target), "--yes")
    assert code == 5, err
    assert "allow-tmp-inputs" in err
    assert not target.exists() or not any(target.iterdir())


class _FakePlan:
    portal_id = "main"
    target = "/scratch/target"

    def summary(self):
        return "deploy plan summary"


class _FakeResult:
    written = ["a.html"]
    replaced = []
    gc_removed = ["old.json"]
    exit_code = 0


@pytest.fixture
def fake_deploy(monkeypatch):
    """Replace the deploy engine; record the CLI's calls (the CLI logic is under test)."""
    import portalkit.deploy as dep
    import portalkit.deploy.deploy as depmod
    calls = []
    monkeypatch.setattr(dep, "plan_deploy", lambda *a, **k: calls.append(("plan", k)) or _FakePlan())
    monkeypatch.setattr(depmod, "raise_for_plan", lambda plan: calls.append(("raise", None)))
    monkeypatch.setattr(dep, "run_deploy", lambda *a, **k: calls.append(("run", k)) or _FakeResult())
    return calls


def test_deploy_cli_flow(env, capsys, monkeypatch, fake_deploy):
    base = ["--sites-dir", str(env["sites"]), "deploy", FAMILY, "20261010T000000Z-0123abcd"]
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    # no TTY, no --yes: plan is made with yes=False (warn-level refused) and nothing is run
    code, out, err = run(capsys, *base)
    assert code == 2 and "--yes" in err
    assert fake_deploy[0][1]["yes"] is False
    assert not [c for c in fake_deploy if c[0] == "run"]
    fake_deploy.clear()
    # dry run
    code, out, _ = run(capsys, *base, "--dry-run")
    assert code == 0 and "dry run" in out
    runs = [k for c, k in fake_deploy if c == "run"]
    assert len(runs) == 1 and runs[0]["dry_run"] is True
    fake_deploy.clear()
    # --yes, --gc, --replace without a value means stub
    code, out, _ = run(capsys, *base, "--yes", "--gc", "--replace", "--htaccess")
    assert code == 0 and "gc removed 1" in out
    runs = [k for c, k in fake_deploy if c == "run"]
    assert runs[0]["dry_run"] is False and runs[0]["gc"] is True and runs[0]["yes"] is True
    assert runs[0]["replace"] == "stub" and runs[0]["htaccess"] is True
    assert runs[0]["portal_id"] == "main"
    fake_deploy.clear()
    # TTY: the prompt decides
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli, "_confirm", lambda prompt: False)
    code, out, _ = run(capsys, *base, "--replace=inplace")
    assert code == 1 and "not deployed" in out
    assert fake_deploy[0][1]["yes"] is True and fake_deploy[0][1]["replace"] == "inplace"
    assert not [c for c in fake_deploy if c[0] == "run"]
    monkeypatch.setattr(cli, "_confirm", lambda prompt: True)
    code, _, _ = run(capsys, *base)
    assert code == 0 and [c for c in fake_deploy if c[0] == "run"]


def test_deploy_unknown_portal(env, capsys):
    code, _, err = run(capsys, "--sites-dir", str(env["sites"]), "deploy", FAMILY,
                       "20261010T000000Z-0123abcd", "--portal", "nope")
    assert code == 2, err


def test_prune_stage_dry_run(env, built, capsys):
    code, out, _ = run(capsys, "--sites-dir", str(env["sites"]), "prune-stage", FAMILY,
                       "--keep", "1", "--dry-run")
    assert code == 0 and "deployed build(s) protected" in out
    assert built["dir"].is_dir()


# ---------------------------------------------------------------------------
# archive-checklist-diff
# ---------------------------------------------------------------------------

CHECKLIST = ("ISOKN,ISOKW,Target,Program,Grating,Grating2,Object,SlitID,Observation,Visit,"
             "VisitGroup,Filename\n"
             "y,,T1,1309,g235m,g395m,O1,s001,1,1,1,jw01309_a_x1d.fits\n"
             ",,T2,1309,g235m,,O2,s002,1,1,1,jw01309_b_x1d.fits\n"
             "n,,T3,2640,prism,,O3,s003,2,1,1,jw02640_c_x1d.fits\n")


def _export(rows, key="ISOKN"):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["id", *cli.CHECKLIST_KEYS, key])
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def test_checklist_diff_function():
    exp = _export([["a", "T2", "1309.0", "O2", "s002", "1", "1", "1", "jw01309_b_x1d.fits", "w"],
                   ["b", "T1", "1309", "O1", "s001", "1", "1", "1", "jw01309_a_x1d.fits", "y"],
                   ["c", "T9", "1", "O9", "s9", "1", "1", "1", "nope.fits", "e"]])
    diff, notes = cli.checklist_diff(exp, CHECKLIST, checklist_name="cl.csv")
    assert "-,,T2,1309,g235m,,O2" in diff and "+w,,T2,1309,g235m,,O2" in diff
    assert "T1" not in "".join(l for l in diff.splitlines() if l.startswith(("+", "-"))
                               and not l.startswith(("+++", "---")))
    assert any("no checklist row matches c" in n for n in notes)
    assert any("1 checklist row(s) change" in n for n in notes)


def test_checklist_diff_columns():
    exp = _export([["a", "T3", "2640", "O3", "s003", "2", "1", "1", "jw02640_c_x1d.fits", "l"]],
                  key="checklist")
    diff, notes = cli.checklist_diff(exp, CHECKLIST)
    assert any("codes go to 'ISOKN'" in n for n in notes)
    assert "+l,,T3" in diff
    diff, _ = cli.checklist_diff(exp, CHECKLIST, column="ISOKW")
    assert "+n,l,T3" in diff
    with pytest.raises(UsageError):
        cli.checklist_diff(exp, CHECKLIST, column="Nope")
    with pytest.raises(UsageError):
        cli.checklist_diff("id,Target\n", CHECKLIST)
    with pytest.raises(UsageError):
        cli.checklist_diff(exp, "a,b\n1,2\n")


def test_checklist_diff_cli_never_writes(tmp_path, capsys):
    cl = tmp_path / "checklist.csv"
    cl.write_text(CHECKLIST)
    before = cl.stat().st_mtime_ns, cl.read_bytes()
    ex = tmp_path / "export.csv"
    ex.write_text(_export([["a", "T2", "1309", "O2", "s002", "1", "1", "1",
                            "jw01309_b_x1d.fits", "g"]]))
    code, out, err = run(capsys, "archive-checklist-diff", str(ex), "--checklist", str(cl))
    assert code == 0 and "+g,,T2" in out and "1 checklist row(s) change" in err
    assert (cl.stat().st_mtime_ns, cl.read_bytes()) == before


# ---------------------------------------------------------------------------
# context helpers used by the build
# ---------------------------------------------------------------------------

def test_input_registry(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    reg = InputRegistry(False, cache_dir=tmp_path / "c")
    with pytest.raises(BlockedInput):
        reg.add(f)
    reg = InputRegistry(True, cache_dir=tmp_path / "c")
    rec = reg.add(f)
    assert rec["sha256"] and rec["size"] == 1
    assert reg.records()[0]["path"] == str(f)
    assert reg.tmp
    paths, missing = expand_inputs([str(tmp_path / "*.txt"), str(tmp_path / "zz*.q")])
    assert paths == [str(f)] and missing == [str(tmp_path / "zz*.q")]


def test_cache_memo(tmp_path):
    import numpy as np
    f = tmp_path / "in.txt"
    f.write_text("1")
    reg = InputRegistry(True, cache_dir=tmp_path / "c")
    cache = Cache(tmp_path / "c", registry=reg)
    calls = []

    def fn():
        calls.append(1)
        return np.arange(4.0)
    a = cache.memo("k", fn, inputs=[f])
    b = cache.memo("k", fn, inputs=[f])
    assert np.array_equal(a, b) and len(calls) == 1
    assert cache.hits == 1 and cache.misses == 1
    os.utime(f, ns=(1, 1))
    cache.memo("k", fn, inputs=[f])
    assert len(calls) == 2
    assert cache.memo("j", lambda: {"x": [1, 2]}) == {"x": [1, 2]}
    off = Cache(tmp_path / "c", enabled=False)
    off.memo("k", fn, inputs=[f])
    assert len(calls) == 3


def test_publish_refuses_unserved_dest(env, tmp_path, capsys):
    """With the (empty) exposure map, a scratch destination is not a public root: refused."""
    code, _, err = run(capsys, "publish-assets", "--dest", str(tmp_path / "assets"), "--dry-run")
    assert code == 3, err
    assert not (tmp_path / "assets").exists()
    code, _, err = run(capsys, "publish-res", "--dest", str(tmp_path / "res"), "--dry-run")
    assert code == 3, err
    assert not (tmp_path / "res").exists()


def test_verify_and_rollback_without_deploy(env, capsys):
    code, out, err = run(capsys, "--sites-dir", str(env["sites"]), "verify", FAMILY)
    assert code == 1, out + err
    code, out, err = run(capsys, "--sites-dir", str(env["sites"]), "rollback", FAMILY,
                         "20261010T000000Z-0123abcd")
    assert code != 0, out + err


def test_serve_missing_dir(capsys, tmp_path):
    code, _, err = run(capsys, "serve", str(tmp_path / "nope"))
    assert code == 2 and "no such directory" in err


def test_serve_mount_bad_option(capsys, tmp_path):
    """WPE9#6: ``portalkit serve`` rejects a --mount without PREFIX=DIR (exit 2, UsageError)."""
    (tmp_path / "built").mkdir()
    code, _, err = run(capsys, "serve", str(tmp_path / "built"), "--mount", "no-equals-sign")
    assert code == 2 and "--mount" in err and "PREFIX=DIR" in err


def test_serve_mount_wired_to_devserver(capsys, tmp_path, monkeypatch):
    """WPE9#6: ``portalkit serve --mount PREFIX=DIR`` passes the mount through to the
    underlying DevServer (checked via the startup line devserver.serve() prints)."""
    import portalkit.deploy.devserver as dsmod

    built = tmp_path / "built"
    built.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()

    class _Stop(Exception):
        pass

    def fake_serve_forever(self):
        raise _Stop

    monkeypatch.setattr(dsmod.DevServer, "serve_forever", fake_serve_forever)
    with pytest.raises(_Stop):
        run(capsys, "serve", str(built), "--port", "0", "--mount", f"/mnt/={foreign}")
    out = capsys.readouterr().out
    assert "mount /mnt/" in out and str(foreign) in out


# ---------------------------------------------------------------------------
# review fixes: registry dir, argument ranges, build ids, caps on foreign catalogs,
# manifold dims for legacy grammars, digest-cache writes, memo scope
# ---------------------------------------------------------------------------

def test_env_sites_dir_for_every_command(env, built, capsys):
    """Without --sites-dir, status / prune-stage resolve $PK_SITES_DIR as build does."""
    assert os.environ.get("PK_SITES_DIR") == str(env["sites"])
    code, out, err = run(capsys, "status", FAMILY, "--json")
    assert code == 0, err
    assert FAMILY in out
    code, out, err = run(capsys, "prune-stage", FAMILY, "--keep", "5", "--dry-run")
    assert code == 0, err


def test_prune_stage_negative_keep(env, capsys):
    code, _, err = run(capsys, "prune-stage", FAMILY, "--keep", "-1", "--dry-run")
    assert code == 2 and ">= 0" in err


def _family_build(env, **kw):
    import datetime as dt
    args = dict(tier=None, pages=None, limit=None, stage_root=env["stage"], allow_tmp_inputs=True,
                vendor_assets=False, jobs=1, sites_dir=env["sites"], emap=env["emap"],
                no_cache=True, now=dt.datetime(2026, 10, 10, 12, 0, 0, tzinfo=dt.timezone.utc),
                log=lambda s: None, run_check=False, thumb_px=480)
    args.update(kw)
    return buildmod._FamilyBuild(FAMILY, **args)


def test_build_ids_never_share_a_staging_dir(env):
    a = _family_build(env)
    b = _family_build(env, limit=5)
    c = _family_build(env)
    try:
        ids = {a.build_id, b.build_id, c.build_id}
        assert len(ids) == 3
        # options enter the hash: same second, different suffix
        assert a.build_id[:16] == b.build_id[:16] and a.build_id[17:] != b.build_id[17:]
        # identical options in the same second: the second build moves on one second
        assert c.build_id[17:] == a.build_id[17:] and c.build_id[:16] != a.build_id[:16]
        assert a.staging != c.staging and all(x.staging.is_dir() for x in (a, b, c))
    finally:
        for x in (a, b, c):
            x._drop_empty_staging()
    assert not any(x.staging.exists() for x in (a, b, c))


def test_caps_matrix_reads_foreign_manifests(tmp_path, monkeypatch):
    from test_gc_manifest import _gc_tree
    from portalkit.spec import CatalogSpec, ForeignRef, PageSpec
    mp = _gc_tree(tmp_path / "gc")
    spec = CatalogSpec(key="gc", table=None, id="", columns=[], title="GC field",
                       foreign=ForeignRef(kind="gc-manifest-1", url_base="https://example.org/gc/",
                                          manifest_path=mp))
    missing = CatalogSpec(key="gone", table=None, id="", columns=[],
                          foreign=ForeignRef(kind="gc-manifest-1", url_base="https://example.org/x/",
                                             manifest_path=tmp_path / "nope" / "manifest.json"))
    mans, bad = cli._foreign_manifests({"gc": spec, "gone": missing}, lambda s: None)
    assert set(mans) == {"gc"} and set(bad) == {"gone"}
    assert mans["gc"]["manifold"]["dims"] == ["f212n", "f480m"]

    class A:
        name = "fx"
        tiers = ("A",)

        def datasets(self, ctx):
            return {"gc": spec, "gone": missing}

        def pages(self, ctx, ds):
            return [PageSpec(id="gc", archetype="explorer", title="GC", datasets=["gc"]),
                    PageSpec(id="gone", archetype="explorer", title="Gone", datasets=["gone"])]
    site = type("S", (), {"adapter": "x:A", "tier_default": "A", "legacy_paths": [],
                          "portal": lambda self, pid=None: type("P", (), {"id": "main"})()})()
    monkeypatch.setattr("portalkit.context.load_site", lambda fam, d=None: site)
    monkeypatch.setattr(buildmod, "load_adapter", lambda ref: (A(), "0" * 64, "x.py"))
    rows = {r["page"]: r for r in cli.caps_matrix("fx")}
    # without the manifest the GC catalog resolves to ['multi'] only (no columns in the spec)
    assert {"sky", "num2", "mags2"} <= set(rows["gc"]["flags"]) and "unresolved" not in rows["gc"]
    assert rows["gone"]["unresolved"] == ["gone"]


def test_resolve_manifold_dims():
    mans = {"gc": {"manifold": {"dims": ["a", "b", "c"]},
                   "groups": [{"key": "mags", "label": "mags", "dims": ["b", "c"]}]}}
    layout = {"row": [{"pane": "sky"},
                      {"col": [{"pane": "manifold", "id": "m", "dataset": "gc"},
                               {"pane": "manifold", "id": "m2", "dataset": "gc",
                                "options": {"dims": "group:mags", "frame": "pca"}},
                               {"pane": "manifold", "id": "m3", "dataset": "gc",
                                "options": {"dims": ["c", "a"]}},
                               {"pane": "manifold", "id": "m4", "dataset": "other"}]}]}
    out = buildmod.resolve_manifold_dims(layout, mans)
    leaves = out["row"][1]["col"]
    assert leaves[0]["options"]["dims"] == ["a", "b", "c"]
    assert leaves[1]["options"] == {"dims": ["b", "c"], "frame": "pca"}
    assert leaves[2]["options"]["dims"] == ["c", "a"]
    assert "options" not in leaves[3]
    # the input is not modified
    assert "options" not in layout["row"][1]["col"][0]
    assert layout["row"][1]["col"][1]["options"]["dims"] == "group:mags"


def test_input_registry_writes_digest_cache_once(tmp_path, monkeypatch):
    files = []
    for i in range(5):
        f = tmp_path / f"f{i}.txt"
        f.write_text(str(i))
        files.append(f)
    reg = InputRegistry(True, cache_dir=tmp_path / "c")
    saves = []
    orig = InputRegistry._save_sha_cache
    monkeypatch.setattr(InputRegistry, "_save_sha_cache", lambda self: saves.append(1) or orig(self))
    reg.add_many(files)
    assert len(saves) == 1
    reg2 = InputRegistry(True, cache_dir=tmp_path / "c")
    reg2.add_many(files)                  # all digests cached: nothing to write
    assert len(saves) == 1
    assert len(json.loads((tmp_path / "c" / "input_sha256.json").read_text())) == 5


def test_memo_key_includes_tier_and_limit(tmp_path):
    from portalkit.context import BuildContext, SiteConfig
    f = tmp_path / "in.txt"
    f.write_text("1")
    site = SiteConfig(family="memo", adapter="x:A")
    calls = []

    def run_ctx(limit, tier="A"):
        reg = InputRegistry(True, cache_dir=tmp_path / "c")
        ctx = BuildContext(family="memo", site=site, staging=tmp_path / "s", tier=tier, limit=limit,
                           cache=Cache(tmp_path / "c", registry=reg), inputs=reg)
        return ctx.cache.memo("rows", lambda: calls.append(limit) or [limit], inputs=[f])
    assert run_ctx(50) == [50]
    assert run_ctx(None) == [None]        # a fixture-mode value is not reused
    assert run_ctx(50) == [50] and calls == [50, None]
    assert run_ctx(50, "B") == [50] and calls == [50, None, 50]


MULTI_SRC = '''
import numpy as np
from portalkit.spec import PageSpec, ReadmeSpec, catalog_from_rows


class Adapter:
    name = "climulti"
    tiers = ("A",)

    def datasets(self, ctx):
        rng = np.random.default_rng(2)
        rows = [{"id": f"M{i:03d}", "ra": 10.0 + 0.01 * i, "dec": 5.0 + 0.01 * i,
                 "f115w": 18 + rng.random(), "f200w": 17 + rng.random(),
                 "f356w": 16 + rng.random(), "f444w": 15 + rng.random()} for i in range(40)]
        cat = catalog_from_rows(rows, "id", "ra", "dec",
                                roles={k: "mag" for k in ("f115w", "f200w", "f356w", "f444w")},
                                key="src", title="Multi sources")
        return {"src": cat, "readme_a": ReadmeSpec(markdown="# A"),
                "readme_b": ReadmeSpec(markdown="# B")}

    def pages(self, ctx, ds):
        return [PageSpec(id="index", archetype="landing", title="A", datasets=["readme_a"], portal="a"),
                PageSpec(id="mani", archetype="manifold", title="M", datasets=["src"], portal="a"),
                PageSpec(id="index", archetype="landing", title="B", datasets=["readme_b"], portal="b")]


ADAPTER = Adapter()
'''


def test_multi_portal_scoping_and_manifold_dims(tmp_path):
    """Landing pages resolve against their own portal's datasets (item 21), and a page with a
    dim-indexed legacy grammar gets an explicit manifold ``options.dims`` list."""
    mod = f"pk_cli_multi_{uuid.uuid4().hex[:8]}"
    (tmp_path / "ad").mkdir()
    (tmp_path / "ad" / f"{mod}.py").write_text(MULTI_SRC)
    sites = tmp_path / "sites"
    sites.mkdir()
    portals = ""
    for pid in ("a", "b"):
        (tmp_path / f"legacy_{pid}").mkdir()
        portals += textwrap.dedent(f'''
            [[portal]]
            id = "{pid}"
            title = "Portal {pid}"
            visibility = "public"
            legacy_dir = "{tmp_path}/legacy_{pid}"
            url = "https://data.rc.ufl.edu/pub/adamginsburg/climulti/{pid}/"
            ''')
        if pid == "a":
            portals += textwrap.dedent('''
                [[portal.page]]
                id = "mani"
                grammar = ["gc-query"]
                ''')
    (sites / "climulti.toml").write_text(f'family = "climulti"\nadapter = "{mod}:ADAPTER"\n'
                                         f'tier_default = "A"\n' + portals)
    mp = pytest.MonkeyPatch()
    mp.syspath_prepend(str(tmp_path / "ad"))
    mp.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    try:
        res = buildmod.build_family("climulti", stage_root=tmp_path / "stage", sites_dir=sites,
                                    emap=ExposureMap(roots={}, url_prefixes={}),
                                    allow_tmp_inputs=True, log=lambda s: None, run_check=False)
    finally:
        mp.undo()
        sys.modules.pop(mod, None)
    errs = {k: v["errors"] for k, v in res.report["portals"].items()}
    assert res.ok, errs
    page_b = script_json(res.staging / "b" / "index.html", "pk-page")
    assert "source_list_search" not in page_b["caps"]["enabled"]
    page_a = script_json(res.staging / "a" / "index.html", "pk-page")
    assert "source_list_search" in page_a["caps"]["enabled"]
    mani = script_json(res.staging / "a" / "mani.html", "pk-page")
    assert mani["page"]["legacy"]["grammar"] == ["gc-query"]

    def panes(n):
        if isinstance(n, dict):
            for k in ("row", "col"):
                if isinstance(n.get(k), list):
                    for c in n[k]:
                        yield from panes(c)
                    return
            yield n
    leaf = next(p for p in panes(mani["layout"]) if p.get("pane") == "manifold")
    dims = leaf["options"]["dims"]
    assert isinstance(dims, list) and set(dims) >= {"f115w", "f200w", "f356w", "f444w"}


def test_related_excludes_portals_not_built_this_run(tmp_path):
    """WPE9#2: ``portal.json``'s ``related`` lists only portals the adapter actually
    assigned a page to this run. A sibling registered in the site but skipped by the
    adapter (e.g. gc_manifold's site also registers sgrb2_manifold, which the adapter
    conditionally omits) must not appear, while two portals that both got pages list each
    other."""
    mod = f"pk_cli_related_{uuid.uuid4().hex[:8]}"
    (tmp_path / "ad").mkdir()
    (tmp_path / "ad" / f"{mod}.py").write_text(MULTI_SRC)
    sites = tmp_path / "sites"
    sites.mkdir()
    portals = ""
    for pid in ("a", "b", "c"):
        (tmp_path / f"legacy_{pid}").mkdir()
        portals += textwrap.dedent(f'''
            [[portal]]
            id = "{pid}"
            title = "Portal {pid}"
            visibility = "public"
            legacy_dir = "{tmp_path}/legacy_{pid}"
            url = "https://data.rc.ufl.edu/pub/adamginsburg/climulti/{pid}/"
            ''')
    (sites / "climulti_related.toml").write_text(
        f'family = "climulti_related"\nadapter = "{mod}:ADAPTER"\ntier_default = "A"\n' + portals)
    mp = pytest.MonkeyPatch()
    mp.syspath_prepend(str(tmp_path / "ad"))
    mp.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    try:
        res = buildmod.build_family("climulti_related", stage_root=tmp_path / "stage", sites_dir=sites,
                                    emap=ExposureMap(roots={}, url_prefixes={}),
                                    allow_tmp_inputs=True, log=lambda s: None, run_check=False)
    finally:
        mp.undo()
        sys.modules.pop(mod, None)
    errs = {k: v["errors"] for k, v in res.report["portals"].items()}
    assert res.ok, errs
    # the adapter (MULTI_SRC) returns pages only for "a" and "b"; "c" is registered in the
    # site but never assigned a page this run
    doc_a = json.loads((res.staging / "a" / "portal.json").read_text())
    doc_b = json.loads((res.staging / "b" / "portal.json").read_text())
    related_a = {r["title"] for r in doc_a.get("related", [])}
    related_b = {r["title"] for r in doc_b.get("related", [])}
    assert related_a == {"Portal b"}
    assert related_b == {"Portal a"}
    assert "Portal c" not in related_a and "Portal c" not in related_b


def test_doc_file_refs_include_ids_chunks():
    """Chunked ids (note 98) and the alias file are verified like column chunks."""
    doc = {"cols": [{"key": "m", "chunk_files": ["cols/m.c000.aaaaaaaa.bin", "cols/m.c001.bbbbbbbb.bin"]}],
           "ids": {"enc": "str", "chunks": ["ids.c000.cccccccc.bin", "ids.c001.dddddddd.bin"],
                   "alias_file": "aliases.eeeeeeee.json"},
           "levels": {"chunks": ["not-a-file-ref"]}}
    refs = buildmod._doc_file_refs(doc)
    assert {"ids.c000.cccccccc.bin", "ids.c001.dddddddd.bin", "aliases.eeeeeeee.json"} <= set(refs)
    assert "cols/m.c001.bbbbbbbb.bin" in refs
    assert "not-a-file-ref" not in refs
