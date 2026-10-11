#!/usr/bin/env python
"""Paired screenshots of legacy and unified pages, written into one review HTML page.

Usage::

    python tools/contact_sheet.py --out /path/to/review \\
        --pair w51/map.html https://.../w51/portal/map.html http://127.0.0.1:8765/w51/map.html \\
        --pair ...
    python tools/contact_sheet.py --out /path/to/review --pairs pairs.json

``pairs.json`` is a list of ``{"name", "legacy", "unified"}`` objects (``note`` optional) or of
``[name, legacy, unified]`` triples. ``legacy`` / ``unified`` are URLs or local HTML paths.
A local legacy page is opened as a ``file://`` URL. A local unified page is an ES-module app,
which Chromium does not load from ``file://``, so it is served over HTTP by
``portalkit.deploy.devserver.DevServer`` (port 0) rooted at ``--serve-root``, by default the
parent of the nearest directory holding ``portal.json`` (so shared ``res/`` and ``_pk/`` trees
next to the portal resolve), else the page's directory. Unified URLs get ``pktest=1`` so the
tool can wait for ``__pk.ready`` before the shot.
When ``name`` matches a ``key`` (``family/legacy_page``) or a ``legacy_page`` of
``tests/data/port_table.json``, the review page shows that row's archetype, capabilities to
fill and special handling next to the pair.

The output directory must not lie under a live portal or legacy generator tree (``LIVE_ROOTS``:
the web tree, the W51, SPHEREx and Robitaille portals, the brick analysis tree and the NIRSpec
archive galleries), or under any directory holding a ``portal.json`` that the synthetic
fixtures did not write. Screenshots are taken with headless Chromium (SwiftShader
flags, as in the e2e harness).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PORT_TABLE = REPO / "tests" / "data" / "port_table.json"
#: live portal trees and legacy generator / portal roots (docs/inventory.json); never written
LIVE_ROOTS = (
    Path("/orange/adamginsburg/web"),
    Path("/orange/adamginsburg/w51/portal"),
    Path("/orange/adamginsburg/spherex"),
    Path("/orange/adamginsburg/robitaille_models/portal"),
    Path("/orange/adamginsburg/jwst/brick"),
    Path("/orange/adamginsburg/jwst/brick/analysis"),    # a symlink into the brick repo checkout
    Path("/orange/adamginsburg/jwst/spectra/mastDownload"),
)
#: SwiftShader WebGL; the 2D canvas also runs in software so screenshot readback does not wait
#: on GPU-process canvas copies (same flags as tests/conftest_browser.py)
CHROMIUM_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--disable-accelerated-2d-canvas"]


@dataclass
class Pair:
    name: str
    legacy: str
    unified: str
    note: str = ""
    shots: dict = field(default_factory=dict)     # side -> file name or None
    errors: dict = field(default_factory=dict)    # side -> message


def _within(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
    except ValueError:
        return False
    return True


def check_out_dir(out: Path) -> Path:
    """Resolve ``out`` and refuse live portal locations (raises ``ValueError``)."""
    given = Path(out).expanduser().absolute()
    out = given.resolve()
    for root in LIVE_ROOTS:
        if any(_within(o, r) for o in (given, out) for r in (root, root.resolve())):
            raise ValueError(f"refusing to write under the live portal tree {root}: {out}")
    for d in (out, *out.parents):
        pj = d / "portal.json"
        if pj.is_file() and not (d / ".synthetic.json").is_file():
            raise ValueError(f"refusing to write inside a portal directory ({pj})")
    return out


_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def is_url(s: str) -> bool:
    return bool(_SCHEME.match(s))


def to_url(s: str) -> str:
    """URL for a legacy page: URLs unchanged, local paths as ``file://``."""
    if is_url(s):
        return s
    p = Path(s).expanduser().resolve()
    if not p.exists():
        raise ValueError(f"not a URL and no such file: {s}")
    return p.as_uri()


def with_pktest(url: str) -> str:
    """``url`` with ``pktest=1`` in its query (existing query and fragment kept)."""
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "pktest" for k, _ in q):
        q.append(("pktest", "1"))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q, safe="/:,~")))


def serve_root_for(page: Path) -> Path:
    """Default DevServer root of a local unified page (see the module docstring)."""
    for d in page.parents:
        if (d / "portal.json").is_file():
            return d.parent
    return page.parent


