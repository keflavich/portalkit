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

import collections
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
#: Cache file format: 2 adds the per-directory stamps (``dirs``) used to revalidate a scan;
#: 3 adds ``st_ctime_ns`` to each stamp.
_CACHE_VERSION = 3
#: Part of the cache key; unchanged by formats 2 and 3, so an older file is simply not read.
_KEY_VERSION = 1
#: A directory stamp taken within this many seconds of the directory's mtime or ctime is not
#: reused.
_RACY_S = 5.0
_MEM_CACHE: dict[str, tuple[float, "ExposureMap"]] = {}
_MEM_STAMPS: dict[str, dict] = {}


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
    #: call counts and wall time of the walk that produced this map (not cached)
    stats: dict = field(default_factory=dict, compare=False)

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
                       _KEY_VERSION], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _default_cache_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".cache" / "portalkit"


def _cache_dir() -> Path:
    """Where scan results are written: ``$PK_CACHE_DIR``, else ``~/.cache/portalkit``."""
    env = os.environ.get("PK_CACHE_DIR")
    if env:
        return Path(env)
    return _default_cache_dir()


def _cache_dirs() -> list[Path]:
    """Where scan results are read from: :func:`_cache_dir`, then (read-only) the default
    ``~/.cache/portalkit`` when ``$PK_CACHE_DIR`` points elsewhere, so a run with an isolated
    cache directory (adapter tests) starts from the user's last scan of the same roots."""
    out = [_cache_dir()]
    d = _default_cache_dir()
    if os.path.abspath(d) != os.path.abspath(out[0]):
        out.append(d)
    return out


def _join_url(prefix: str, rel: str) -> str:
    if not rel:
        return prefix.rstrip("/")
    return prefix.rstrip("/") + "/" + quote(rel, safe="/-_.~")


def _scan_threads() -> int:
    try:
        return max(1, int(os.environ.get("PK_SCAN_THREADS", "16")))
    except ValueError:
        return 16


def _valid_stamp(old: object) -> list | None:
    """A copy of a cached directory stamp, or ``None`` when it is malformed (the cache file
    is an optimisation; a bad stamp only costs a ``scandir``)."""
    if not (isinstance(old, list) and len(old) == 6):
        return None
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in old[:4]):
        return None
    if not isinstance(old[4], (int, float)) or isinstance(old[4], bool):
        return None
    kids = old[5]
    if not isinstance(kids, list):
        return None
    for x in kids:
        if not (isinstance(x, list) and len(x) == 2 and isinstance(x[0], str) and x[0]
                and "/" not in x[0] and x[0] not in (".", "..") and x[1] in ("d", "l")):
            return None
    return [*old[:5], [list(x) for x in kids]]


