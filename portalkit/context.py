"""Build context: site registry, cache, input registry and provenance (ARCHITECTURE.md §9.3).

* :class:`SiteConfig` is the parsed and validated ``sites/<family>.toml`` (section 4.1).
* :class:`InputRegistry` records every input file or directory (sha256, mtime, size) and
  rejects tmp paths (``/blue/*/tmp/``, ``/tmp/``, ``*/scratchpad/*``) unless the build runs
  with ``--allow-tmp-inputs``; a rejection raises :class:`~portalkit.deploy.BlockedInput`
  (exit code 5).
* :class:`Cache` memoizes expensive adapter steps under ``~/.cache/portalkit/<family>/``
  (``$PK_CACHE_DIR`` overrides the base). The key is ``(key, [(path, mtime_ns, size)])``
  over the declared inputs; values are stored as ``.npz`` (numpy arrays), ``.ecsv`` (astropy
  Tables) or JSON. Nothing is pickled.
* :class:`BuildContext` bundles these for the adapter (``ctx.legacy`` is the
  :class:`~portalkit.legacy.importshim.LegacyImporter`).
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping

import numpy as np

from .deploy import BlockedInput, UsageError
from .deploy.stage import is_tmp_path
from .legacy.importshim import LegacyImporter, LegacyImportError, sha256_file

__all__ = [
    "SITES_DIR", "PageConfig", "PortalConfig", "SiteConfig", "load_site", "expand_inputs",
    "InputRegistry", "Cache", "BuildContext", "cache_base", "LegacyImporter",
    "LegacyImportError",
]

#: default directory of the site registries (``<repo>/sites``)
SITES_DIR = Path(__file__).resolve().parents[1] / "sites"


def sites_dir(path: os.PathLike | str | None = None) -> Path:
    """``path``, else ``$PK_SITES_DIR``, else :data:`SITES_DIR`."""
    if path is not None:
        return Path(path)
    env = os.environ.get("PK_SITES_DIR")
    return Path(env) if env else SITES_DIR


# ---------------------------------------------------------------------------
# site registry (section 4.1)
# ---------------------------------------------------------------------------

@dataclass
class PageConfig:
    """One ``[[portal.page]]`` entry: the legacy -> unified mapping."""
    id: str
    legacy: list[str] = field(default_factory=list)
    preset: str = ""
    grammar: list[str] = field(default_factory=list)


@dataclass
class PortalConfig:
    """One ``[[portal]]`` entry."""
    id: str
    title: str
    visibility: Literal["public", "secure"]
    legacy_dir: str
    url: str
    target_dir: str | None = None
    served_by: str = "data.rc.ufl.edu"
    inputs: list[str] = field(default_factory=list)
    aladin_url: str | None = None
    pages: list[PageConfig] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def page(self, page_id: str) -> PageConfig | None:
        for p in self.pages:
            if p.id == page_id:
                return p
        return None


@dataclass
class SiteConfig:
    """Parsed ``sites/<family>.toml``."""
    family: str
    adapter: str | None = None
    tier_default: Literal["A", "B", "C"] = "B"
    legacy_paths: list[str] = field(default_factory=list)
    status: Literal["active", "excluded"] = "active"
    portals: list[PortalConfig] = field(default_factory=list)
    path: Path | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, obj: dict, path: os.PathLike | str | None = None) -> "SiteConfig":
        """Validate (``schema.validate_site``) and convert; :class:`UsageError` on errors."""
        from .schema import validate_site
        stem = Path(path).stem if path is not None else None
        errs = validate_site(obj, stem)
        if errs:
            raise UsageError(f"invalid site registry {path or obj.get('family')}", details=errs)
        portals = []
        for p in obj.get("portal", []) or []:
            pages = [PageConfig(id=g["id"], legacy=list(g.get("legacy", [])),
                                preset=str(g.get("preset", "")),
                                grammar=list(g.get("grammar", []))) for g in p.get("page", []) or []]
            portals.append(PortalConfig(
                id=p["id"], title=p["title"], visibility=p["visibility"],
                legacy_dir=p["legacy_dir"], url=p["url"], target_dir=p.get("target_dir"),
                served_by=p.get("served_by", "data.rc.ufl.edu"), inputs=list(p.get("inputs", [])),
                aladin_url=p.get("aladin_url"), pages=pages, raw=dict(p)))
        return cls(family=obj["family"], adapter=obj.get("adapter"),
                   tier_default=obj.get("tier_default", "B"),
                   legacy_paths=list(obj.get("legacy_paths", [])),
                   status=obj.get("status", "active"), portals=portals,
                   path=Path(path) if path is not None else None, raw=dict(obj))

    def portal(self, portal_id: str | None = None) -> PortalConfig:
        """The portal ``portal_id``; without an id, the only (or first) portal."""
        if not self.portals:
            raise UsageError(f"family {self.family!r} has no [[portal]] entries")
        if portal_id is None:
            return self.portals[0]
        for p in self.portals:
            if p.id == portal_id:
                return p
        raise UsageError(f"family {self.family!r} has no portal {portal_id!r} "
                         f"(have {[p.id for p in self.portals]})")

    @property
    def inputs(self) -> list[str]:
        """Every declared input pattern of every portal, in order, without repeats."""
        out: list[str] = []
        for p in self.portals:
            for i in p.inputs:
                if i not in out:
                    out.append(i)
        return out


def load_site(family: str, sites: os.PathLike | str | None = None) -> SiteConfig:
    """Load ``<sites>/<family>.toml`` (default :func:`sites_dir`)."""
    if not re.fullmatch(r"[a-z0-9_]+", family or ""):
        raise UsageError(f"bad family name {family!r} (expected [a-z0-9_]+)")
    path = sites_dir(sites) / f"{family}.toml"
    try:
        with open(path, "rb") as fh:
            obj = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise UsageError(f"no site registry {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"cannot parse {path}: {exc}") from exc
    return SiteConfig.from_dict(obj, path)


_GLOB_CHARS = re.compile(r"[*?\[]")


def expand_inputs(patterns: Iterable[str]) -> tuple[list[str], list[str]]:
    """Expand input globs. Returns ``(paths, unmatched)``: existing paths (sorted per
    pattern, without repeats) and the patterns that matched nothing."""
    out: list[str] = []
    missing: list[str] = []
    for pat in patterns:
        pat = os.path.expanduser(pat)
        hits = sorted(glob.glob(pat)) if _GLOB_CHARS.search(pat) else ([pat] if os.path.exists(pat) else [])
        if not hits:
            missing.append(pat)
        for h in hits:
            h = os.path.abspath(h)
            if h not in out:
                out.append(h)
    return out, missing


# ---------------------------------------------------------------------------
# cache base and the input registry
# ---------------------------------------------------------------------------

def cache_base() -> Path:
    """``$PK_CACHE_DIR`` or ``~/.cache/portalkit``."""
    env = os.environ.get("PK_CACHE_DIR")
    return Path(env) if env else Path.home() / ".cache" / "portalkit"


def _stat_key(path: str) -> tuple[str, int, int]:
    st = os.stat(path)
    return (os.path.abspath(path), int(st.st_mtime_ns), int(st.st_size))


def _tree_digest(path: str) -> tuple[str, int, int]:
    """(sha256 over sorted ``relpath, size, mtime_ns`` lines, newest mtime_ns, total size)
    of a directory tree; symlinked directories are not followed."""
    h = hashlib.sha256()
    newest = 0
    total = 0
    rows = []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except FileNotFoundError:
                continue
            rows.append(f"{os.path.relpath(full, path)}\t{st.st_size}\t{st.st_mtime_ns}\n")
            newest = max(newest, st.st_mtime_ns)
            total += st.st_size
    for r in sorted(rows):
        h.update(r.encode("utf-8"))
    return h.hexdigest(), newest, total


class InputRegistry:
    """Records build inputs: ``add(path)`` -> ``{path, sha256, mtime, size}``.

    Directories get a digest of their file listing (relative path, size, mtime) rather than
    of their bytes. File digests are cached in ``<cache_dir>/input_sha256.json`` keyed by
    ``(path, mtime_ns, size)``, so unchanged multi-GB inputs are hashed once. ``add_many``
    writes that file once at the end; a single ``add`` writes it when it hashed a new file.
    """

    def __init__(self, allow_tmp: bool = False, cache_dir: os.PathLike | str | None = None) -> None:
        self.allow_tmp = bool(allow_tmp)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._records: dict[str, dict] = {}
        self._sha_cache: dict[str, str] | None = None
        self._dirty = False
        self.tmp: list[str] = []

    # sha cache --------------------------------------------------------
    def _sha_cache_path(self) -> Path | None:
        return self.cache_dir / "input_sha256.json" if self.cache_dir is not None else None

    def _load_sha_cache(self) -> dict[str, str]:
        if self._sha_cache is None:
            self._sha_cache = {}
            p = self._sha_cache_path()
            if p is not None and p.is_file():
                try:
                    obj = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    obj = {}
                if isinstance(obj, dict):
                    self._sha_cache = {str(k): str(v) for k, v in obj.items()}
        return self._sha_cache

    def flush(self) -> None:
        """Write the digest cache if new digests were added since the last write."""
        if self._dirty:
            self._save_sha_cache()

    def _save_sha_cache(self) -> None:
        p = self._sha_cache_path()
        self._dirty = False
        if p is None or self._sha_cache is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(self._sha_cache, sort_keys=True), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass   # the digest cache is an optimisation only

    # public -------------------------------------------------------------
    def check(self, path: os.PathLike | str) -> None:
        """Raise :class:`BlockedInput` (exit 5) for a tmp path unless tmp inputs are allowed."""
        p = os.path.abspath(os.fspath(path))
        if is_tmp_path(p):
            if p not in self.tmp:
                self.tmp.append(p)
            if not self.allow_tmp:
                raise BlockedInput(f"input {p} lies under a tmp path (/blue/*/tmp/, /tmp/ or "
                                   f"*/scratchpad/*); copy it to a permanent location or build "
                                   f"with --allow-tmp-inputs (staging only, never deployable)")

    def add(self, path: os.PathLike | str, *, save: bool = True) -> dict:
        """Register ``path`` (file or directory); returns its record. ``save=False`` defers
        the digest-cache write to :meth:`flush`."""
        p = os.path.abspath(os.fspath(path))
        self.check(p)
        if not os.path.exists(p):
            raise FileNotFoundError(f"input {p} does not exist")
        if os.path.isdir(p):
            sha, mtime_ns, size = _tree_digest(p)
            kind = "dir"
        else:
            _, mtime_ns, size = _stat_key(p)
            key = f"{p}|{mtime_ns}|{size}"
            cache = self._load_sha_cache()
            sha = cache.get(key)
            if sha is None:
                sha = sha256_file(p)
                cache[key] = sha
                self._dirty = True
                if save:
                    self.flush()
            kind = "file"
        import datetime as _dt
        mtime = _dt.datetime.fromtimestamp(mtime_ns / 1e9, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rec = {"path": p, "sha256": sha, "mtime": mtime, "size": int(size), "kind": kind}
        self._records[p] = rec
        return dict(rec)

    def add_many(self, paths: Iterable[os.PathLike | str]) -> list[dict]:
        """``add`` each path; the digest cache is written once, also when one fails."""
        try:
            return [self.add(p, save=False) for p in paths]
        finally:
            self.flush()

    def records(self) -> list[dict]:
        """Every registered input, sorted by path."""
        return [dict(self._records[k]) for k in sorted(self._records)]

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, path: object) -> bool:
        return isinstance(path, (str, os.PathLike)) and os.path.abspath(os.fspath(path)) in self._records


# ---------------------------------------------------------------------------
# cache (no pickle)
# ---------------------------------------------------------------------------

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class Cache:
    """``memo(key, fn, inputs=[paths])``: compute once per input state, store without pickle.

    Supported values: a numpy array (``.npz``), a ``dict`` of numpy arrays (``.npz``), an
    astropy ``Table`` (``.ecsv``), or anything JSON-serialisable (``.json``). Other values
    raise ``TypeError``. With ``enabled=False`` (``--no-cache``) ``fn()`` always runs and
    nothing is written.

    ``scope`` (a JSON-serialisable dict) is folded into every key. :class:`BuildContext` sets
    it to ``{"tier", "limit"}``, so a value computed in a fixture run (``--limit``) or for
    another tier is never returned to a different build.
    """

    def __init__(self, directory: os.PathLike | str, *, enabled: bool = True,
                 registry: InputRegistry | None = None, log: Callable[[str], None] | None = None,
                 scope: Mapping[str, Any] | None = None) -> None:
        self.dir = Path(directory)
        self.enabled = bool(enabled)
        self.registry = registry
        self.log = log
        self.scope: dict[str, Any] | None = dict(scope) if scope is not None else None
        self.hits = 0
        self.misses = 0

    def key_hash(self, key: str, inputs: Iterable[os.PathLike | str] = ()) -> str:
        """sha256 of ``(key, [(path, mtime_ns, size)][, scope])`` (first 24 hex digits)."""
        stats = []
        for p in inputs:
            p = os.fspath(p)
            if os.path.isdir(p):
                sha, mtime_ns, size = _tree_digest(p)
                stats.append([os.path.abspath(p), mtime_ns, size, sha])
            else:
                stats.append(list(_stat_key(p)))
        parts: list[Any] = [str(key), stats]
        if self.scope:
            parts.append(self.scope)
        blob = json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:24]

    def _stem(self, key: str, h: str) -> Path:
        safe = _SAFE.sub("_", str(key)).strip("_")[:60] or "value"
        return self.dir / f"{safe}.{h}"

    def memo(self, key: str, fn: Callable[[], Any], inputs: Iterable[os.PathLike | str] = ()) -> Any:
        inputs = [os.fspath(p) for p in inputs]
        if self.registry is not None:
            self.registry.add_many(inputs)
        if not self.enabled:
            return fn()
        h = self.key_hash(key, inputs)
        stem = self._stem(key, h)
        meta_path = stem.with_name(stem.name + ".meta.json")
        if meta_path.is_file():
            try:
                value = self._load(stem, json.loads(meta_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                value = None
                ok = False
            else:
                ok = True
            if ok:
                self.hits += 1
                return value
        self.misses += 1
        value = fn()
        self._store(stem, meta_path, key, value)
        return value

    # storage -------------------------------------------------------------
    @staticmethod
    def _atomic(path: Path, writer: Callable[[Path], None]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}" + "".join(path.suffixes[-1:]))
        writer(tmp)
        os.replace(tmp, path)

    def _store(self, stem: Path, meta_path: Path, key: str, value: Any) -> None:
        from astropy.table import Table
        if isinstance(value, np.ndarray):
            if value.dtype == object:
                raise TypeError(f"cache {key!r}: object arrays cannot be cached without pickle")
            kind, ext = "ndarray", ".npz"
            self._atomic(stem.with_name(stem.name + ext),
                         lambda p: np.savez(p, value=value))
        elif isinstance(value, dict) and value and all(isinstance(v, np.ndarray) for v in value.values()):
            if any(v.dtype == object for v in value.values()):
                raise TypeError(f"cache {key!r}: object arrays cannot be cached without pickle")
            if not all(isinstance(k, str) and k.isidentifier() for k in value):
                raise TypeError(f"cache {key!r}: array dict keys must be identifiers")
            kind, ext = "arrays", ".npz"
            self._atomic(stem.with_name(stem.name + ext), lambda p: np.savez(p, **value))
        elif isinstance(value, Table):
            kind, ext = "table", ".ecsv"
            self._atomic(stem.with_name(stem.name + ext),
                         lambda p: value.write(p, format="ascii.ecsv", overwrite=True))
        else:
            try:
                text = json.dumps(value)
            except TypeError as exc:
                raise TypeError(f"cache {key!r}: {type(value).__name__} is not an ndarray, "
                                f"dict of ndarrays, Table or JSON value") from exc
            kind, ext = "json", ".json"
            self._atomic(stem.with_name(stem.name + ext), lambda p: p.write_text(text, encoding="utf-8"))
        meta = {"kind": kind, "file": stem.name + ext, "key": str(key)}
        self._atomic(meta_path, lambda p: p.write_text(json.dumps(meta), encoding="utf-8"))
        if self.log is not None:
            self.log(f"cache store {key} -> {stem.name}{ext}")

    def _load(self, stem: Path, meta: dict) -> Any:
        kind = meta["kind"]
        path = stem.parent / meta["file"]
        if kind == "ndarray":
            with np.load(path, allow_pickle=False) as z:
                return z["value"]
        if kind == "arrays":
            with np.load(path, allow_pickle=False) as z:
                return {k: z[k] for k in z.files}
        if kind == "table":
            from astropy.table import Table
            return Table.read(path, format="ascii.ecsv")
        if kind == "json":
            return json.loads(path.read_text(encoding="utf-8"))
        raise KeyError(f"unknown cache kind {kind!r}")


# ---------------------------------------------------------------------------
# build context
# ---------------------------------------------------------------------------

@dataclass
class BuildContext:
    """What an adapter sees (section 9.3). ``cache``, ``inputs`` and ``legacy`` are created
    from ``site`` when not given."""

    family: str
    site: SiteConfig
    staging: Path                       # the only place the builder writes
    tier: Literal["A", "B", "C"]
    limit: int | None = None            # fixture mode
    seed: int = 20261010
    allow_tmp_inputs: bool = False
    cache: Cache = None                 # type: ignore[assignment]
    inputs: InputRegistry = None        # type: ignore[assignment]
    legacy: LegacyImporter = None       # type: ignore[assignment]
    log: Callable[[str], None] = print

    def __post_init__(self) -> None:
        if self.tier not in ("A", "B", "C"):
            raise UsageError(f"tier must be A, B or C, got {self.tier!r}")
        self.staging = Path(self.staging)
        base = cache_base() / self.family
        if self.inputs is None:
            self.inputs = InputRegistry(self.allow_tmp_inputs, cache_dir=base)
        if self.cache is None:
            self.cache = Cache(base, registry=self.inputs)
        if self.cache.scope is None:
            self.cache.scope = {"tier": self.tier, "limit": self.limit}
        if self.legacy is None:
            self.legacy = LegacyImporter(self.site.legacy_paths if self.site else ())

    @property
    def fixture(self) -> bool:
        return self.limit is not None