class Servers:
    """One DevServer per root directory, for local unified pages."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root).expanduser().resolve() if root else None
        self.running: dict[Path, object] = {}

    def url_for(self, s: str) -> str:
        """HTTP URL (with ``pktest=1``) of a unified page given as a URL or a local path; a
        ``#fragment`` or ``?query`` after a local path is kept."""
        if is_url(s):
            return with_pktest(s)
        path, sep, frag = s.partition("#")
        path, qsep, query = path.partition("?")
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            raise ValueError(f"not a URL and no such file: {s}")
        root = self.root or serve_root_for(p)
        try:
            rel = p.relative_to(root)
        except ValueError:
            raise ValueError(f"{p} is not under the serve root {root}") from None
        if root not in self.running:
            if str(REPO) not in sys.path:
                sys.path.insert(0, str(REPO))     # run from a checkout without an install
            from portalkit.deploy.devserver import DevServer
            self.running[root] = DevServer(root).start()
        base = self.running[root].url
        url = base + urllib.parse.quote(rel.as_posix()) + (qsep + query) + (sep + frag)
        return with_pktest(url)

    def close(self) -> None:
        for srv in self.running.values():
            srv.stop()
        self.running.clear()


def load_pairs(args: argparse.Namespace) -> list[Pair]:
    pairs: list[Pair] = []
    if args.pairs:
        doc = json.loads(Path(args.pairs).read_text(encoding="utf-8"))
        for item in doc:
            if isinstance(item, dict):
                pairs.append(Pair(str(item["name"]), str(item["legacy"]), str(item["unified"]),
                                  str(item.get("note", ""))))
            elif isinstance(item, (list, tuple)) and len(item) >= 3:
                pairs.append(Pair(str(item[0]), str(item[1]), str(item[2])))
            else:
                raise ValueError(f"bad pair entry: {item!r}")
    for name, legacy, unified in args.pair or []:
        pairs.append(Pair(name, legacy, unified))
    if not pairs:
        raise ValueError("no pairs given (--pair NAME LEGACY UNIFIED or --pairs FILE)")
    return pairs


def port_rows(path: Path = PORT_TABLE) -> dict[str, dict]:
    """Port-table rows keyed by ``key`` and by ``legacy_page`` (first row wins)."""
    if not path.is_file():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r.get("key", f"{r['family']}/{r['legacy_page']}"), r)
        out.setdefault(r["legacy_page"], r)
    return out


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:80] or "pair"


def shoot(pairs: list[Pair], out: Path, *, width: int, height: int, wait_ms: int, full_page: bool,
          timeout_ms: int, serve_root: Path | None = None) -> None:
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import sync_playwright

    img_dir = out / "img"
    img_dir.mkdir(parents=True, exist_ok=True)
    servers = Servers(serve_root)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        try:
            for k, pr in enumerate(pairs):
                for side in ("legacy", "unified"):
                    fname = f"{k:03d}_{slug(pr.name)}_{side}.png"
                    ctx = browser.new_context(viewport={"width": width, "height": height})
                    try:
                        page = ctx.new_page()
                        src = getattr(pr, side)
                        url = servers.url_for(src) if side == "unified" else to_url(src)
                        page.goto(url, wait_until="load", timeout=timeout_ms)
                        if side == "unified":
                            # testhooks.js installs window.__pk only with ?pktest=1 (added above)
                            page.evaluate("""async (ms) => {
                                if (!window.__pk || !window.__pk.ready) return;
                                const t = new Promise((r) => setTimeout(r, ms));
                                await Promise.race([window.__pk.ready.catch(() => {}), t]);
                                // then until no visible pane is still loading (or the timeout)
                                const t0 = performance.now();
                                const busy = () => Array.from(document.querySelectorAll('section.pk-pane'))
                                  .some((s) => s.offsetParent !== null && s.dataset.state === 'loading');
                                while (busy() && performance.now() - t0 < ms) {
                                  await new Promise((r) => setTimeout(r, 100));
                                }
                            }""", timeout_ms)
                        page.wait_for_timeout(wait_ms)
                        page.screenshot(path=str(img_dir / fname), full_page=full_page, timeout=timeout_ms)
                        pr.shots[side] = f"img/{fname}"
                    except (PWError, ValueError, OSError) as err:
                        pr.shots[side] = None
                        pr.errors[side] = str(err).splitlines()[0][:300]
                    finally:
                        ctx.close()
        finally:
            browser.close()
            servers.close()