class _Listings:
    """Directory listings of one scan, shared by every route and both roots.

    A directory is listed at most once per scan, keyed by its real path, however many
    symlink routes (and roots) reach it; each use re-renders the warnings with the route's
    own logical path, so the result equals listing the directory once per route.

    ``stamps`` (from an earlier scan's cache file) maps a real directory to
    ``[st_dev, st_ino, st_mtime_ns, st_ctime_ns, listed_at, children]`` where ``children``
    are the ``[name, "d"|"l"]`` sub-directories and symlinks of that listing. A directory
    whose ``stat`` still gives the same device, inode, mtime and ctime reuses those names
    without a ``scandir``. Adding, removing or renaming an entry (a symlink included) updates
    the directory ctime, and so do ``chmod``/``chown`` (a directory made unlistable) and
    ``utime`` (an mtime restored with ``touch -r``); users cannot set ctime. A stamp taken
    less than :data:`_RACY_S` seconds after the directory's mtime or ctime is not trusted
    (coarse timestamp granularity), and symlinks are always resolved again (``stat``,
    ``readlink``, ``realpath``), because a link's target can change without its directory
    changing. A malformed stamp is ignored. ``stamps=None`` disables reuse and recording
    (``cache_s=0``).
    """

    def __init__(self, stamps: Mapping | None, record: bool) -> None:
        self.stamps = stamps or {}
        self.record = record
        self.mem: dict[str, tuple[list[tuple], list[tuple]]] = {}
        self.out: dict[str, list] = {}
        self.counts: collections.Counter = collections.Counter()

    def list_real(self, real: str, logical: str) -> tuple[list[tuple], list[tuple],
                                                           list | None, collections.Counter]:
        """List one directory (runs in a worker thread; returns its own counters).

        Returns ``(children, rel_warnings, stamp, counts)``: children are ``("dir", name)``
        or ``("link", name, realpath, link_text)`` sorted by name (only directory symlinks;
        prune globs are applied by the caller), warnings are ``(kind, name, text)``."""
        c: collections.Counter = collections.Counter()
        names: list[list[str]] | None = None
        stamp: list | None = None
        st = None
        if self.record or real in self.stamps:
            c["stat"] += 1
            try:
                st = os.stat(logical)
            except OSError:
                st = None             # the scandir below reports the error
            old = _valid_stamp(self.stamps.get(real))
            if (st is not None and old is not None and old[0] == st.st_dev
                    and old[1] == st.st_ino and old[2] == st.st_mtime_ns
                    and old[3] == st.st_ctime_ns
                    and old[4] - max(st.st_mtime_ns, st.st_ctime_ns) / 1e9 >= _RACY_S):
                names = [list(x) for x in old[5]]
                stamp = old
                c["reused"] += 1
        if names is None:
            listed_at = time.time()
            c["scandir"] += 1
            try:
                with os.scandir(logical) as it:
                    dents = sorted(it, key=lambda de: de.name)
            except OSError as exc:
                return [], [("list", "", exc.strerror or str(exc))], None, c
            c["dirents"] += len(dents)
            names = []
            for de in dents:
                try:
                    if de.is_symlink():
                        names.append([de.name, "l"])
                    elif de.is_dir(follow_symlinks=False):
                        names.append([de.name, "d"])
                except OSError:
                    continue
            if st is not None:
                stamp = [st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, listed_at,
                         names]
        children: list[tuple] = []
        warns: list[tuple] = []
        for name, typ in names:
            if typ == "d":
                children.append(("dir", name))
                continue
            path = os.path.join(logical, name)
            # only directory symlinks are mapped (section 11.2); one stat() tells dangling
            # links and file links apart before the costlier realpath()
            c["stat"] += 1
            try:
                lst = os.stat(path)
            except OSError:
                warns.append(("dangling", name, ""))
                continue
            if not stat.S_ISDIR(lst.st_mode):
                continue
            c["readlink"] += 1
            try:
                link_text = os.readlink(path)
            except OSError as exc:
                warns.append(("readlink", name, exc.strerror or str(exc)))
                continue
            c["realpath"] += 1
            children.append(("link", name, os.path.realpath(path), link_text))
        return children, warns, stamp, c

    def run_level(self, pool: ThreadPoolExecutor, items: list[tuple[str, str]]) -> None:
        """List every ``(real, logical)`` not listed yet in this scan, concurrently."""
        todo: list[tuple[str, str]] = []
        seen: set[str] = set()
        for real, logical in items:
            if real not in self.mem and real not in seen:
                seen.add(real)
                todo.append((real, logical))
        self.counts["shared"] += len(items) - len(todo)
        for (real, _), (children, warns, stamp, c) in zip(
                todo, pool.map(lambda it: self.list_real(*it), todo)):
            self.mem[real] = (children, warns)
            self.counts.update(c)
            if stamp is not None:
                self.out[real] = stamp

    def get(self, real: str, logical: str, prune: tuple[str, ...]) -> tuple[list[tuple],
                                                                            list[str]]:
        """Children (pruned) and warnings of ``real`` as seen through ``logical``."""
        children, rel = self.mem[real]
        warnings = []
        for kind, name, text in rel:
            if kind == "list":
                warnings.append(f"cannot list {logical}: {text}")
            elif kind == "dangling":
                if not any(fnmatch.fnmatch(name, pat) for pat in prune):
                    warnings.append(f"dangling symlink {os.path.join(logical, name)}")
            elif not any(fnmatch.fnmatch(name, pat) for pat in prune):
                warnings.append(f"cannot read link {os.path.join(logical, name)}: {text}")
        kids = [ch for ch in children if not any(fnmatch.fnmatch(ch[1], pat) for pat in prune)]
        return kids, warnings


def _walk_root(kind: str, root: str, prefix: str, depth: int, prune: tuple[str, ...],
               emap: ExposureMap, listings: _Listings, pool: ThreadPoolExecutor) -> None:
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
    # entry list does not depend on thread timing. Each item carries the real path of its
    # directory (a symlink's realpath, or the parent's real path plus the name), which keys
    # the shared listings.
    expanded: set[str] = {root_real}
    # level items: (logical path, real path, rel url path, chain, ancestor realpaths)
    level: list[tuple[str, str, str, tuple[str, ...], frozenset]] = [
        (root, root_real, "", (f"{kind}: {root}",), frozenset([root_real]))]
    d = 0
    while level and d < depth:
        listings.run_level(pool, [(item[1], item[0]) for item in level])
        nxt: list[tuple[str, str, str, tuple[str, ...], frozenset]] = []
        for logical, real, rel, chain, ancestors in level:
            children, warns = listings.get(real, logical, prune)
            emap.warnings.extend(warns)
            for child in children:
                name = child[1]
                path = os.path.join(logical, name)
                child_rel = f"{rel}/{name}" if rel else name
                if child[0] == "dir":
                    nxt.append((path, os.path.join(real, name), child_rel, chain, ancestors))
                    continue
                target, link_text = child[2], child[3]
                hop = chain + (f"{path} -> {link_text}",)
                emap.entries.append(ExposureEntry(real=target, kind=kind,
                                                  url=_join_url(prefix, child_rel), via=hop))
                if target not in ancestors and target not in expanded:
                    expanded.add(target)
                    nxt.append((path, target, child_rel, hop, ancestors | {target}))
        level = nxt
        d += 1


