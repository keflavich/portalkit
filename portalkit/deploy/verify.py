"""Deployed-portal checks (ARCHITECTURE.md section 11.5 and section 13 level 5).

``verify`` always runs the local checks: the ownership manifest is valid, every owned file
exists as a regular file with the recorded sha256, no ``.pk-tmp`` leftovers remain, replaced
legacy files still exist, and the target's exposure still satisfies the portal visibility.

``verify --http`` fetches every owned file with :mod:`urllib` (no ``requests``). Each body is
streamed to a temporary file and hashed afterwards, which avoids the ``curl | grep -q``
SIGPIPE false negative. Requests send ``Accept-Encoding: gzip`` as a browser does, so a server
that compresses ``.bin`` files (``mod_deflate``) is detected; a gzip-encoded body matches when
either the raw or the decoded bytes have the manifest sha256.

* secure portals: every URL must answer 401 without credentials; with ``PK_BASIC_AUTH``
  (``user:pass``) it must answer 200 and match the manifest sha256;
* public portals: every URL must answer 200 and match;
* ``Content-Type``, ``Content-Encoding`` (a failure on ``.bin``) and ``Cache-Control`` are
  reported, together with whether the ``.htaccess`` cache headers took effect;
* the ownership manifest URL (``<portal url>/.portalkit-deploy.json``, or the portal's
  secondary manifest) must answer 403 or 404 when the target holds the portalkit
  ``.htaccess`` deny rule (:func:`check_manifest_denied`). Any other answer, or a target
  without the rule whose manifest answers 200, is a warning (``VerifyReport.warnings``), never
  a failure: Apache ignores the file under ``AllowOverride None``.
* a 5xx answer while the target holds a portalkit ``.htaccess`` is reported as a probable
  ``AllowOverride`` problem: Apache answers 500 for every file of a directory whose
  ``.htaccess`` uses a directive class (``RedirectMatch`` and ``Header`` need ``FileInfo``)
  that ``AllowOverride`` does not allow. ``PK_DENY_HTACCESS=0`` stops deploy from writing the
  deny-only file (docs/deploy.md ".htaccess").
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import os
import zlib
import shutil
import stat
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urljoin

from . import DeployError
from .deploy import (TMP_SUFFIX, _get, load_manifest, manifest_path_for, resolve_target,
                     select_portal, sha256_file)
from .exposure import ExposureMap, check_target, classify
from .htaccess import (expected_cache_control, has_cache_headers, has_deny_rule,
                       htaccess_state)

DEFAULT_TIMEOUT = 30.0
_CHUNK = 1 << 20


@dataclass
class FileCheck:
    """One HTTP check."""

    path: str
    url: str
    ok: bool = True
    status: int | None = None            # status without credentials
    auth_status: int | None = None       # status with credentials (secure + PK_BASIC_AUTH)
    sha256: str | None = None
    expected: str | None = None
    size: int | None = None
    content_type: str | None = None
    content_encoding: str | None = None
    cache_control: str | None = None
    problems: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.problems.append(msg)


@dataclass
class VerifyReport:
    family: str
    portal: str
    target: str
    visibility: str
    base_url: str | None = None
    local: list[str] = field(default_factory=list)
    checks: list[FileCheck] = field(default_factory=list)
    htaccess: bool | None = None          # did the cache headers take effect (None: not checked)
    authenticated: bool = False
    notes: list[str] = field(default_factory=list)
    #: did the manifest URL answer 403/404 (None: not checked or not decidable, e.g. a secure
    #: portal checked without credentials)
    manifest_denied: bool | None = None
    #: problems that do not fail verify (exit 0), such as a served ownership manifest
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.local and all(c.ok for c in self.checks)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def to_json(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d

    def format(self) -> str:
        lines = [f"verify {self.family}/{self.portal} ({self.visibility}) target {self.target}"]
        for p in self.local:
            lines.append(f"  LOCAL  {p}")
        nfail = sum(1 for c in self.checks if not c.ok)
        if self.checks:
            lines.append(f"  http   {len(self.checks) - nfail}/{len(self.checks)} ok"
                         f"{' (authenticated)' if self.authenticated else ''} at {self.base_url}")
        for c in self.checks:
            if not c.ok:
                lines.append(f"  FAIL   {c.path}: {'; '.join(c.problems)}")
        if self.htaccess is not None:
            lines.append(f"  cache headers {'in effect' if self.htaccess else 'NOT in effect'}")
        if self.manifest_denied is not None:
            lines.append(f"  manifest URL {'denied' if self.manifest_denied else 'SERVED'}")
        for w in self.warnings:
            lines.append(f"  WARN   {w}")
        for n in self.notes:
            lines.append(f"  note   {n}")
        lines.append("  OK" if self.ok else "  FAILED")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# local checks
# ---------------------------------------------------------------------------

def verify_local(target: os.PathLike | str, man: Mapping | None) -> list[str]:
    """Problems with the deployed files on disk (empty when everything matches)."""
    tgt = Path(target)
    problems: list[str] = []
    if man is None:
        return [f"no ownership manifest in {tgt}"]
    for rel, ent in sorted(man.get("files", {}).items()):
        p = tgt / rel
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            problems.append(f"missing: {rel}")
            continue
        except NotADirectoryError:
            problems.append(f"missing (a file is in the way): {rel}")
            continue
        if stat.S_ISLNK(st.st_mode):
            problems.append(f"symlink: {rel}")
            continue
        if not stat.S_ISREG(st.st_mode):
            problems.append(f"not a regular file: {rel}")
            continue
        sha, size = sha256_file(p)
        if sha != ent.get("sha256") or size != ent.get("size", size):
            problems.append(f"sha256 mismatch: {rel}")
    for dirpath, dirnames, filenames in os.walk(tgt):
        for n in filenames:
            if n.endswith(TMP_SUFFIX):
                problems.append(f"leftover temporary file: "
                                f"{os.path.relpath(os.path.join(dirpath, n), tgt)}")
    for r in man.get("replaced", []):
        lp = Path(os.path.normpath(tgt / r["legacy"]))
        if not lp.is_file():
            problems.append(f"replaced legacy file missing: {r['legacy']}")
    return problems


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _auth_header(auth: str) -> str:
    return "Basic " + base64.b64encode(auth.encode("utf-8")).decode("ascii")


def _decoded_sha(path: str) -> str | None:
    """sha256 of the gunzipped content of ``path`` (``None`` if it is not gzip data)."""
    h = hashlib.sha256()
    try:
        with gzip.open(path, "rb") as fh:
            while True:
                buf = fh.read(_CHUNK)
                if not buf:
                    break
                h.update(buf)
    except (OSError, EOFError, zlib.error):
        return None
    return h.hexdigest()


def _fetch(url: str, *, auth: str | None, method: str, timeout: float, tmpdir: str,
           opener: urllib.request.OpenerDirector) -> tuple[int | None, Mapping, list[str],
                                                            int | None, str | None]:
    """Return ``(status, headers, sha256s, size, error)``; GET bodies go to a temp file first.

    ``sha256s`` holds the sha256 of the raw body and, for a gzip ``Content-Encoding``, of the
    decoded body as well."""
    req = urllib.request.Request(url, method=method,
                                 headers={"User-Agent": "portalkit-verify",
                                          "Accept-Encoding": "gzip"})
    if auth:
        req.add_header("Authorization", _auth_header(auth))
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, exc.headers or {}, [], None, None
    except urllib.error.URLError as exc:
        return None, {}, [], None, f"unreachable: {exc.reason}"
    except (TimeoutError, OSError) as exc:
        return None, {}, [], None, f"unreachable: {exc}"
    with resp:
        status = resp.status
        headers = resp.headers
        if method == "HEAD":
            return status, headers, [], None, None
        fd, tmp = tempfile.mkstemp(dir=tmpdir, suffix=".download")
        try:
            try:
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(resp, out, _CHUNK)
            except (TimeoutError, OSError) as exc:
                return status, headers, [], None, f"download failed: {exc}"
            sha, size = sha256_file(tmp)
            shas = [sha]
            if "gzip" in (headers.get("Content-Encoding") or "").lower():
                dec = _decoded_sha(tmp)
                if dec:
                    shas.append(dec)
        finally:
            os.unlink(tmp)
    return status, headers, shas, size, None


def file_url(base_url: str, rel: str) -> str:
    base = base_url if base_url.endswith("/") else base_url + "/"
    return base + quote(rel, safe="/-_.~")


def verify_http(base_url: str, files: Mapping[str, Mapping], *, visibility: str,
                auth: str | None = None, timeout: float = DEFAULT_TIMEOUT,
                replaced: list[Mapping] | None = None, htaccess_expected: bool = False,
                opener: urllib.request.OpenerDirector | None = None
                ) -> tuple[list[FileCheck], bool | None]:
    """Fetch every file of ``files`` (``{rel: {"sha256": ...}}``) under ``base_url``.

    Returns the checks and whether the section 11.5 cache headers took effect (``None`` when
    no file has an expected Cache-Control)."""
    opener = opener or urllib.request.build_opener()
    checks: list[FileCheck] = []
    cache_hits = cache_total = 0
    with tempfile.TemporaryDirectory(prefix="pk-verify-") as tmpdir:
        for rel in sorted(files):
            expected = files[rel].get("sha256")
            chk = FileCheck(path=rel, url=file_url(base_url, rel), expected=expected)
            if visibility == "secure":
                st, hdrs, _, _, err = _fetch(chk.url, auth=None, method="HEAD", timeout=timeout,
                                             tmpdir=tmpdir, opener=opener)
                chk.status = st
                if err:
                    chk.fail(err)
                elif st != 401:
                    chk.fail(f"secure URL answered {st} without credentials (expected 401)")
                if auth:
                    st2, hdrs, shas, size, err = _fetch(chk.url, auth=auth, method="GET",
                                                        timeout=timeout, tmpdir=tmpdir,
                                                        opener=opener)
                    chk.auth_status = st2
                    if err:
                        chk.fail(err)
                    elif st2 != 200:
                        chk.fail(f"answered {st2} with credentials (expected 200)")
                    chk.size = size
                else:
                    hdrs, shas = {}, []
            else:
                # public URLs must answer 200 without credentials (section 11.5), so
                # PK_BASIC_AUTH is never sent to them
                st, hdrs, shas, size, err = _fetch(chk.url, auth=None, method="GET",
                                                   timeout=timeout, tmpdir=tmpdir, opener=opener)
                chk.status, chk.size = st, size
                if err:
                    chk.fail(err)
                elif st != 200:
                    chk.fail(f"answered {st} (expected 200)"
                             + (" without credentials; a public portal must not need login"
                                if st == 401 else ""))
            if shas:
                chk.sha256 = expected if expected in shas else shas[0]
                if expected and expected not in shas:
                    chk.fail("sha256 differs from the ownership manifest")
            if hdrs:
                chk.content_type = hdrs.get("Content-Type")
                chk.content_encoding = hdrs.get("Content-Encoding")
                chk.cache_control = hdrs.get("Cache-Control")
                if rel.endswith(".bin") and chk.content_encoding:
                    chk.fail(f"Content-Encoding {chk.content_encoding!r} on a .bin file")
                want = expected_cache_control(rel)
                if want is not None and (chk.status == 200 or chk.auth_status == 200):
                    cache_total += 1
                    if (chk.cache_control or "").strip() == want:
                        cache_hits += 1
            checks.append(chk)
        for r in replaced or []:
            url = urljoin(base_url if base_url.endswith("/") else base_url + "/",
                          quote(r["legacy"], safe="/-_.~"))
            chk = FileCheck(path=r["legacy"], url=url)
            want = 401 if visibility == "secure" and not auth else 200
            st, _, _, _, err = _fetch(url, auth=auth if visibility == "secure" else None,
                                      method="GET", timeout=timeout, tmpdir=tmpdir, opener=opener)
            chk.status = st
            if err:
                chk.fail(err)
            elif st != want:
                chk.fail(f"replaced legacy URL answered {st} (expected {want})")
            checks.append(chk)
    if cache_total == 0 or (not htaccess_expected and cache_hits == 0):
        effective = None
    else:
        effective = cache_hits == cache_total
    return checks, effective


#: Warning text for a 5xx answer from a directory that holds a portalkit ``.htaccess``.
ALLOWOVERRIDE_HINT = ("the portalkit .htaccess is probably rejected by AllowOverride (its "
                      "RedirectMatch/Header/AddType directives need the FileInfo class; Apache "
                      "answers 500 for every file of the directory otherwise); allow FileInfo, "
                      "or set PK_DENY_HTACCESS=0 and remove the deny-only .htaccess")


def check_manifest_denied(base_url: str, manifest_rel: str, *, visibility: str,
                          deny_expected: bool, auth: str | None = None,
                          timeout: float = DEFAULT_TIMEOUT,
                          opener: urllib.request.OpenerDirector | None = None,
                          htaccess_present: bool = False
                          ) -> tuple[bool | None, int | None, str | None]:
    """HEAD ``<base_url>/<manifest_rel>`` (the ownership manifest, docs/deploy.md
    ".htaccess") and return ``(denied, status, warning)``.

    ``denied`` is True for 403 or 404, False for a 2xx (the manifest is served) and ``None``
    when the answer says nothing about the rule (unreachable, a 5xx, any other status, or
    401 from a secure portal checked without credentials: Apache applies a ``.htaccess``
    ``RedirectMatch`` after authentication). ``warning`` is set when the manifest is served (or the check failed)
    while ``deny_expected`` (the target holds the portalkit deny rule), when it is served
    without the rule, and for a 5xx while ``deny_expected`` or ``htaccess_present`` (the
    target holds a portalkit ``.htaccess``, with or without the deny rule): Apache rejecting
    the ``.htaccess`` (:data:`ALLOWOVERRIDE_HINT`). A 5xx is never reported as served;
    ``None`` otherwise."""
    opener = opener or urllib.request.build_opener()
    url = file_url(base_url, manifest_rel)
    send = auth if visibility == "secure" else None
    with tempfile.TemporaryDirectory(prefix="pk-verify-") as tmpdir:
        st, _, _, _, err = _fetch(url, auth=send, method="HEAD", timeout=timeout,
                                  tmpdir=tmpdir, opener=opener)
    if err:
        return None, None, (f"{url}: {err}; the deny rule was not checked"
                            if deny_expected else None)
    if st in (403, 404):
        return True, st, None
    if st == 401 and visibility == "secure" and not send:
        return None, st, None
    if st is not None and 500 <= st < 600:
        return None, st, (f"{url} answered {st}: {ALLOWOVERRIDE_HINT}"
                          if deny_expected or htaccess_present else None)
    if st is None or not 200 <= st < 300:
        # neither served nor denied (urllib follows redirects, so this is e.g. 400 or 410)
        return None, st, (f"{url} answered {st} (expected 403); the deny rule was not "
                          "confirmed" if deny_expected else None)
    if deny_expected:
        return False, st, (f"{url} answered {st} (expected 403): the .htaccess deny rule is "
                           "not in effect (AllowOverride None?), so the ownership manifest "
                           "and its absolute paths are served")
    return False, st, (f"{url} answered {st}: the target has no portalkit .htaccess deny "
                       "rule (a foreign .htaccess?), so the ownership manifest is served")


def foreign_record_urls(base_url: str, target: os.PathLike | str,
                        files: Mapping[str, Mapping]) -> list[tuple[str, str]]:
    """``[(label, absolute index URL)]`` of the foreign record sets (page.json records refs
    with ``base``, contract (A)) of the deployed pages among ``files``, resolved against the
    page URL under ``base_url``. Unreadable pages are skipped."""
    from ..legacy.extract import LegacyExtractError, script_json
    base = base_url if base_url.endswith("/") else base_url + "/"
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rel in sorted(files):
        if not rel.endswith(".html"):
            continue
        try:
            page = script_json(Path(target) / rel, "pk-page")
        except (LegacyExtractError, OSError, UnicodeDecodeError):
            continue
        dsets = page.get("datasets") if isinstance(page, Mapping) else None
        for ds, ref in (dsets or {}).items():
            if not (isinstance(ref, Mapping) and ref.get("kind") == "records"
                    and isinstance(ref.get("base"), str) and isinstance(ref.get("url"), str)):
                continue
            url = urljoin(file_url(base, rel), ref["url"])
            if url not in seen:
                seen.add(url)
                out.append((f"foreign records {ds} ({rel})", url))
    return out


def check_foreign_records(base_url: str, target: os.PathLike | str,
                          files: Mapping[str, Mapping], *, visibility: str,
                          auth: str | None = None, timeout: float = DEFAULT_TIMEOUT,
                          opener: urllib.request.OpenerDirector | None = None
                          ) -> list[FileCheck]:
    """HEAD the index of every foreign record set the deployed pages reference.

    A public index must answer 200. A secure portal may reference secure indexes: without
    credentials 401 counts as reachable, with ``auth`` (sent only from a secure portal)
    200 is expected. Anything else (404, 5xx, unreachable) fails the check."""
    opener = opener or urllib.request.build_opener()
    checks: list[FileCheck] = []
    with tempfile.TemporaryDirectory(prefix="pk-verify-") as tmpdir:
        for label, url in foreign_record_urls(base_url, target, files):
            chk = FileCheck(path=label, url=url)
            send = auth if visibility == "secure" else None
            st, hdrs, _, _, err = _fetch(url, auth=send, method="HEAD", timeout=timeout,
                                         tmpdir=tmpdir, opener=opener)
            chk.status = st
            if err:
                chk.fail(err)
            elif st == 401 and visibility == "secure" and not send:
                chk.problems.append("answered 401 (secure; not checked without credentials)")
            elif st != 200:
                chk.fail(f"foreign record index answered {st} (expected 200"
                         + (" or 401" if visibility == "secure" and not send else "") + ")")
            if hdrs:
                chk.content_type = hdrs.get("Content-Type")
                chk.cache_control = hdrs.get("Cache-Control")
            checks.append(chk)
    return checks


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def verify(family: str, site: Any, *, portal_id: str | None = None,
           target: os.PathLike | str | None = None, http: bool = False,
           auth_env: str = "PK_BASIC_AUTH", base_url: str | None = None,
           timeout: float = DEFAULT_TIMEOUT, emap: ExposureMap | None = None,
           check_exposure: bool = True, shared_legacy_dir: bool = False) -> VerifyReport:
    """``portalkit verify FAMILY [--portal ID] [--http] [--auth-env PK_BASIC_AUTH]``.

    ``site`` is the parsed site registry or one ``[[portal]]`` entry. ``base_url`` overrides
    the portal ``url`` (tests point it at a :class:`~portalkit.deploy.DevServer`)."""
    entry = select_portal(site, portal_id)
    pid = _get(entry, "id")
    visibility = _get(entry, "visibility")
    tgt = Path(os.path.abspath(target)) if target else resolve_target(entry, family,
                                                                      shared_legacy_dir)
    rep = VerifyReport(family=family, portal=pid, target=str(tgt), visibility=visibility)
    try:
        man = load_manifest(manifest_path_for(tgt, family, pid)) if tgt.is_dir() else None
    except DeployError as exc:
        rep.local.append(str(exc))
        return rep
    rep.local.extend(verify_local(tgt, man))
    if check_exposure:
        exp = classify(tgt, emap)
        dec = check_target(visibility, exp, _get(entry, "served_by"), yes=True)
        if not dec.ok:
            rep.local.append(f"exposure: {dec.message}")
        elif dec.message:
            rep.notes.append(dec.message)
    if http and man is not None:
        url = base_url or _get(entry, "url")
        if not url:
            rep.local.append("portal has no url; cannot verify over HTTP")
            return rep
        rep.base_url = url
        auth = os.environ.get(auth_env) if auth_env else None
        if auth is not None and ":" not in auth:
            rep.local.append(f"${auth_env} must look like user:pass")
            auth = None
        if visibility == "secure" and not auth:
            rep.notes.append(f"${auth_env} not set: secure files checked for 401 only "
                             "(no sha256 comparison)")
        rep.authenticated = bool(auth)
        rep.checks, rep.htaccess = verify_http(
            url, man.get("files", {}), visibility=visibility, auth=auth, timeout=timeout,
            replaced=man.get("replaced", []),
            htaccess_expected=has_cache_headers(tgt))
        n5xx = sum(1 for c in rep.checks
                   if any(x is not None and 500 <= x < 600 for x in (c.status, c.auth_status)))
        if n5xx and htaccess_state(tgt) in ("cache", "deny"):
            rep.warnings.append(f"{n5xx} owned URL(s) answered 5xx: {ALLOWOVERRIDE_HINT}")
        if rep.htaccess is False:
            rep.notes.append(".htaccess cache headers are not in effect (AllowOverride?); "
                             "the viewer works either way")
        fchecks = check_foreign_records(url, tgt, man.get("files", {}), visibility=visibility,
                                        auth=auth, timeout=timeout)
        rep.checks.extend(fchecks)
        if fchecks:
            rep.notes.append(f"{len(fchecks)} foreign record index(es) HEAD-checked")
        mrel = os.path.relpath(manifest_path_for(tgt, family, pid), tgt).replace(os.sep, "/")
        denied, mst, warn = check_manifest_denied(
            url, mrel, visibility=visibility, deny_expected=has_deny_rule(tgt), auth=auth,
            timeout=timeout, htaccess_present=htaccess_state(tgt) in ("cache", "deny"))
        rep.manifest_denied = denied
        if warn and warn not in rep.warnings:
            rep.warnings.append(warn)
        elif denied is None and mst == 401:
            rep.notes.append(f"{mrel} answered 401: the deny rule is checked with "
                             f"${auth_env} only")
    return rep

