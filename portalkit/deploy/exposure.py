"""Symlink-graph exposure classifier (ARCHITECTURE.md section 11.2).

``scan`` walks the served roots (``web/public`` and ``web/secure``), following directory
symlinks to ``depth`` levels below each root, and records a ``realpath -> URL prefix`` entry
for every root and every symlink it meets. ``classify`` resolves a path with
``os.path.realpath`` and collects every entry whose realpath is a prefix of it: each such
entry is one way the web server can reach the path. URLs are listed longest prefix first,
so ``public_urls[0]`` / ``secure_urls[0]`` is the most specific (canonical) URL.

The policy helpers (:func:`check_target`, :func:`check_references`) implement the section
11.2 policy table and return :class:`PolicyDecision` objects; the deploy module turns a
refusal into :class:`~portalkit.deploy.ExposureRefusal` (exit 3).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import posixpath
import stat
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Literal, Mapping
from urllib.parse import quote, urlsplit

Kind = Literal["public", "secure", "both", "unserved"]

#: Default served roots (section 11.2).
DEFAULT_ROOTS: dict[str, Path] = {
    "public": Path("/orange/adamginsburg/web/public"),
    "secure": Path("/orange/adamginsburg/web/secure"),
}

#: URL prefix per root kind (section 11.2). Always ends in ``/``.
URL_PREFIXES: dict[str, str] = {
    "public": "https://data.rc.ufl.edu/pub/adamginsburg/",
    "secure": "https://data.rc.ufl.edu/secure/adamginsburg/",
}

#: ``served_by`` value for targets hosted elsewhere (GC release site).
EXTERNAL = "external"

_KINDS = ("public", "secure")
_CACHE_VERSION = 1
_MEM_CACHE: dict[str, tuple[float, "ExposureMap"]] = {}


# ---------------------------------------------------------------------------
# data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExposureEntry:
    """One way a served root reaches a real directory (or file).

    ``real`` is the resolved path, ``url`` the URL of that path (no trailing slash for the
    root entries either; :meth:`ExposureMap.match` adds the remainder), ``via`` the chain of
    ``"<link> -> <readlink text>"`` hops from the root (the root path alone for a root entry).
    """

    real: str
    kind: str
    url: str
    via: tuple[str, ...]
    is_dir: bool = True


@dataclass
class ExposureMap:
    """Result of :func:`scan`: a list of entries plus the scan parameters."""

    roots: dict[str, str]
    url_prefixes: dict[str, str]
    entries: list[ExposureEntry] = field(default_factory=list)
    depth: int = 4
    prune: tuple[str, ...] = ("*_hips",)
    scanned: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def match(self, real: str) -> list[tuple[ExposureEntry, str]]:
        """Entries whose realpath is ``real`` or a parent directory of it, longest first,
        each paired with the remainder of ``real`` below the entry (``""`` for an exact hit)."""
        out: list[tuple[ExposureEntry, str]] = []
        for ent in self.entries:
            if real == ent.real:
                out.append((ent, ""))
            elif ent.is_dir:
                base = ent.real if ent.real.endswith("/") else ent.real + "/"
                if real.startswith(base):
                    out.append((ent, real[len(base):]))
        out.sort(key=lambda t: (-len(t[0].real), t[0].kind, t[0].url))
        return out

    def to_json(self) -> dict:
        return {"version": _CACHE_VERSION, "roots": self.roots, "url_prefixes": self.url_prefixes,
                "depth": self.depth, "prune": list(self.prune), "scanned": self.scanned,
                "warnings": self.warnings,
                "entries": [dict(asdict(e), via=list(e.via)) for e in self.entries]}

    @classmethod
    def from_json(cls, obj: Mapping) -> "ExposureMap":
        if obj.get("version") != _CACHE_VERSION:
            raise ValueError("exposure cache version mismatch")
        ents = [ExposureEntry(real=e["real"], kind=e["kind"], url=e["url"],
                              via=tuple(e["via"]), is_dir=bool(e.get("is_dir", True)))
                for e in obj["entries"]]
        return cls(roots=dict(obj["roots"]), url_prefixes=dict(obj["url_prefixes"]),
                   entries=ents, depth=int(obj["depth"]), prune=tuple(obj["prune"]),
                   scanned=float(obj["scanned"]), warnings=list(obj.get("warnings", [])))


@dataclass
class Exposure:
    """Classification of one path (section 11.2)."""

    kind: Kind
    public_urls: list[str]
    secure_urls: list[str]
    via: list[str]                     # symlink chain(s) that expose the path

    @property
    def served(self) -> bool:
        return self.kind != "unserved"

    @property
    def public_via(self) -> list[str]:
        return [v for v in self.via if v.startswith("public: ")]

    @property
    def secure_via(self) -> list[str]:
        return [v for v in self.via if v.startswith("secure: ")]


@dataclass
class PolicyDecision:
    """Outcome of a policy check: ``level`` is ``ok``, ``warn`` or ``refuse``."""

    level: Literal["ok", "warn", "refuse"]
    message: str = ""
    exit_code: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.level != "refuse"


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def _norm_roots(roots: Mapping[str, os.PathLike | str]) -> dict[str, str]:
    out = {}
    for kind, root in roots.items():
        if kind not in _KINDS:
            raise ValueError(f"root kind must be one of {_KINDS}, got {kind!r}")
        out[kind] = os.path.abspath(os.fspath(root))
    return out


def _cache_key(roots: dict[str, str], prefixes: dict[str, str], depth: int,
               prune: tuple[str, ...]) -> str:
    blob = json.dumps([sorted(roots.items()), sorted(prefixes.items()), depth, list(prune),
                       _CACHE_VERSION], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _cache_dir() -> Path:
    env = os.environ.get("PK_CACHE_DIR")
    if env:
        return Path(env)
    return Path(os.path.expanduser("~")) / ".cache" / "portalkit"


def _join_url(prefix: str, rel: str) -> str:
    if not rel:
        return prefix.rstrip("/")
    return prefix.rstrip("/") + "/" + quote(rel, safe="/-_.~")


def _scan_threads() -> int:
    try:
        return max(1, int(os.environ.get("PK_SCAN_THREADS", "16")))
    except ValueError:
        return 16


def _list_dir(logical: str, prune: tuple[str, ...]) -> tuple[list[tuple], list[str]]:
    """List one directory for :func:`_walk_root`.

    Returns ``(children, warnings)``; children are ``("dir", name, path)`` or
    ``("link", name, path, realpath, link_text)`` sorted by name. Runs in a worker thread:
    on a network filesystem the walk is dominated by directory listings, which release the
    GIL, so listing a level's directories concurrently shortens the scan without changing
    its result.
    """
    warnings: list[str] = []
    try:
        with os.scandir(logical) as it:
            dents = sorted(it, key=lambda de: de.name)
    except OSError as exc:
        return [], [f"cannot list {logical}: {exc.strerror or exc}"]
    out: list[tuple] = []
    for de in dents:
        name = de.name
        if any(fnmatch.fnmatch(name, pat) for pat in prune):
            continue
        try:
            is_link = de.is_symlink()
        except OSError:
            continue
        if is_link:
            # only directory symlinks are mapped (section 11.2); one stat() tells dangling
            # links and file links apart before the costlier realpath()
            try:
                st = os.stat(de.path)
            except OSError:
                warnings.append(f"dangling symlink {de.path}")
                continue
            if not stat.S_ISDIR(st.st_mode):
                continue
            try:
                link_text = os.readlink(de.path)
            except OSError as exc:
                warnings.append(f"cannot read link {de.path}: {exc.strerror or exc}")
                continue
            out.append(("link", name, de.path, os.path.realpath(de.path), link_text))
        else:
            try:
                is_dir = de.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                out.append(("dir", name, de.path))
    return out, warnings


def _walk_root(kind: str, root: str, prefix: str, depth: int, prune: tuple[str, ...],
               emap: ExposureMap) -> None:
    root_real = os.path.realpath(root)
    if not os.path.isdir(root_real):
        emap.warnings.append(f"{kind} root {root} is not a directory")
        return
    emap.entries.append(ExposureEntry(real=root_real, kind=kind, url=_join_url(prefix, ""),
                                      via=(f"{kind}: {root}",)))
    # Level-synchronous breadth-first walk: every directory at depth d is listed (in
    # parallel) before any at depth d+1, so a symlink target is expanded along its shortest
    # route from the root and the walk covers everything within ``depth`` of it. Results are
    # consumed in a fixed order (level by level, each level in discovery order), so the
    # entry list does not depend on thread timing.
    expanded: set[str] = {root_real}
    # level items: (logical path, rel url path, chain, ancestor realpaths)
    level: list[tuple[str, str, tuple[str, ...], frozenset]] = [
        (root, "", (f"{kind}: {root}",), frozenset([root_real]))]
    d = 0
    with ThreadPoolExecutor(max_workers=_scan_threads()) as pool:
        while level and d < depth:
            listings = pool.map(lambda item: _list_dir(item[0], prune), level)
            nxt: list[tuple[str, str, tuple[str, ...], frozenset]] = []
            for (logical, rel, chain, ancestors), (children, warns) in zip(level, listings):
                emap.warnings.extend(warns)
                for child in children:
                    name, path = child[1], child[2]
                    child_rel = f"{rel}/{name}" if rel else name
                    if child[0] == "dir":
                        nxt.append((path, child_rel, chain, ancestors))
                        continue
                    target, link_text = child[3], child[4]
                    hop = chain + (f"{path} -> {link_text}",)
                    emap.entries.append(ExposureEntry(real=target, kind=kind,
                                                      url=_join_url(prefix, child_rel), via=hop))
                    if target not in ancestors and target not in expanded:
                        expanded.add(target)
                        nxt.append((path, child_rel, hop, ancestors | {target}))
            level = nxt
            d += 1


def scan(roots: Mapping[str, os.PathLike | str] | None = None, depth: int = 4,
         prune: tuple[str, ...] = ("*_hips",), cache_s: int = 3600,
         url_prefixes: Mapping[str, str] | None = None) -> ExposureMap:
    """Walk the served roots and build the ``realpath -> URL prefix`` map.

    ``roots`` maps ``"public"``/``"secure"`` to a directory (default :data:`DEFAULT_ROOTS`).
    Directory symlinks are followed to ``depth`` levels below each root; entries whose name
    matches a ``prune`` glob are skipped (HiPS trees are huge and never hold symlinks to
    other trees). Each level's directories are listed by ``$PK_SCAN_THREADS`` worker threads
    (default 16); the result does not depend on the thread count. Results are cached in
    memory and under ``~/.cache/portalkit`` (or ``$PK_CACHE_DIR``) for ``cache_s`` seconds;
    ``cache_s=0`` disables both caches.
    """
    roots_n = _norm_roots(DEFAULT_ROOTS if roots is None else roots)
    prefixes = dict(URL_PREFIXES)
    if url_prefixes:
        prefixes.update(url_prefixes)
    prefixes = {k: (v if v.endswith("/") else v + "/") for k, v in prefixes.items()}
    prune = tuple(prune)
    key = _cache_key(roots_n, prefixes, depth, prune)
    now = time.time()
    if cache_s > 0:
        hit = _MEM_CACHE.get(key)
        if hit is not None and now - hit[0] < cache_s:
            return hit[1]
        cpath = _cache_dir() / f"exposure-{key}.json"
        try:
            with open(cpath, encoding="utf-8") as fh:
                cached = ExposureMap.from_json(json.load(fh))
            if now - cached.scanned < cache_s:
                _MEM_CACHE[key] = (cached.scanned, cached)
                return cached
        except (OSError, ValueError, KeyError, TypeError):
            pass
    emap = ExposureMap(roots=roots_n, url_prefixes=prefixes, depth=depth, prune=prune,
                       scanned=now)
    for kind in _KINDS:
        if kind in roots_n:
            _walk_root(kind, roots_n[kind], prefixes[kind], depth, prune, emap)
    if cache_s > 0:
        _MEM_CACHE[key] = (now, emap)
        _write_cache(_cache_dir() / f"exposure-{key}.json", emap)
    return emap


def _write_cache(path: Path, emap: ExposureMap) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".pk-tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(emap.to_json(), fh)
        os.replace(tmp, path)
    except OSError:
        # the cache is an optimisation only
        pass


def clear_cache() -> None:
    """Forget in-memory scan results (the on-disk cache expires by age)."""
    _MEM_CACHE.clear()


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

def classify(path: os.PathLike | str, emap: ExposureMap | None = None) -> Exposure:
    """Classify ``path`` as ``public``, ``secure``, ``both`` or ``unserved``.

    ``path`` need not exist (deploy targets are created on first deploy); its existing
    ancestors are resolved through symlinks.
    """
    if emap is None:
        emap = scan()
    real = os.path.realpath(os.fspath(path))
    trailing = not os.path.isfile(real)
    urls: dict[str, list[str]] = {"public": [], "secure": []}
    via: list[str] = []
    for ent, rest in emap.match(real):
        url = _join_url(ent.url + "/", rest) if rest else ent.url
        if trailing and ent.is_dir and not url.endswith("/"):
            url += "/"
        if url not in urls[ent.kind]:
            urls[ent.kind].append(url)
        hops = [ent.via[0].split(": ", 1)[1]] + list(ent.via[1:])
        chain = f"{ent.kind}: " + " => ".join(hops)
        if chain not in via:
            via.append(chain)
    if urls["public"] and urls["secure"]:
        kind: Kind = "both"
    elif urls["public"]:
        kind = "public"
    elif urls["secure"]:
        kind = "secure"
    else:
        kind = "unserved"
    return Exposure(kind=kind, public_urls=urls["public"], secure_urls=urls["secure"], via=via)


def classify_url(url: str, emap: ExposureMap | None = None) -> str:
    """Classify a URL string: ``public`` / ``secure`` (by the served URL prefixes),
    ``external`` (another host or path) or ``relative``.

    A root-relative URL (``/secure/adamginsburg/x.bin``) is resolved against the served
    host, i.e. matched against the path part of each URL prefix."""
    prefixes = emap.url_prefixes if emap is not None else URL_PREFIXES
    parts = urlsplit(url)
    if not parts.scheme and url.startswith("/") and not url.startswith("//"):
        for kind in _KINDS:
            ppath = urlsplit(prefixes.get(kind, URL_PREFIXES[kind])).path
            if ppath and (parts.path.startswith(ppath) or parts.path == ppath.rstrip("/")):
                return kind
        return "external"
    if not parts.scheme and not url.startswith("//"):
        return "relative"
    if url.startswith("//"):
        url = "https:" + url
    for kind in _KINDS:
        p = prefixes.get(kind, URL_PREFIXES[kind])
        if url.startswith(p) or url == p.rstrip("/"):
            return kind
    return "external"


# ---------------------------------------------------------------------------
# policy (section 11.2)
# ---------------------------------------------------------------------------

def check_target(visibility: str, exposure: Exposure, served_by: str | None = None,
                 yes: bool = False) -> PolicyDecision:
    """Apply the section 11.2 policy to a deploy target.

    * secure portal, target ``public`` or ``both`` -> refuse (exit 3), naming the public chain;
    * public portal, target ``secure`` only -> warn; allowed only with ``served_by="external"``
      or ``yes``, refused otherwise;
    * ``unserved`` -> refuse unless ``served_by="external"``.
    """
    external = (served_by == EXTERNAL)
    if visibility not in ("public", "secure"):
        return PolicyDecision("refuse", f"unknown visibility {visibility!r}", 3)
    if exposure.kind == "unserved":
        if external:
            return PolicyDecision("ok", "target is not served by data.rc.ufl.edu "
                                  "(served_by = external)")
        return PolicyDecision("refuse", "target is not reachable from any served root "
                              "(set served_by = \"external\" for an externally hosted site)", 3)
    if visibility == "secure" and exposure.kind in ("public", "both"):
        chains = exposure.public_via or exposure.via
        return PolicyDecision(
            "refuse", "secure portal target is publicly exposed via "
            + "; ".join(chains) + " (public URL " + (exposure.public_urls or ["?"])[0] + ")", 3,
            notes=chains)
    if visibility == "public" and exposure.kind == "secure":
        msg = ("public portal target is reachable only under secure (" +
               (exposure.secure_urls or ["?"])[0] + ")")
        if external or yes:
            return PolicyDecision("warn", msg)
        return PolicyDecision("refuse", msg + "; pass --yes or set served_by = \"external\"", 3)
    if visibility == "public" and exposure.kind == "both":
        return PolicyDecision("ok", "notice: target is also reachable under secure ("
                              + (exposure.secure_urls or ["?"])[0] + ")",
                              notes=exposure.secure_via)
    return PolicyDecision("ok")


@dataclass
class Reference:
    """A URL found in a staged page or data document."""

    url: str
    source: str               # staged file the URL came from
    fetched: bool = True       # False for plain links (``link_only``, ``related``, products)
    resolved: str | None = None  # filesystem path for relative URLs that leave the target


def check_references(visibility: str, refs: Iterable[Reference],
                     emap: ExposureMap | None = None) -> PolicyDecision:
    """Section 11.2 reference policy for a page of the given visibility.

    For a public page: a fetched reference to secure data refuses (exit 3); a link to a
    secure URL warns (the viewer renders a "login required" badge). Secure pages may
    reference anything.
    """
    if visibility != "public":
        return PolicyDecision("ok")
    refusals: list[str] = []
    warns: list[str] = []
    for ref in refs:
        kind = classify_url(ref.url, emap)
        if kind == "relative":
            if ref.resolved is None:
                continue
            exp = classify(ref.resolved, emap)
            if exp.kind == "secure":
                kind = "secure"
            elif exp.kind == "unserved":
                warns.append(f"{ref.source}: {ref.url} resolves to an unserved path")
                continue
            else:
                continue
        if kind != "secure":
            continue
        if ref.fetched:
            refusals.append(f"{ref.source}: fetches secure data {ref.url}")
        else:
            warns.append(f"{ref.source}: links to secure URL {ref.url} (login required)")
    if refusals:
        return PolicyDecision("refuse", "public page references secure data "
                              "(mark the reference link_only if it is a plain link)", 3,
                              notes=refusals + warns)
    if warns:
        return PolicyDecision("warn", f"{len(warns)} link(s) to secure URLs", notes=warns)
    return PolicyDecision("ok")


def relative_ref_path(base_dir: os.PathLike | str, url: str) -> str:
    """Filesystem path a relative URL resolves to from ``base_dir`` (query/fragment dropped)."""
    part = urlsplit(url).path
    return os.path.normpath(os.path.join(os.fspath(base_dir), *posixpath.normpath(part).split("/")))