def _read_cache(key: str) -> tuple[ExposureMap | None, dict | None]:
    """The newest valid cache file of ``key`` across :func:`_cache_dirs`, with its
    directory stamps (``None, None`` when there is none)."""
    best: tuple[ExposureMap, dict] | None = None
    for d in _cache_dirs():
        try:
            with open(d / f"exposure-{key}.json", encoding="utf-8") as fh:
                obj = json.load(fh)
            emap = ExposureMap.from_json(obj)
            dirs = obj.get("dirs")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        if best is None or emap.scanned > best[0].scanned:
            best = (emap, dirs if isinstance(dirs, dict) else {})
    return (best[0], best[1]) if best else (None, None)


def scan(roots: Mapping[str, os.PathLike | str] | None = None, depth: int = 4,
         prune: tuple[str, ...] = ("*_hips",), cache_s: int = 3600,
         url_prefixes: Mapping[str, str] | None = None) -> ExposureMap:
    """Walk the served roots and build the ``realpath -> URL prefix`` map.

    ``roots`` maps ``"public"``/``"secure"`` to a directory (default :data:`DEFAULT_ROOTS`).
    Directory symlinks are followed to ``depth`` levels below each root; entries whose name
    matches a ``prune`` glob are skipped (HiPS trees are huge and never hold symlinks to
    other trees). Each level's directories are listed by ``$PK_SCAN_THREADS`` worker threads
    (default 16); the result does not depend on the thread count. A directory reached by
    several routes (symlinks, or both roots) is listed once per scan.

    Caching (``cache_s > 0``): a result younger than ``cache_s`` seconds is returned from
    memory or from ``exposure-<key>.json`` under ``$PK_CACHE_DIR`` (default
    ``~/.cache/portalkit``; with ``$PK_CACHE_DIR`` set, the default directory is also read,
    never written). An older result is not trusted as is: the walk runs again, but every
    directory whose ``(st_dev, st_ino, st_mtime_ns, st_ctime_ns)`` stamp is unchanged reuses
    its recorded sub-directory and symlink names instead of being listed (one ``stat`` in
    place of a ``scandir``; every entry change, ``chmod``, ``chown`` and ``utime`` updates
    ctime), and every symlink is resolved again (see :class:`_Listings`), so the result
    equals a fresh walk by the same user and groups (a change of the scanning user's group
    membership changes no stamp; pass ``cache_s=0`` after one). ``cache_s=0`` disables every
    cache and forces a fresh walk.
    ``ExposureMap.stats`` reports the call counts of the walk (empty for a cached result).
    """
    roots_n = _norm_roots(DEFAULT_ROOTS if roots is None else roots)
    prefixes = dict(URL_PREFIXES)
    if url_prefixes:
        prefixes.update(url_prefixes)
    prefixes = {k: (v if v.endswith("/") else v + "/") for k, v in prefixes.items()}
    prune = tuple(prune)
    key = _cache_key(roots_n, prefixes, depth, prune)
    now = time.time()
    stamps: dict | None = None
    if cache_s > 0:
        hit = _MEM_CACHE.get(key)
        if hit is not None and now - hit[0] < cache_s:
            return hit[1]
        cached, stamps = _read_cache(key)
        if cached is not None and now - cached.scanned < cache_s:
            _MEM_CACHE[key] = (cached.scanned, cached)
            return cached
        if hit is not None and _MEM_STAMPS.get(key) is not None and (
                cached is None or hit[0] >= cached.scanned):
            stamps = _MEM_STAMPS[key]
    emap = ExposureMap(roots=roots_n, url_prefixes=prefixes, depth=depth, prune=prune,
                       scanned=now)
    listings = _Listings(stamps, record=cache_s > 0)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=_scan_threads()) as pool:
        for kind in _KINDS:
            if kind in roots_n:
                _walk_root(kind, roots_n[kind], prefixes[kind], depth, prune, emap,
                           listings, pool)
    emap.stats = dict(listings.counts, dirs=len(listings.mem),
                      wall_s=round(time.perf_counter() - t0, 3))
    if cache_s > 0:
        _MEM_CACHE[key] = (now, emap)
        _MEM_STAMPS[key] = listings.out
        _write_cache(_cache_dir() / f"exposure-{key}.json", emap, listings.out)
    return emap


def _write_cache(path: Path, emap: ExposureMap, dirs: Mapping | None = None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".pk-tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            obj = emap.to_json()
            obj["dirs"] = dict(dirs or {})
            json.dump(obj, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        # the cache is an optimisation only
        pass


def clear_cache() -> None:
    """Forget in-memory scan results (the on-disk cache expires by age)."""
    _MEM_CACHE.clear()
    _MEM_STAMPS.clear()


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