def render(pairs: list[Pair], rows: dict[str, dict], title: str) -> str:
    esc = html.escape
    cards = []
    for pr in pairs:
        row = rows.get(pr.name)
        meta = []
        if row:
            meta.append(f"<p class='arch'>{esc(row.get('archetype', ''))}</p>")
            fills = row.get("capabilities_to_fill") or []
            if fills:
                meta.append("<p class='fill'><b>To fill:</b> " + ", ".join(esc(c) for c in fills) + "</p>")
            if row.get("special_handling"):
                meta.append(f"<p class='note'>{esc(row['special_handling'])}</p>")
        if pr.note:
            meta.append(f"<p class='note'>{esc(pr.note)}</p>")
        sides = []
        for side in ("legacy", "unified"):
            url = getattr(pr, side)
            shot = pr.shots.get(side)
            if shot:
                body = f"<a href='{esc(shot)}'><img loading='lazy' src='{esc(shot)}' alt='{esc(side)} screenshot'></a>"
            else:
                body = f"<div class='missing'>{esc(pr.errors.get(side, 'not captured'))}</div>"
            sides.append(f"<figure><figcaption>{esc(side)} &middot; <a href='{esc(url)}'>{esc(url)}</a>"
                         f"</figcaption>{body}</figure>")
        cards.append(f"<section class='pair' id='{esc(slug(pr.name))}'><h2>{esc(pr.name)}</h2>"
                     + "".join(meta) + "<div class='sides'>" + "".join(sides) + "</div></section>")
    toc = "".join(f"<li><a href='#{esc(slug(p.name))}'>{esc(p.name)}</a></li>" for p in pairs)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
:root {{ --bg:#fafaf8; --fg:#1d1d1b; --muted:#666; --line:#ddd; --card:#fff; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#16171a; --fg:#e8e6e1; --muted:#a0a0a0; --line:#333; --card:#1f2024; }} }}
body {{ margin:0; padding:16px; background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; }}
h1 {{ font-size:20px; margin:0 0 8px; }}
ol.toc {{ columns: 3 16em; color:var(--muted); }}
.pair {{ background:var(--card); border:1px solid var(--line); border-radius:6px; padding:12px; margin:16px 0; }}
.pair h2 {{ font-size:16px; margin:0 0 4px; }}
.arch {{ color:var(--muted); margin:0; }}
.fill, .note {{ margin:4px 0; font-size:13px; }}
.sides {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 420px), 1fr)); gap:12px; margin-top:8px; }}
figure {{ margin:0; min-width:0; }}
figcaption {{ font-size:12px; color:var(--muted); overflow-wrap:anywhere; margin-bottom:4px; }}
img {{ width:100%; height:auto; border:1px solid var(--line); }}
.missing {{ padding:24px; border:1px dashed var(--line); color:var(--muted); }}
</style></head>
<body>
<h1>{esc(title)}</h1>
<p>{len(pairs)} pairs. Left: legacy page. Right: unified page.</p>
<ol class="toc">{toc}</ol>
{''.join(cards)}
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="output directory (index.html + img/)")
    ap.add_argument("--pair", nargs=3, action="append", metavar=("NAME", "LEGACY", "UNIFIED"))
    ap.add_argument("--pairs", type=Path, help="JSON file with pairs")
    ap.add_argument("--title", default="Legacy vs unified contact sheet")
    ap.add_argument("--viewport", default="1400x900", help="WIDTHxHEIGHT")
    ap.add_argument("--wait-ms", type=int, default=1500, help="extra settle time before each shot")
    ap.add_argument("--timeout-ms", type=int, default=180000)
    ap.add_argument("--full-page", action="store_true")
    ap.add_argument("--no-shots", action="store_true", help="write the HTML only (no browser)")
    ap.add_argument("--serve-root", type=Path, default=None,
                    help="DevServer root for local unified pages (default: parent of the portal root)")
    a = ap.parse_args(argv)
    try:
        out = check_out_dir(a.out)
        pairs = load_pairs(a)
        m = re.fullmatch(r"(\d+)x(\d+)", a.viewport)
        if not m:
            raise ValueError(f"bad --viewport {a.viewport!r}")
    except ValueError as err:
        print(f"contact_sheet: {err}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    if not a.no_shots:
        shoot(pairs, out, width=int(m.group(1)), height=int(m.group(2)), wait_ms=a.wait_ms,
              full_page=a.full_page, timeout_ms=a.timeout_ms, serve_root=a.serve_root)
    (out / "index.html").write_text(render(pairs, port_rows(), a.title), encoding="utf-8")
    bad = sum(1 for p in pairs for s in ("legacy", "unified") if not p.shots.get(s)) if not a.no_shots else 0
    print(f"wrote {out / 'index.html'} ({len(pairs)} pairs, {bad} missing shots)")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["Pair", "Servers", "check_out_dir", "load_pairs", "port_rows", "render", "serve_root_for", "shoot",
           "main", "to_url", "with_pktest"]
